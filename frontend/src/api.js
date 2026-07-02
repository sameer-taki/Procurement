// Same-origin fetch helpers. FastAPI serves this SPA in production and the Vite
// dev server proxies /api + /auth, so relative paths work in both.
async function request(path, opts = {}) {
  const res = await fetch(path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
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
