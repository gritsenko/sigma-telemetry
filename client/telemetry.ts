/**
 * Sigma telemetry client: anonymous sessions and named events for a static web game.
 *
 * No dependencies, no cookies. The body is a plain string, so fetch sends it as text/plain: a
 * CORS "simple" request with no preflight. On pagehide the rest goes by sendBeacon; a batch the
 * browser refused is kept in localStorage and sent first on the next load.
 *
 * Drop this file into the game (e.g. packages/web/src/telemetry.ts) and create one instance.
 */

export type Params = Record<string, string | number | boolean | null>;

export interface TelemetryOptions {
  /** Collector URL, e.g. https://stats.example.com/e. */
  endpoint: string;
  /** Build id (git hash, date): lets the stats split by version. */
  build: string;
  lang?: string;
  /** Active play between heartbeats, ms. */
  heartbeatMs?: number;
}

export interface Telemetry {
  readonly clientId: string;
  readonly sessionId: string;
  track(name: string, params?: Params): void;
  /** Off persists in localStorage and drops whatever is queued. */
  setEnabled(on: boolean): void;
  readonly enabled: boolean;
}

interface QueuedEvent {
  name: string;
  at: number;
  params?: Params;
}

interface Payload {
  client: string;
  session: string;
  build: string;
  lang: string;
  activeMs: number;
  ended: boolean;
  events: QueuedEvent[];
}

const CLIENT_KEY = 'sigma.telemetry.client';
const OFF_KEY = 'sigma.telemetry.off';
const PENDING_KEY = 'sigma.telemetry.pending';
const BATCH = 50;
const MAX_QUEUE = 200;
const FLUSH_AT = 20;
const MAX_FAILURES = 3;

function randomId(): string {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

// Storage may throw (private mode, blocked site data): telemetry must never break the game.
function load(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function save(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

export function createTelemetry(o: TelemetryOptions): Telemetry {
  const clientId = load(CLIENT_KEY) ?? randomId();
  save(CLIENT_KEY, clientId);
  const sessionId = randomId();
  const started = performance.now();
  const heartbeatMs = o.heartbeatMs ?? 20_000;
  let enabled = load(OFF_KEY) !== '1';
  let queue: QueuedEvent[] = [];
  let activeMs = 0;
  let sinceBeat = 0;
  let failures = 0;
  let ended = false;
  let inFlight = false;

  const payload = (events: QueuedEvent[], end: boolean): Payload => ({
    client: clientId,
    session: sessionId,
    build: o.build,
    lang: o.lang ?? navigator.language,
    activeMs,
    ended: end,
    events,
  });

  const post = (body: string): Promise<boolean> =>
    fetch(o.endpoint, { method: 'POST', body, keepalive: true, credentials: 'omit' }).then(
      (r) => r.ok || r.status === 409 || r.status === 413,
      () => false,
    );

  async function flush(): Promise<void> {
    if (!enabled || inFlight || failures >= MAX_FAILURES) return;
    inFlight = true;
    const batch = queue.slice(0, BATCH);
    const ok = await post(JSON.stringify(payload(batch, false)));
    inFlight = false;
    if (ok) {
      queue = queue.slice(batch.length);
      failures = 0;
    } else {
      failures++;
    }
  }

  // The previous load's batch the browser refused to beacon: its own session, sent once.
  const pending = load(PENDING_KEY);
  save(PENDING_KEY, null);
  if (pending && enabled) void post(pending);

  function finish(): void {
    if (!enabled || ended) return;
    ended = true;
    const body = JSON.stringify(payload(queue.slice(0, BATCH), true));
    queue = [];
    const sent =
      typeof navigator.sendBeacon === 'function' &&
      navigator.sendBeacon(o.endpoint, new Blob([body], { type: 'text/plain' }));
    if (!sent) save(PENDING_KEY, body);
  }

  // Only time with the tab visible and focused counts as play.
  setInterval(() => {
    if (ended || document.visibilityState !== 'visible' || !document.hasFocus()) return;
    activeMs += 1000;
    sinceBeat += 1000;
    if (sinceBeat >= heartbeatMs) {
      sinceBeat = 0;
      void flush();
    }
  }, 1000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') void flush();
  });
  // A page restored from the bfcache goes on as a new beat of the same session.
  addEventListener('pagehide', finish);
  addEventListener('pageshow', (e) => {
    if (e.persisted) ended = false;
  });

  return {
    clientId,
    sessionId,
    get enabled() {
      return enabled;
    },
    track(name, params) {
      if (!enabled || ended) return;
      if (queue.length >= MAX_QUEUE) queue.shift();
      queue.push({ name, at: Math.round(performance.now() - started), ...(params && { params }) });
      if (queue.length >= FLUSH_AT) void flush();
    },
    setEnabled(on) {
      enabled = on;
      save(OFF_KEY, on ? null : '1');
      if (!on) queue = [];
    },
  };
}
