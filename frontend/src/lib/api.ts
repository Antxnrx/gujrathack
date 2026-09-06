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

export function setToken(token: string | null): void {
  bearer = token;
  if (token) localStorage.setItem("setu.token", token);
  else localStorage.removeItem("setu.token");
}

export function loadToken(): void {
  bearer = localStorage.getItem("setu.token");
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
