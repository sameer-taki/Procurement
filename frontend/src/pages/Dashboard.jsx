import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { num, relativeTime } from '../format.js'

export default function Dashboard() {
  const { setUser } = useAuth()
  const [data, setData] = useState(null)
  const [paper, setPaper] = useState(null) // planning summary; optional
  const [error, setError] = useState('')

  useEffect(() => {
    api.get('/api/dashboard')
      .then(setData)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
    // Paper coverage tile — the slim summary endpoint (below_cover +
    // cover_months) instead of the full order-page; degrade gracefully (no
    // tile) if the call fails.
    api.get('/api/planning/summary')
      .then(setPaper)
      .catch(() => {})
  }, [setUser])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="muted">Loading dashboard…</div>

  const c = data.counts
  return (
    <div>
      <div className="page-head">
        <h1>Dashboard</h1>
        <span className="muted">stock as of {relativeTime(data.as_of)}</span>
      </div>

      <div className={`tiles ${paper ? 'tiles-5' : ''}`}>
        <Tile label="Items" value={num(c.items)} />
        <Tile label="Materials" value={num(c.materials)} />
        <Tile label="Stock locations" value={num(c.tracked_locations)} />
        <Tile label="Below reorder" value={num(c.below_reorder)} warn={c.below_reorder > 0} />
        {paper && (
          <Tile
            label={`Paper below ${paper.cover_months ?? 3}-mo cover`}
            value={num(paper.below_cover)}
            warn={paper.below_cover > 0}
          />
        )}
      </div>

      <div className="grid-2">
        <section className="card">
          <h2>Needs attention</h2>
          {data.low_stock.length === 0 ? (
            <p className="muted">Nothing below reorder point.</p>
          ) : (
            <table className="table">
              <thead>
                <tr><th>SKU</th><th>Material</th><th className="r">Available</th><th className="r">Reorder pt</th></tr>
              </thead>
              <tbody>
                {data.low_stock.map((x) => (
                  <tr key={x.sku}>
                    <td><Link to={`/stock/${x.sku}`}>{x.sku}</Link></td>
                    <td>{x.name}</td>
                    <td className="r warn">{num(x.available)}</td>
                    <td className="r">{num(x.reorder_point)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card">
          <h2>Source systems</h2>
          <table className="table">
            <thead><tr><th>System</th><th>Mode</th><th>Configured</th></tr></thead>
            <tbody>
              {data.systems.map((s) => (
                <tr key={s.system}>
                  <td>{s.system}</td>
                  <td><span className={`badge ${s.mode}`}>{s.mode}</span></td>
                  <td>{s.configured ? 'yes' : 'no'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            “demo” = source not yet wired; showing placeholder data until BC / Kiwiplan / Accura
            credentials are set.
          </p>
        </section>
      </div>
    </div>
  )
}

function Tile({ label, value, warn }) {
  return (
    <div className={`tile ${warn ? 'tile-warn' : ''}`}>
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  )
}
