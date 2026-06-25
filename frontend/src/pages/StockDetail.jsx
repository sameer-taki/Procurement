import React, { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { money, num, relativeTime } from '../format.js'

export default function StockDetail() {
  const { sku } = useParams()
  const { setUser } = useAuth()
  const [v, setV] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.get(`/api/stock/${encodeURIComponent(sku)}`)
      .then(setV)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [sku, setUser])

  useEffect(load, [load])

  async function refresh() {
    setBusy(true)
    setError('')
    try {
      setV(await api.post(`/api/stock/${encodeURIComponent(sku)}/refresh`))
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (error) return <div className="error">{error}</div>
  if (!v) return <div className="muted">Loading {sku}…</div>

  return (
    <div>
      <div className="page-head">
        <div>
          <Link to="/stock" className="back">← Stock</Link>
          <h1>{v.sku} <span className="muted thin">{v.name}</span></h1>
        </div>
        <button className="btn" onClick={refresh} disabled={busy}>
          {busy ? 'Refreshing…' : 'Refresh this material'}
        </button>
      </div>

      <div className="meta-row">
        <Meta label="Type" value={v.item_type} />
        <Meta label="UoM" value={v.uom} />
        <Meta label="Reorder point" value={v.reorder_point != null ? num(v.reorder_point) : '—'} />
        <Meta label="Lead time" value={v.lead_time_days != null ? `${v.lead_time_days} d` : '—'} />
        <Meta label="Price (BC)" value={v.price ? money(v.price.unit_price, v.price.currency) : '—'} />
        <Meta label="Stock as of" value={relativeTime(v.as_of)} />
      </div>

      <div className="tiles">
        <Tile label="On hand" value={num(v.totals.on_hand)} />
        <Tile label="Allocated" value={num(v.totals.allocated)} />
        <Tile label="On order" value={num(v.totals.on_order)} />
        <Tile label="Available" value={num(v.totals.available)} warn={v.below_reorder} />
      </div>
      {v.below_reorder && <div className="banner warn">Available is below the reorder point.</div>}

      {v.by_system.map((sys) => (
        <section className="card" key={sys.system}>
          <h2>
            {sys.system} <span className={`badge ${sys.mode}`}>{sys.mode}</span>
          </h2>
          <table className="table">
            <thead>
              <tr>
                <th>Location</th><th className="r">On hand</th><th className="r">Allocated</th>
                <th className="r">On order</th><th className="r">Available</th><th>As of</th>
              </tr>
            </thead>
            <tbody>
              {sys.rows.map((r, i) => (
                <tr key={i}>
                  <td>{r.location || '—'}</td>
                  <td className="r">{num(r.on_hand)}</td>
                  <td className="r">{num(r.allocated)}</td>
                  <td className="r">{num(r.on_order)}</td>
                  <td className="r">{num(r.available)}</td>
                  <td className="muted small">{relativeTime(r.as_of)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  )
}

function Meta({ label, value }) {
  return (
    <div className="meta">
      <div className="meta-label">{label}</div>
      <div className="meta-value">{value}</div>
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
