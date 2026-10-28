import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

ORIGIN = "https://sigma.gritsenko.biz"
TOKEN = "t0ken"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("STATS_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
    import app

    importlib.reload(app)
    with TestClient(app.app) as c:
        c.app_module = app
        yield c


def send(c, body, origin=ORIGIN):
    headers = {"content-type": "text/plain;charset=UTF-8"}
    if origin:
        headers["origin"] = origin
    raw = body if isinstance(body, str) else json.dumps(body)
    return c.post("/e", content=raw, headers=headers)


def beat(session="s" * 32, client="c" * 32, events=(), **kw):
    return {"client": client, "session": session, "build": "abc", "lang": "ru-RU",
            "activeMs": 0, "ended": False, "events": list(events), **kw}


def stats(c, path):
    r = c.get(path, headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    return r.json()


def test_ingest_and_cors(client):
    r = send(client, beat(events=[{"name": "Game Start", "at": 5, "params": {"civ": "rus"}}]))
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == ORIGIN
    ev = stats(client, "/stats/events")["events"]
    assert ev == [{"name": "game_start", "count": 1, "sessions": 1, "clients": 1}]


def test_foreign_origin_and_no_origin_refused(client):
    assert send(client, beat(), origin="https://evil.example").status_code == 403
    assert send(client, beat(), origin=None).status_code == 403


def test_bad_bodies(client):
    assert send(client, "not json").status_code == 400
    assert send(client, beat(client="short")).status_code == 400
    assert send(client, "x" * 20000).status_code == 413


def test_session_bound_to_client(client):
    assert send(client, beat()).status_code == 204
    assert send(client, beat(client="d" * 32)).status_code == 409


def test_active_time_clamped_and_monotonic(client):
    send(client, beat(activeMs=10_000_000))
    row = client.app_module.db.conn.execute("SELECT active_ms FROM sessions").fetchone()
    assert row[0] <= 31_000
    send(client, beat(activeMs=1000))
    row2 = client.app_module.db.conn.execute("SELECT active_ms FROM sessions").fetchone()
    assert row2[0] == row[0]


def test_event_caps(client):
    app = client.app_module
    send(client, beat(events=[{"name": f"e{i}"} for i in range(80)]))
    assert app.db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 50
    app.db.names = {f"n{i}" for i in range(app.MAX_EVENT_NAMES)}
    send(client, beat(session="t" * 32, events=[{"name": "brand_new"}]))
    names = {r[0] for r in app.db.conn.execute("SELECT name FROM events WHERE session = ?", ("t" * 32,))}
    assert names == {"_other"}


def test_long_params_replaced(client):
    send(client, beat(events=[{"name": "x", "params": {"k": "v" * 2000}}]))
    p = client.app_module.db.conn.execute("SELECT params FROM events").fetchone()[0]
    assert json.loads(p) == {"_too_long": 2008}


def test_funnel_respects_order(client):
    send(client, beat(client="a" * 32, session="1" * 32,
                      events=[{"name": "open", "at": 1}, {"name": "start", "at": 2},
                              {"name": "city", "at": 3}]))
    # Reached "city" before "start": counts for the first two steps only... and stops at start.
    send(client, beat(client="b" * 32, session="2" * 32,
                      events=[{"name": "open", "at": 1}, {"name": "city", "at": 2},
                              {"name": "start", "at": 3}]))
    steps = stats(client, "/stats/funnel?steps=open,start,city")["steps"]
    assert [s["clients"] for s in steps] == [2, 2, 1]


def test_params_distribution(client):
    for i, civ in enumerate(["rus", "rus", "ger"]):
        send(client, beat(client=f"{i}" * 32, session=f"{i}" * 32,
                          events=[{"name": "civ_chosen", "params": {"civ": civ}}]))
    values = stats(client, "/stats/params?name=civ_chosen&key=civ")["values"]
    assert values[0] == {"value": "rus", "count": 2, "clients": 2}


def test_stats_need_token(client):
    assert client.get("/stats/summary").status_code == 401
    days = stats(client, "/stats/summary")["days"]
    assert days == []
    send(client, beat())
    assert stats(client, "/stats/summary")["days"][0]["sessions"] == 1
