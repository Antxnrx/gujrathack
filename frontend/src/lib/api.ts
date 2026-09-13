/**
 * The single door to the API.
 *
 * Everything goes through `request` so that four things are decided once instead of
 * per call site: the `/api` prefix, the bearer token when Model 1 is configured, the
 * abort signal React Query supplies when a view unmounts, and what an error *is*.
 *
 * That last one matters more than it looks. The API's unhandled-error handler returns
 * `{error, type}` and its validation failures return FastAPI's `{detail: [...]}`, so a
 * component that reads `err.message` gets "[object Object]" from one of them unless
 * something flattens both. Here is that something.
 */

const BASE = "/api";

/** Set by the login shim when Model 1 issues a token. Absent in the offline demo,
 *  where the API runs in its documented `auth: disabled` mode. */
let bearer: string | null = null;
/** Model 1 issues a refresh token beside the access token. The console used to
 *  discard it, which is why a session lasted exactly fifteen minutes. */
let refresh: string | null = null;
/** Where Model 1 lives. Recorded at sign-in so a renewal does not need the shell
 *  to have loaded /api/health first — a token can expire before it has. */
let registry: string | null = null;

export function setToken(token: string | null, refreshToken?: string | null,
                         model1?: string | null): void {
  bearer = token;
  if (token) localStorage.setItem("setu.token", token);
  else localStorage.removeItem("setu.token");

  if (refreshToken !== undefined) {
    refresh = refreshToken;
    if (refreshToken) localStorage.setItem("setu.refresh", refreshToken);
    else localStorage.removeItem("setu.refresh");
  }
  if (model1) {
    registry = model1;
    localStorage.setItem("setu.model1", model1);
  }
  if (token === null && refreshToken === undefined) {
    // Dropping the access token without saying anything about the refresh token
    // means the session is over — clear both, or the next load silently retries
    // with a refresh token whose session has gone.
    refresh = null;
    localStorage.removeItem("setu.refresh");
  }
}

/** The current access token, for the one caller that cannot go through
 *  `request`: the HLS player, which hands URLs to hls.js and to the browser's
 *  own media element rather than fetching them itself. */
export function getToken(): string | null {
  return bearer;
}

export function loadToken(): void {
  bearer = localStorage.getItem("setu.token");
  refresh = localStorage.getItem("setu.refresh");
  registry = localStorage.getItem("setu.model1");
}

/**
 * Renew the access token, at most once at a time.
 *
 * Model 1's access tokens live fifteen minutes. Every panel on this console
 * polls, so an expiry does not fail one request — it fails eight at once, and
 * eight parallel refreshes would race, each rotating the refresh token and
 * invalidating the others. The in-flight promise is shared so the burst becomes
 * one renewal that every caller waits on.
 *
 * Returns true if the token was renewed and the caller should retry.
 */
let renewing: Promise<boolean> | null = null;

export function renewToken(): Promise<boolean> {
  return renew();
}

function renew(): Promise<boolean> {
  if (renewing) return renewing;
  const base = registry ?? "";
  if (!refresh || !base) return Promise.resolve(false);

  renewing = (async () => {
    try {
      const res = await fetch(`${base}/api/v1/auth/refresh`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (!res.ok) return false;
      const body = await res.json();
      if (!body.access_token) return false;
      // Model 1 rotates the refresh token too, so the new one must replace the
      // old one or the *next* renewal fails with a token already spent.
      setToken(body.access_token, body.refresh_token ?? refresh);
      return true;
    } catch {
      return false;
    } finally {
      renewing = null;
    }
  })();
  return renewing;
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string, readonly body?: unknown) {
    super(message);
    this.name = "ApiError";
  }
}

function humanise(status: number, body: unknown): string {
  if (typeof body === "string" && body) return body;
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    // FastAPI validation: name the field, because "Input should be a valid list" on
    // its own has sent people looking in the wrong place.
    if (Array.isArray(b.detail)) {
      return b.detail
        .map((d) => {
          const e = d as { loc?: unknown[]; msg?: string };
          const where = Array.isArray(e.loc) ? e.loc.filter((p) => p !== "body").join(".") : "";
          return where ? `${where}: ${e.msg ?? "invalid"}` : (e.msg ?? "invalid");
        })
        .join("; ");
    }
    if (typeof b.detail === "string") return b.detail;
    if (typeof b.error === "string") return b.type ? `${b.error} (${b.type})` : b.error;
  }
  return `HTTP ${status}`;
}

export async function request<T>(
  path: string,
  init: RequestInit & { params?: Record<string, unknown> } = {},
): Promise<T> {
  const { params, ...rest } = init;
  let url = BASE + path;
  if (params) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      // Repeated keys, not comma-joined: every list filter in the API is a
      // `Query(default=None)` of `list[str]`, which reads repeats.
      if (Array.isArray(v)) v.forEach((item) => item !== "" && qs.append(k, String(item)));
      else qs.set(k, String(v));
    }
    const s = qs.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }

  const headers = new Headers(rest.headers);
  if (rest.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (bearer) headers.set("authorization", `Bearer ${bearer}`);

  const res = await fetch(url, { ...rest, headers });
  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let body: unknown = text;
  if (text && res.headers.get("content-type")?.includes("json")) {
    try {
      body = JSON.parse(text);
    } catch {
      /* keep the text; a proxy error page is not JSON and saying so is the point */
    }
  }
  // A 401 while holding a token means the access token has expired — Model 1
  // issues them with a fifteen-minute life. Left alone this is the worst kind of
  // failure: every panel quietly empties at once, the live dot goes to
  // "offline", and the operator is looking at a console that shows nothing and
  // explains nothing. Fifteen minutes is also shorter than a demo.
  //
  // So the refresh token is spent first and the request retried once. Only when
  // that fails is the session really over, and then the shell is told so the
  // sign-in screen comes back where an operator can act on it.
  //
  // Guarded on `bearer` so an unauthenticated 401 — the documented state before
  // anyone has signed in — does not fire the event and loop. `_retried` stops a
  // renewed token that is still refused from recursing.
  if (res.status === 401 && bearer) {
    if (!(init as { _retried?: boolean })._retried && await renew()) {
      return request<T>(path, { ...init, _retried: true } as typeof init);
    }
    setToken(null);
    window.dispatchEvent(new CustomEvent("setu:unauthorized"));
  }
  if (!res.ok) throw new ApiError(res.status, humanise(res.status, body), body);
  return body as T;
}

export const api = {
  get: <T>(path: string, params?: Record<string, unknown>, signal?: AbortSignal) =>
    request<T>(path, { params, signal }),
  post: <T>(path: string, body?: unknown, params?: Record<string, unknown>, signal?: AbortSignal) =>
    request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
      params,
      signal,
    }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
  del: <T>(path: string, params?: Record<string, unknown>) =>
    request<T>(path, { method: "DELETE", params }),
};
