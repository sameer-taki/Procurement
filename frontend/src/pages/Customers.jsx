import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'

// The customer master, synced read-only from Business Central (BC owns it, same
// as the item master). This is the reference list the forecast picker draws
// from; it is not editable here — new customers appear once BC syncs them.
export default function Customers() {
  const { setUser } = useAuth()
  const [q, setQ] = useState('')
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    const qs = q.trim() ? `?q=${encodeURIComponent(q.trim())}&limit=200` : '?limit=200'
    api.get(`/api/customers${qs}`)
      .then(setRows)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [q, setUser])

  // Debounce the text filter (same 200 ms pattern as the stock search).
  useEffect(() => {
    const handle = setTimeout(load, 200)
    return () => clearTimeout(handle)
  }, [load])

  return (
    <div>
      <div className="page-head">
        <h1>Customers</h1>
        <span className="muted">customer master — synced from Business Central</span>
      </div>

      <div className="filters">
        <input
          className="input"
          style={{ width: 320 }}
          placeholder="Search by name…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      {error && <div className="error">{error}</div>}

      <table className="table">
        <thead>
          <tr>
            <th>Name</th><th>BC customer no.</th><th>Email</th>
          </tr>
        </thead>
        <tbody>
          {(rows || []).map((c) => (
            <tr key={c.bc_customer_no || c.name}>
              <td>{c.name}</td>
              <td className="nowrap">{c.bc_customer_no || '—'}</td>
              <td>{c.email || <span className="muted small">—</span>}</td>
            </tr>
          ))}
          {rows && rows.length === 0 && (
            <tr><td colSpan="3" className="muted center-cell">No customers match this search.</td></tr>
          )}
        </tbody>
      </table>
      <p className="muted small">
        Customers are owned by Business Central and refreshed automatically; they can't be edited
        here. This is the list the forecast customer picker draws from.
      </p>
    </div>
  )
}
