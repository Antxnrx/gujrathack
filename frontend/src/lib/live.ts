/**
 * One SSE connection for the whole console.
 *
 * The backend fans out on a single `/api/live` stream and drops any topic outside
 * `bus.CHANNELS`, so there is nothing to gain from a second connection and something
 * to lose: each one holds a queue in the API process (`sse_queue_size`, 200) and the
 * server runs one worker on purpose, because the broker's LISTEN connection and the
 * subscriber set live in that process's memory.
 *
 * So: one EventSource, reference-counted, shared by every component that subscribes.
 * A component gets a callback; it never gets the socket.
 */

import { renewToken } from "./api";

type Handler = (topic: string, data: unknown) => void;

// The frame names are the bus channel names verbatim — sse.frame() passes
// `event=message["topic"]` straight through — so these are `setu_*`, not the friendlier
// names the routes use. Getting them wrong produces a stream that connects, keepalives
// forever and delivers nothing, which is the failure this list exists to prevent.
const TOPICS = ["setu_events", "setu_alerts", "setu_pursuits"] as const;

/** What the console calls each channel internally. */
export const EVENTS = "setu_events";
export const ALERTS = "setu_alerts";
export const PURSUITS = "setu_pursuits";

let source: EventSource | null = null;
let handlers = new Set<Handler>();
/** Declared as a named union rather than inferred from the variable: `typeof state` on a
 *  `let` narrows to whatever it currently holds, so consumers saw the literal "idle" and
 *  a setter that could never be handed "error". */
export type LiveState = "idle" | "connecting" | "open" | "error";

let state: LiveState = "idle";
const stateWatchers = new Set<(s: LiveState) => void>();

/** Consecutive failed reopen attempts. Reset the moment a connection opens. */
let retries = 0;
/** Past this, stop: something is wrong that reconnecting cannot fix, and an
 *  unbounded reopen loop against the API is worse than a badge reading
 *  "offline". */
const MAX_RETRIES = 4;
let retryTimer: number | undefined;

function setState(s: LiveState) {
  state = s;
  stateWatchers.forEach((w) => w(s));
}

function open() {
  if (source) return;
  setState("connecting");
  // The token rides in the query string because EventSource cannot set headers.
  // See backend/app/security.py::current_operator for why that trade is made.
  const tok = localStorage.getItem("setu.token");
  const es = new EventSource("/api/live" + (tok ? `?access_token=${encodeURIComponent(tok)}` : ""));
  source = es;

  // Named events, plus `message` for the keepalive and anything the server adds later.
  // Registering per topic rather than one `message` handler means a payload never has
  // to be parsed just to discover it is a heartbeat.
  for (const topic of TOPICS) {
    es.addEventListener(topic, (ev) => {
      let data: unknown = null;
      try {
        data = JSON.parse((ev as MessageEvent).data);
      } catch {
        return;
      }
      handlers.forEach((h) => h(topic, data));
    });
  }
  es.onopen = () => { retries = 0; setState("open"); };
  es.onerror = () => {
    // A dropped connection and a refused one are different failures.
    //
    // EventSource retries a dropped *connection* on its own, and for that this
    // only has to report. It does not retry an HTTP error: a non-2xx response
    // puts the stream in CLOSED permanently. The token is baked into the URL
    // because EventSource cannot set headers, and the access token lives fifteen
    // minutes — so the ordinary course of leaving the console open was a stream
    // that died at the fifteen-minute mark and a badge that read "offline" for
    // the rest of the shift, with live alerts silently not arriving.
    //
    // So a CLOSED stream renews the token and opens a new one, bounded. This is
    // the same fix the video player needed for the same reason; the difference
    // is that hls.js retries too eagerly and EventSource not at all.
    if (es.readyState !== EventSource.CLOSED) { setState("connecting"); return; }

    setState("error");
    if (retries >= MAX_RETRIES || handlers.size === 0) return;
    retries += 1;
    const wait = Math.min(30_000, 1000 * 2 ** (retries - 1));
    window.clearTimeout(retryTimer);
    retryTimer = window.setTimeout(() => {
      // Nobody may be listening any more by the time this fires.
      if (handlers.size === 0) return;
      renewToken().then((ok) => {
        if (!ok || handlers.size === 0) return;
        source?.close();
        source = null;
        open();
      });
    }, wait);
  };
}

function close() {
  window.clearTimeout(retryTimer);
  retries = 0;
  source?.close();
  source = null;
  setState("idle");
}

export function subscribe(handler: Handler): () => void {
  handlers.add(handler);
  open();
  return () => {
    handlers.delete(handler);
    if (handlers.size === 0) close();
  };
}

export function watchState(w: (s: LiveState) => void): () => void {
  stateWatchers.add(w);
  w(state);
  return () => stateWatchers.delete(w);
}

