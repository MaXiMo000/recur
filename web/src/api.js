/* One place that knows how to talk to the API.
 *
 * `credentials: "include"` on every call is what carries the session cookie.
 * The token is httpOnly, so this file cannot read it and neither can anything
 * else that gets injected onto the page -- which is the reason it is a cookie
 * and not localStorage. */

/* Empty by default: the API is served from the same origin as this app, so
 * requests are relative. That is not a convenience -- a cross-origin setup
 * needs SameSite=None cookies, which removes the CSRF protection SameSite
 * exists to provide. Vite proxies /api to the local API in development. */
const BASE = import.meta.env.VITE_API_URL || "";

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function request(path, { method = "GET", body, form } = {}) {
  const init = { method, credentials: "include", headers: {} };
  if (form) {
    init.body = form; // let the browser set the multipart boundary
  } else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(`${BASE}${path}`, init);
  } catch {
    throw new ApiError(0, "Can't reach the server.");
  }

  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }

  if (!res.ok) {
    // FastAPI puts validation errors in a list; a raw [object Object] helps
    // nobody, so flatten it to the first readable message.
    let detail = data?.detail;
    if (Array.isArray(detail)) detail = detail[0]?.msg || "That input isn't valid.";
    throw new ApiError(res.status, detail || `Request failed (${res.status}).`);
  }
  return data;
}

export const api = {
  register: (email, password) =>
    request("/api/auth/register", { method: "POST", body: { email, password } }),
  verify: (token) => request("/api/auth/verify", { method: "POST", body: { token } }),
  login: (email, password) =>
    request("/api/auth/login", { method: "POST", body: { email, password } }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  forgot: (email) => request("/api/auth/forgot", { method: "POST", body: { email } }),
  reset: (token, password) =>
    request("/api/auth/reset", { method: "POST", body: { token, password } }),

  me: () => request("/api/me"),
  deleteAccount: () => request("/api/me", { method: "DELETE" }),
  exportAll: () => request("/api/export"),

  summary: () => request("/api/summary"),
  subscriptions: () => request("/api/subscriptions"),
  upcoming: (days = 30) => request(`/api/upcoming?days=${days}`),
  increases: () => request("/api/increases"),
  history: (id) => request(`/api/history/${id}`),

  reviewQueue: () => request("/api/review-queue"),
  resolveQueueItem: (queueId, merchant, ignore = false) =>
    request("/api/review-queue/resolve", {
      method: "POST",
      body: { queue_id: queueId, merchant, ignore },
    }),

  upload: (file, { account = "card", dayfirst = false, currency = "USD" } = {}) => {
    const form = new FormData();
    form.append("file", file);
    form.append("account", account);
    form.append("dayfirst", String(dayfirst));
    form.append("currency", currency);
    return request("/api/upload", { method: "POST", form });
  },
};
