import React, { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { num, relativeTime } from '../format.js'
import {
  basisLabel, belowCover, canCreateSuggestion, canPlanPaper, coverBadge,
  fmtTonnes, latestAsOf, monthLabel, nextArrival,
} from '../paperPlanning.js'

// The GML procurement SOP "Order Page": every paper grade with its usage basis
// (forecast explosion or trailing history), months of stock, and the suggested
// order that brings it back to the cover target — plus per-vendor container
// plans (orders rounded up to whole 40ft FCLs). The backend owns all of the
// maths; officer/admin can import BC usage or turn the plan into ONE draft
// coverage requisition that flows into the Phase 2 approval lifecycle.
export default function PaperPlanning() {
  const { user, setUser } = useAuth()
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(() => {
    api.get('/api/planning/order-page')
      .then(setData)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [setUser])

  useEffect(load, [load])

  async function importUsage() {
    setBusy('import')
    setError('')
    setNotice('')
    try {
      const res = await api.post('/api/planning/import-usage')
      setNotice(`Usage imported from BC — ${num(res.imported)} rows imported, ${num(res.skipped)} skipped.`)
      load()
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message) // 403 for non-officers surfaces here
    } finally {
      setBusy('')
    }
  }

  async function createRequisition() {
    setBusy('suggest')
    setError('')
    setNotice('')
    try {
      const res = await api.post('/api/planning/suggest-orders', {})
      if (res && res.created === false) {
        setNotice(res.message || 'All grades at or above cover — nothing to order.')
      } else if (res && res.id) {
        navigate(`/requisitions/${res.id}`)
      }
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message)
    } finally {
      setBusy('')
    }
  }

  if (error && !data) return <div className="error">{error}</div>
  if (!data) return <div className="muted">Loading order page…</div>

  const rows = data.rows || []
  const plans = data.container_plans || []
  const skipped = data.skipped_forecasts || []
  const asOf = latestAsOf(rows)
  const eta = nextArrival(rows)
  const window = data.window || []
  const canPlan = canPlanPaper(user)
  const suggestGate = canCreateSuggestion(data)
  const openReq = data.open_coverage_requisition

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Order Page</h1>
          <span className="muted">
            paper coverage {window.length > 0 && (
              <>· {monthLabel(window[0])} – {monthLabel(window[window.length - 1])} </>
            )}· stock as of {relativeTime(asOf)}
          </span>
        </div>
        {canPlan && (
          <div className="form-actions">
            <button className="btn" onClick={importUsage} disabled={!!busy}>
              {busy === 'import' ? 'Importing…' : 'Import usage from BC'}
            </button>
            <button
              className="btn btn-primary"
              onClick={createRequisition}
              disabled={!!busy || !suggestGate.enabled}
              title={suggestGate.reason || undefined}
            >
              {busy === 'suggest' ? 'Creating…' : 'Create suggested requisition'}
            </button>
          </div>
        )}
      </div>

      {error && <div className="error">{error}</div>}
      {notice && <div className="banner">{notice}</div>}
      {openReq && (
        <div className="banner">
          Coverage requisition{' '}
          <Link to={`/requisitions/${openReq.id}`}>{openReq.number}</Link>{' '}
          is still in flight ({openReq.status}) — its volume is already spoken for;
          action it before planning another order.
        </div>
      )}
      {skipped.length > 0 && (
        <div className="banner warn">
          Forecasts skipped (no BOM to explode): {skipped.join(', ')} — paper usage for these
          finished goods falls back to history.
        </div>
      )}

      <div className="tiles">
        <Tile label="Grades tracked" value={num(rows.length)} />
        <Tile
          label={`Below ${data.cover_months}-month cover`}
          value={num(data.below_cover)}
          warn={data.below_cover > 0}
        />
        <Tile label="Next arrival ETA" value={eta || '—'} />
        <Tile label="Cover target" value={`${num(data.cover_months)} mo`} />
      </div>

      <table className="table">
        <thead>
          <tr>
            <th>SKU</th><th>Grade</th><th className="r">Deckle</th><th>Basis</th>
            <th className="r">Monthly use</th><th className="r">3-mo need</th>
            <th className="r">On hand</th><th className="r">Allocated</th>
            <th className="r">In transit</th><th className="r">Months of stock</th>
            <th className="r">Suggested order</th><th>Vendor</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.sku} className={belowCover(r.months_of_stock, data.cover_months) ? 'row-warn' : ''}>
              <td><Link to={`/stock/${r.sku}`}>{r.sku}</Link></td>
              <td>{r.grade || '—'}</td>
              <td className="r nowrap">{r.deckle_mm != null ? <>{num(r.deckle_mm)} <span className="muted small">mm</span></> : '—'}</td>
              <td><span className="chip" title={
                r.monthly_forecast != null || r.monthly_history != null
                  ? `forecast ${r.monthly_forecast != null ? num(r.monthly_forecast) : '—'} / history ${r.monthly_history != null ? num(r.monthly_history) : '—'} kg/mo`
                  : undefined
              }>{basisLabel(r, data.cover_months)}</span></td>
              <td className="r">{num(r.monthly_usage)}</td>
              <td className="r">{num(r.usage_3mo)}</td>
              <td className="r">{num(r.on_hand)}</td>
              <td className="r">{num(r.allocated)}</td>
              <td className="r">
                {num(r.in_transit)}
                {r.next_eta && <div className="muted small nowrap">eta {r.next_eta}</div>}
              </td>
              <td className="r">
                <span className={`badge ${coverBadge(r.months_of_stock, data.cover_months)}`}>
                  {r.months_of_stock != null ? num(r.months_of_stock) : '—'}
                </span>
              </td>
              <td className="r">
                {r.requirement_kg > 0
                  ? <strong>{fmtTonnes(r.requirement_kg)}</strong>
                  : <span className="muted">{fmtTonnes(r.requirement_kg || 0)}</span>}
              </td>
              <td>
                {r.vendor || <span className="muted small">no vendor</span>}
                {r.lead_time_days != null && <span className="muted small"> · {num(r.lead_time_days)} d</span>}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan="12" className="muted center-cell">No paper grades tracked yet.</td></tr>
          )}
        </tbody>
      </table>
      <p className="muted small">
        Quantities in kg; suggested orders in tonnes. Basis: FORECAST = customer forecasts exploded
        through the BOM; HISTORY = mean of the last 3 months' BC usage; NONE = no basis, coverage
        unknown. Suggested order = {num(data.cover_months)} months of use + allocated − on hand −
        in transit, before FCL rounding.
      </p>

      {plans.length > 0 && (
        <>
          <div className="page-head" style={{ marginTop: 8 }}>
            <h1 style={{ fontSize: 18 }}>Container plans</h1>
            <span className="muted small">orders rounded up to whole 40 ft FCLs ({fmtTonnes(data.kg_per_fcl)} each), slack topped onto the largest line</span>
          </div>
          <div className="grid-2">
            {plans.map((p) => (
              <section className="card" key={p.vendor_id ?? p.vendor ?? 'unpriced'}>
                <h2>
                  {p.vendor || <span className="muted">No vendor priced</span>}{' '}
                  <span className="muted small">
                    {num(p.containers)} × 40 ft FCL · {fmtTonnes(p.total_kg)}
                  </span>
                </h2>
                <table className="table">
                  <thead>
                    <tr><th>SKU</th><th className="r">Requirement</th><th className="r">Order</th></tr>
                  </thead>
                  <tbody>
                    {(p.lines || []).map((l) => (
                      <tr key={l.sku}>
                        <td><Link to={`/stock/${l.sku}`}>{l.sku}</Link></td>
                        <td className="r">{fmtTonnes(l.requirement_kg)}</td>
                        <td className="r"><strong>{fmtTonnes(l.order_kg)}</strong></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            ))}
          </div>
        </>
      )}

      {!canPlan && (
        <p className="muted small">
          Only an officer or admin can import usage or raise the suggested coverage requisition.
        </p>
      )}
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
