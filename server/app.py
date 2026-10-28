"""Sigma telemetry collector: anonymous play sessions and funnel events from a static web game.

The browser posts JSON as text/plain (a CORS "simple" request: no preflight, no cookies), either
with fetch(keepalive) on a heartbeat or navigator.sendBeacon when the page hides. Nothing here is
trusted: the Origin allowlist only keeps honest browsers on other sites out, so every field is
clamped and every collection capped. Design follows the jam.my.games ext-play ingest.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query, Request, Response

DB_PATH = os.environ.get("DB_PATH", "telemetry.db")
ALLOWED_ORIGINS = {
    o.strip().rstrip("/")
    for o in os.environ.get(
        "ALLOWED_ORIGINS", "https://sigma.gritsenko.biz,http://localhost:5380"
    ).split(",")
    if o.strip()
}
# Requests without an Origin header (curl, tests) are refused unless this is set.
ALLOW_NO_ORIGIN = os.environ.get("ALLOW_NO_ORIGIN") == "1"
STATS_TOKEN = os.environ.get("STATS_TOKEN", "")
# Behind Caddy the client IP comes in X-Forwarded-For; trust it only when told so.
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "0"))

MAX_BODY = 16 * 1024
MAX_EVENTS_PER_REQUEST = 50
MAX_EVENTS_PER_SESSION = 1000
MAX_EVENT_NAMES = 200
MAX_PARAMS = 1024
# Active time can't run ahead of the wall clock by more than one heartbeat's slack.
ACTIVE_SLACK_MS = 30_000
MAX_SESSION_MS = 12 * 3600_000

ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
NAME_RE = re.compile(r"[^a-z0-9_]+")
SHORT_RE = re.compile(r"[^A-Za-z0-9_.+-]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  client TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  active_ms INTEGER NOT NULL DEFAULT 0,
  events INTEGER NOT NULL DEFAULT 0,
  ended INTEGER NOT NULL DEFAULT 0,
  build TEXT NOT NULL DEFAULT '',
  lang TEXT NOT NULL DEFAULT '',
  ua TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS sessions_client ON sessions(client);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  session TEXT NOT NULL,
  client TEXT NOT NULL,
  ts INTEGER NOT NULL,
  at_ms INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL,
  params TEXT
);
CREATE INDEX IF NOT EXISTS events_name_ts ON events(name, ts);
CREATE INDEX IF NOT EXISTS events_client ON events(client, name);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Db:
    """One connection behind a lock: a single uvicorn worker, writes are tiny."""

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.names = {r[0] for r in self.conn.execute("SELECT DISTINCT name FROM events")}

    def purge(self, days: int) -> None:
        if days <= 0:
            return
        cutoff = now_ms() - days * 86400_000
        with self.lock:
            self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM sessions WHERE last_seen < ?", (cutoff,))


@dataclass
class Bucket:
    tokens: float
    at: float


class RateLimit:
    """Token bucket per IP: a burst for the pagehide beacon, then one request per `period` s."""

    def __init__(self, burst: int = 20, period: float = 2.0) -> None:
        self.burst, self.period = burst, period
        self.buckets: dict[str, Bucket] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        t = time.monotonic()
        with self.lock:
            if len(self.buckets) > 50_000:
                self.buckets = {
                    k: b for k, b in self.buckets.items() if t - b.at < self.burst * self.period
                }
            b = self.buckets.get(key) or Bucket(self.burst, t)
            b.tokens = min(self.burst, b.tokens + (t - b.at) / self.period)
            b.at = t
            self.buckets[key] = b
            if b.tokens < 1:
                return False
            b.tokens -= 1
            return True


db: Db
limiter = RateLimit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global db
    db = Db(DB_PATH)
    db.purge(RETENTION_DAYS)
    yield
    db.conn.close()


app = FastAPI(title="sigma-telemetry", lifespan=lifespan, docs_url=None, redoc_url=None)


def cors(origin: str | None) -> dict[str, str]:
    if origin and origin in ALLOWED_ORIGINS:
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    return {}


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def short(v: object, n: int) -> str:
    return SHORT_RE.sub("", v)[:n] if isinstance(v, str) else ""


def as_int(v: object) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def event_name(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return NAME_RE.sub("_", raw.strip().lower()).strip("_")[:64]


def params_text(raw: object) -> str | None:
    if not isinstance(raw, dict) or not raw:
        return None
    text = json.dumps(raw, separators=(",", ":"), ensure_ascii=False)
    return text if len(text) <= MAX_PARAMS else json.dumps({"_too_long": len(text)})


@app.options("/e")
def preflight(request: Request) -> Response:
    headers = cors(request.headers.get("origin"))
    if headers:
        headers |= {
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Max-Age": "86400",
        }
    return Response(status_code=204, headers=headers)


@app.post("/e")
async def ingest(request: Request) -> Response:
    origin = request.headers.get("origin")
    headers = cors(origin)
    if not headers and not (origin is None and ALLOW_NO_ORIGIN):
        return Response(status_code=403)
    if not limiter.allow(client_ip(request)):
        return Response(status_code=429, headers=headers)
    body = await request.body()
    if len(body) > MAX_BODY:
        return Response(status_code=413, headers=headers)
    try:
        data = json.loads(body)
    except ValueError:
        return Response(status_code=400, headers=headers)
    if not isinstance(data, dict):
        return Response(status_code=400, headers=headers)
    client, session = data.get("client"), data.get("session")
    if not (isinstance(client, str) and ID_RE.match(client)):
        return Response(status_code=400, headers=headers)
    if not (isinstance(session, str) and ID_RE.match(session)):
        return Response(status_code=400, headers=headers)
    if not record(data, client, session, request.headers.get("user-agent", "")):
        return Response(status_code=409, headers=headers)
    return Response(status_code=204, headers=headers)


def record(data: dict, client: str, session: str, ua: str) -> bool:
    t = now_ms()
    c = db.conn
    with db.lock:
        c.execute("BEGIN")
        try:
            c.execute(
                "INSERT INTO sessions (id, client, started_at, last_seen, build, lang, ua)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (
                    session,
                    client,
                    t,
                    t,
                    short(data.get("build"), 40),
                    short(data.get("lang"), 16),
                    ua[:200],
                ),
            )
            row = c.execute(
                "SELECT client, started_at, events FROM sessions WHERE id = ?", (session,)
            ).fetchone()
            # A session id is bound to the client that opened it.
            if row["client"] != client:
                c.execute("ROLLBACK")
                return False
            wall = t - row["started_at"] + ACTIVE_SLACK_MS
            active = max(0, min(as_int(data.get("activeMs")), wall, MAX_SESSION_MS))
            room = max(0, MAX_EVENTS_PER_SESSION - row["events"])
            raw_events = data.get("events")
            events = raw_events[: min(MAX_EVENTS_PER_REQUEST, room)] if isinstance(raw_events, list) else []
            rows = []
            for e in events:
                if not isinstance(e, dict):
                    continue
                name = event_name(e.get("name"))
                if not name:
                    continue
                if name not in db.names:
                    if len(db.names) >= MAX_EVENT_NAMES:
                        name = "_other"
                    db.names.add(name)
                at = max(0, min(as_int(e.get("at")), MAX_SESSION_MS))
                rows.append((session, client, t, at, name, params_text(e.get("params"))))
            c.executemany(
                "INSERT INTO events (session, client, ts, at_ms, name, params)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            c.execute(
                "UPDATE sessions SET last_seen = ?, active_ms = MAX(active_ms, ?),"
                " events = events + ?, ended = MAX(ended, ?) WHERE id = ?",
                (t, active, len(rows), 1 if data.get("ended") is True else 0, session),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return True


# --- reading -------------------------------------------------------------------------------


def authorize(request: Request) -> None:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    token = token or request.query_params.get("token", "")
    if not STATS_TOKEN or token != STATS_TOKEN:
        raise HTTPException(status_code=401)


def since(days: int) -> int:
    return now_ms() - days * 86400_000


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/stats/summary")
def summary(request: Request, days: int = Query(30, ge=1, le=365)) -> dict:
    """Per day (UTC): sessions, distinct and first-time clients, active minutes."""
    authorize(request)
    rows = db.conn.execute(
        """
        WITH firsts AS (SELECT client, MIN(started_at) AS first FROM sessions GROUP BY client)
        SELECT date(s.started_at / 1000, 'unixepoch') AS day,
               COUNT(*) AS sessions,
               COUNT(DISTINCT s.client) AS clients,
               COUNT(DISTINCT CASE WHEN date(f.first / 1000, 'unixepoch')
                                      = date(s.started_at / 1000, 'unixepoch')
                                   THEN s.client END) AS new_clients,
               ROUND(SUM(s.active_ms) / 60000.0, 1) AS active_min,
               ROUND(AVG(s.active_ms) / 60000.0, 1) AS avg_active_min
        FROM sessions s JOIN firsts f ON f.client = s.client
        WHERE s.started_at >= ?
        GROUP BY day ORDER BY day
        """,
        (since(days),),
    ).fetchall()
    return {"days": [dict(r) for r in rows]}


@app.get("/stats/events")
def events(
    request: Request, days: int = Query(30, ge=1, le=365), build: str | None = None
) -> dict:
    """Per event name: total, distinct sessions and clients."""
    authorize(request)
    sql = (
        "SELECT e.name, COUNT(*) AS count, COUNT(DISTINCT e.session) AS sessions,"
        " COUNT(DISTINCT e.client) AS clients FROM events e"
    )
    args: list = [since(days)]
    if build:
        sql += " JOIN sessions s ON s.id = e.session WHERE e.ts >= ? AND s.build = ?"
        args.append(build)
    else:
        sql += " WHERE e.ts >= ?"
    rows = db.conn.execute(sql + " GROUP BY e.name ORDER BY clients DESC", args).fetchall()
    return {"events": [dict(r) for r in rows]}


@app.get("/stats/funnel")
def funnel(
    request: Request, steps: str, days: int = Query(30, ge=1, le=365)
) -> dict:
    """Clients that reached each step in order, by the first occurrence of each.

    An event's time is its session's start plus the client's offset: heartbeats batch events,
    so the server receive time can't order two events of one batch.
    """
    authorize(request)
    names = [event_name(s) for s in steps.split(",") if event_name(s)][:20]
    if not names:
        raise HTTPException(status_code=400)
    marks = ",".join("?" * len(names))
    first: dict[str, dict[str, int]] = {}
    for r in db.conn.execute(
        f"SELECT e.client, e.name, MIN(s.started_at + e.at_ms) AS t"
        f" FROM events e JOIN sessions s ON s.id = e.session"
        f" WHERE e.ts >= ? AND e.name IN ({marks}) GROUP BY e.client, e.name",
        [since(days), *names],
    ):
        first.setdefault(r["client"], {})[r["name"]] = r["t"]
    counts = [0] * len(names)
    for seen in first.values():
        prev = -1
        for i, n in enumerate(names):
            t = seen.get(n)
            if t is None or t < prev:
                break
            counts[i] += 1
            prev = t
    return {"steps": [{"name": n, "clients": c} for n, c in zip(names, counts)]}


@app.get("/stats/params")
def params(
    request: Request, name: str, key: str, days: int = Query(30, ge=1, le=365)
) -> dict:
    """Distribution of one params field of one event, e.g. name=civ_chosen&key=civ."""
    authorize(request)
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", key):
        raise HTTPException(status_code=400)
    rows = db.conn.execute(
        "SELECT json_extract(params, ?) AS value, COUNT(*) AS count,"
        " COUNT(DISTINCT client) AS clients FROM events"
        " WHERE name = ? AND ts >= ? AND json_valid(params)"
        " GROUP BY value ORDER BY count DESC LIMIT 200",
        (f"$.{key}", event_name(name), since(days)),
    ).fetchall()
    return {"values": [dict(r) for r in rows]}
