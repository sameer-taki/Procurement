import React, { useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'

export default function Login() {
  const { providers, refresh } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await api.post('/auth/admin-login', { username, password })
      await refresh()
    } catch (err) {
      setError(err.status === 401 ? 'Invalid credentials' : err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="card login">
        <div className="brand big"><span className="logo">◆</span> Golden Procurement</div>
        <p className="muted">Stock · requisitions · purchasing · paper planning</p>

        {providers.entra && (
          <a className="btn btn-primary block" href="/auth/login">
            Sign in with Microsoft
          </a>
        )}
        {providers.entra && <div className="divider">or admin sign-in</div>}

        <form onSubmit={submit}>
          <label>Username</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
          <label>Password</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          {error && <div className="error">{error}</div>}
          <button className="btn btn-primary block" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
