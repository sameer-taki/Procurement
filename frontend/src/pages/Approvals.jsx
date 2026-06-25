import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { money, num, relativeTime } from '../format.js'
import { statusBadge } from '../requisitions.js'

export default function Approvals() {
  const { setUser } = useAuth()
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('') // `${id}:${action}`

  const load = useCallback(() => {
    api.get('/api/approvals')
      .then(setRows)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [setUser])

  useEffect(load, [load])

  async function act(id, action, body) {
    setBusy(`${id}:${action}`)
    setError('')
    try {
      await api.post(`/api/requisitions/${id}/${action}`, body)
      load()
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message)
    } finally {
      setBusy('')
    }
  }

  function reject(id) {
    const reason = window.prompt('Reason for rejection?')
    if (reason == null) return
    act(id, 'reject', { reason })
  }

  return (
    <div>
      <div className="page-head">
        <h1>Approvals</h1>
        <span className="muted">waiting on me</span>
      </div>

      {error && <div className="error">{error}</div>}

      <table className="table">
        <thead>
          <tr>
            <th>Number</th><th>Requester</th><th>Status</th>
            <th className="r">Lines</th><th className="r">Estimated</th>
            <th>Created</th><th></th>
          </tr>
        </thead>
        <tbody>
          {(rows || []).map((r) => (
            <tr key={r.id}>
              <td><Link to={`/requisitions/${r.id}`}>{r.number}</Link></td>
              <td>{r.requester}</td>
              <td><span className={`badge ${statusBadge(r.status)}`}>{r.status.replace('_', ' ')}</span></td>
              <td className="r">{num(r.line_count)}</td>
              <td className="r">{money(r.estimated_amount)} <span className="muted small">est.</span></td>
              <td className="muted small">{relativeTime(r.created_at)}</td>
              <td className="r nowrap">
                <button
                  className="btn btn-primary small-btn"
                  disabled={!!busy}
                  onClick={() => act(r.id, 'approve')}
                >
                  {busy === `${r.id}:approve` ? 'Approving…' : 'Approve'}
                </button>{' '}
                <button
                  className="btn small-btn"
                  disabled={!!busy}
                  onClick={() => reject(r.id)}
                >
                  {busy === `${r.id}:reject` ? 'Rejecting…' : 'Reject'}
                </button>
              </td>
            </tr>
          ))}
          {rows && rows.length === 0 && (
            <tr><td colSpan="7" className="muted center-cell">Nothing is waiting on your approval.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
