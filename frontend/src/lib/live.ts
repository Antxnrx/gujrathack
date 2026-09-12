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
  es.onopen = () => setState("open");
  es.onerror = () => {
    // EventSource reconnects on its own with backoff. Closing it here would replace
    // that with nothing, so this only reports.
    setState(es.readyState === EventSource.CLOSED ? "error" : "connecting");
  };
}

function close() {
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

