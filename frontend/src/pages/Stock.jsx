import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { num, relativeTime } from '../format.js'

export default function Stock() {
  const { setUser } = useAuth()
  const [q, setQ] = useState('')
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const handle = setTimeout(() => {
      api.get(`/api/stock?q=${encodeURIComponent(q)}`)
        .then(setData)
        .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
    }, 200)
    return () => clearTimeout(handle)
  }, [q, setUser])

  const results = data?.results || []
  return (
    <div>
      <div className="page-head">
        <h1>Stock</h1>
      </div>
      <input
        className="search"
        placeholder="Search any SKU or material name…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        autoFocus
      />
      {error && <div className="error">{error}</div>}

      <table className="table">
        <thead>
          <tr>
            <th>SKU</th><th>Material</th><th>Type</th>
            <th className="r">On hand</th><th className="r">Available</th>
            <th>Systems</th><th>As of</th>
          </tr>
        </thead>
        <tbody>
          {results.map((r) => (
            <tr key={r.sku} className={r.below_reorder ? 'row-warn' : ''}>
              <td><Link to={`/stock/${r.sku}`}>{r.sku}</Link></td>
              <td>{r.name}</td>
              <td className="muted">{r.item_type}</td>
              <td className="r">{num(r.totals.on_hand)} <span className="muted small">{r.uom}</span></td>
              <td className={`r ${r.below_reorder ? 'warn' : ''}`}>{num(r.totals.available)}</td>
              <td>{r.systems.map((s) => <span key={s} className="chip">{s}</span>)}</td>
              <td className="muted small">{relativeTime(r.as_of)}</td>
            </tr>
          ))}
          {data && results.length === 0 && (
            <tr><td colSpan="7" className="muted center-cell">No materials match “{q}”.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
