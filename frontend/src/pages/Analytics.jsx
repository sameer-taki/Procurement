import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { money, num, relativeTime } from '../format.js'
import {
  canPushWarehouse, pct, pushResultRows, vendorSpendRows, warehouseStatusLabel,
} from '../analytics.js'

// Phase 5 — procurement analytics computed from canonical data + BC's reported
// match: spend (received_qty × unit_price), on-time-delivery, and an indicative
// stock-turn proxy. Read is open to any authed user; an ADMIN can push the
// figures to the Azure SQL warehouse (guarded — a no-op until AZURE_SQL_DSN is
// set). The app never fabricates money; spend reflects what was actually
// received against canonical PO lines.
export default function Analytics() {
  const { user, setUser } = useAuth()
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [pushResult, setPushResult] = useState(null)

  const load = useCallback(() => {
    api.get('/api/analytics')
      .then(setData)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [setUser])

  useEffect(load, [load])

  async function pushToWarehouse() {
    setBusy(true)
    setError('')
    setPushResult(null)
    try {
      setPushResult(await api.post('/api/analytics/push'))
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message) // 403 if not admin
    } finally {
      setBusy(false)
    }
  }

  if (error && !data) return <div className="error">{error}</div>
  if (!data) return <div className="muted">Loading analytics…</div>

  const spend = data.spend || {}
  const otd = data.on_time_delivery || {}
  const turn = data.stock_turn || {}
  const vendorRows = vendorSpendRows(spend.by_vendor || [], spend.total || 0)
  const canPush = canPushWarehouse(user)

  return (
    <div>
      <div className="page-head">
        <h1>Analytics</h1>
        <span className="muted">figures as of {relativeTime(data.as_of)}</span>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="tiles">
        <Tile label="Total spend (received)" value={money(spend.total || 0)} />
        <Tile
          label="On-time delivery"
          value={pct(otd.rate, otd.sample)}
        />
        <Tile
          label="Stock-turn (indicative)"
          value={turn.value != null ? num(turn.value) : '—'}
        />
        <Tile label="Vendors with spend" value={num(vendorRows.length)} />
      </div>

      <div className="grid-2">
        <section className="card">
          <h2>Spend by vendor</h2>
          {vendorRows.length === 0 ? (
            <p className="muted">No received spend yet — receive against a PO to populate this.</p>
          ) : (
            <table className="table">
              <thead>
                <tr><th>Vendor</th><th className="r">Spend</th><th>Share</th></tr>
              </thead>
              <tbody>
                {vendorRows.map((v) => (
                  <tr key={v.vendor}>
                    <td>{v.vendor}</td>
                    <td className="r">{money(v.amount)}</td>
                    <td><ShareBar share={v.share} /></td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <td className="r muted">Total</td>
                  <td className="r"><strong>{money(spend.total || 0)}</strong></td>
                  <td></td>
                </tr>
              </tfoot>
            </table>
          )}
          <p className="muted small">
            Spend = received quantity × the chosen vendor's unit price, from canonical PO lines and
            booked receipts. Money masters live in BC; this is the procurement view.
          </p>
        </section>

        <section className="card">
          <h2>Delivery & stock</h2>
          <table className="table">
            <tbody>
              <tr>
                <td>On-time delivery rate</td>
                <td className="r"><strong>{pct(otd.rate, otd.sample)}</strong></td>
              </tr>
              <tr>
                <td className="muted small">Received lines with a needed-by date measured</td>
                <td className="r muted small">{num(otd.sample || 0)}</td>
              </tr>
              <tr>
                <td>Stock-turn <span className="muted small">(indicative)</span></td>
                <td className="r"><strong>{turn.value != null ? num(turn.value) : '—'}</strong></td>
              </tr>
            </tbody>
          </table>
          {otd.rate != null && (otd.sample || 0) > 0 && (
            <ShareBar share={Number(otd.rate)} ok />
          )}
          <p className="muted small">
            On-time = received on or before the source requisition line's needed-by date.{' '}
            {turn.note || 'Stock-turn is an indicative proxy (allocated ÷ on-hand) across materials, not an accounting figure.'}
          </p>
        </section>
      </div>

      <section className="card">
        <h2>Warehouse</h2>
        <p className="muted small">
          Push these figures to the Azure SQL warehouse for Power BI. The writer is guarded — until
          AZURE_SQL_DSN is configured it reports <code>skipped:not-configured</code> and writes
          nothing.
        </p>
        {canPush ? (
          <div className="form-actions">
            <button className="btn btn-primary" onClick={pushToWarehouse} disabled={busy}>
              {busy ? 'Pushing…' : 'Push to warehouse'}
            </button>
          </div>
        ) : (
          <p className="muted small">Only an admin can push figures to the warehouse.</p>
        )}

        {pushResult && (
          <table className="table" style={{ marginTop: 12 }}>
            <thead>
              <tr><th>Table</th><th>Status</th></tr>
            </thead>
            <tbody>
              {pushResultRows(pushResult.warehouse).map((r) => (
                <tr key={r.table}>
                  <td>{r.table}</td>
                  <td>{r.label}</td>
                </tr>
              ))}
              {pushResultRows(pushResult.warehouse).length === 0 && (
                <tr><td colSpan="2" className="muted">{warehouseStatusLabel(pushResult.warehouse)}</td></tr>
              )}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}

// Plain-CSS share bar (no chart library — CSP/dep constraints). `share` is a
// 0..1 fraction.
function ShareBar({ share, ok }) {
  const w = Math.max(0, Math.min(1, Number(share || 0))) * 100
  return (
    <div className="bar-track" title={`${Math.round(w)}%`}>
      <div className={`bar-fill ${ok ? 'bar-ok' : ''}`} style={{ width: `${w}%` }} />
    </div>
  )
}

function Tile({ label, value }) {
  return (
    <div className="tile">
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  )
}
