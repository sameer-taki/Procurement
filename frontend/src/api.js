// Fetch helpers. Two deployment shapes are supported by one code path:
//   • same-origin — FastAPI self-serves the SPA, or Vercel rewrites proxy
//     /api + /auth to the backend. VITE_API_BASE_URL is empty; relative paths.
//   • cross-origin — the Vercel frontend calls the backend directly
//     (VITE_API_BASE_URL=https://mcp.golden.com.fj); the backend's CORS allows it.
// Auth: when a token provider is registered (Clerk), every request carries the
// Clerk session JWT as a Bearer header. credentials:'include' keeps the
// break-glass admin session cookie working on same-origin.
const BASE = import.meta.env.VITE_API_BASE_URL || ''

let tokenProvider = null

// auth.jsx registers this so api calls can attach the current Clerk token.
// Pass null to clear it (local/break-glass mode).
export function setTokenProvider(fn) {
  tokenProvider = fn
}

async function request(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  if (tokenProvider) {
    try {
      const token = await tokenProvider()
      if (token) headers['Authorization'] = `Bearer ${token}`
    } catch (_) { /* fall through unauthenticated → 401 handled by callers */ }
  }
  // headers last so our merged/auth headers win over any in opts.
  const res = await fetch(BASE + path, { credentials: 'include', ...opts, headers })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail || detail
    } catch (_) { /* non-JSON error body */ }
    const err = new Error(detail)
    err.status = res.status
    throw err
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  get: (path) => request(path),
  post: (path, body) =>
    request(path, { method: 'POST', body: body != null ? JSON.stringify(body) : undefined }),
  put: (path, body) =>
    request(path, { method: 'PUT', body: body != null ? JSON.stringify(body) : undefined }),
  patch: (path, body) =>
    request(path, { method: 'PATCH', body: body != null ? JSON.stringify(body) : undefined }),
  del: (path) => request(path, { method: 'DELETE' }),
}
