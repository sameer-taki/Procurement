import React, { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { money, num, relativeTime } from '../format.js'
import { availablePOActions, poStatusBadge } from '../purchaseOrders.js'
import {
  buildReceivePayload, canShowReceiveForm, hasReceiptLines, lineReceiptRows,
  matchBadge, validateReceipt,
} from '../receiving.js'

// How a PO's email-notify status (returned by the backend) reads in the UI.
function emailLabel(status) {
  if (!status) return 'not sent yet'
  if (status === 'sent') return 'sent'
  if (status === 'skipped:not-configured') return 'skipped — Graph not configured'
  if (status.startsWith('error:')) return `failed — ${status.slice('error:'.length)}`
  return status
}

export default function PurchaseOrderDetail() {
  const { id } = useParams()
  const { user, setUser } = useAuth()
  const [po, setPo] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(() => {
    api.get(`/api/purchase-orders/${id}`)
      .then(setPo)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [id, setUser])

  useEffect(load, [load])

  async function act(action, path) {
    setBusy(action)
    setError('')
    try {
      await api.post(path)
      load()
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message)
    } finally {
      setBusy('')
    }
  }

  if (error && !po) return <div className="error">{error}</div>
  if (!po) return <div className="muted">Loading purchase order…</div>

  const actions = availablePOActions(user, po.status)
  const anyAction = actions.issue || actions.outbox
  const rcptRows = lineReceiptRows(po.lines)

  return (
    <div>
      <div className="page-head">
        <div>
          <Link to="/purchase-orders" className="back">← Purchase orders</Link>
          <h1>
            {po.number}{' '}
            <span className={`badge ${poStatusBadge(po.status)}`}>{po.status.replace(/_/g, ' ')}</span>
          </h1>
        </div>
        {anyAction && (
          <div className="form-actions">
            {actions.issue && (
              <button className="btn btn-primary" disabled={!!busy} onClick={() => act('issue', `/api/purchase-orders/${id}/issue`)}>
                {busy === 'issue' ? 'Issuing…' : 'Issue & post to BC'}
              </button>
            )}
            {actions.outbox && (
              <button className="btn" disabled={!!busy} onClick={() => act('outbox', '/api/outbox/process')}>
                {busy === 'outbox' ? 'Processing…' : 'Process outbox'}
              </button>
            )}
          </div>
        )}
      </div>

      {error && <div className="error">{error}</div>}

      <div className="meta-row">
        <Meta label="Vendor" value={po.vendor?.name || '—'} />
        <Meta label="Vendor email" value={po.vendor?.email || <span className="muted">—</span>} />
        <Meta
          label="Source requisition"
          value={po.requisition_id
            ? <Link to={`/requisitions/${po.requisition_id}`}>{po.requisition_number || po.requisition_id}</Link>
            : '—'}
        />
        <Meta label="BC PO no" value={po.bc_po_no || <span className="muted">not posted</span>} />
        <Meta label="Vendor email status" value={emailLabel(po.email_status)} />
        <Meta
          label="3-way match"
          value={po.match_status
            ? <span className={`badge ${matchBadge(po.match_status)}`}>{po.match_status}</span>
            : <span className="muted">pending</span>}
        />
        <Meta label="Total" value={<strong>{money(po.total)}</strong>} />
        <Meta label="Created" value={relativeTime(po.created_at)} />
      </div>

      <section className="card">
        <h2>Lines</h2>
        <table className="table">
          <thead>
            <tr>
              <th>SKU</th><th>Material</th>
              <th className="r">Ordered</th><th className="r">Received</th>
              <th className="r">Unit price</th><th className="r">Line total</th>
            </tr>
          </thead>
          <tbody>
            {(po.lines || []).map((l, i) => {
              const r = rcptRows[i]
              return (
                <tr key={i}>
                  <td><Link to={`/stock/${l.sku}`}>{l.sku}</Link></td>
                  <td>{l.name}</td>
                  <td className="r">{num(l.quantity)}</td>
                  <td className={`r ${r.fully_received ? 'ok-text' : (r.received > 0 ? 'warn' : 'muted')}`}>
                    {num(r.received)}{r.outstanding > 0 ? <span className="muted small"> / {num(r.outstanding)} due</span> : null}
                  </td>
                  <td className="r">{l.unit_price != null ? money(l.unit_price) : '—'}</td>
                  <td className="r">{money(l.line_total)}</td>
                </tr>
              )
            })}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan="5" className="r muted">Total</td>
              <td className="r"><strong>{money(po.total)}</strong></td>
            </tr>
          </tfoot>
        </table>
        <p className="muted small">
          Unit prices are the chosen vendor's buying price (cheapest vendor per material);
          order quantity is rounded up to the vendor MOQ.
        </p>
      </section>

      <ReceivingSection po={po} rows={rcptRows} id={id} user={user} setUser={setUser} reload={load} />

      <section className="card">
        <h2>History</h2>
        {(!po.events || po.events.length === 0) ? (
          <p className="muted">No events recorded yet.</p>
        ) : (
          <ul className="timeline">
            {po.events.map((ev, i) => (
              <li key={i} className="timeline-item">
                <div className="timeline-dot" />
                <div className="timeline-body">
                  <div>
                    <strong>{ev.event_type}</strong>
                    {ev.from_status && (
                      <span className="muted small">
                        {' '}· {ev.from_status.replace(/_/g, ' ')} → {ev.to_status?.replace(/_/g, ' ')}
                      </span>
                    )}
                  </div>
                  <div className="muted small">
                    {ev.actor} · {relativeTime(ev.occurred_at)}
                  </div>
                  {ev.detail && <div className="small">{renderDetail(ev.detail)}</div>}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

// Receiving (Phase 5): GRN capture against the PO, the receipts already booked,
// and the BC 3-way match. The receive form only shows for OFFICER/ADMIN while
// the PO is in a receivable state — the backend still enforces RBAC (403),
// state (409) and over-receipt (400).
function ReceivingSection({ po, rows, id, user, setUser, reload }) {
  const [quantities, setQuantities] = useState({}) // {po_line_id: qty-string}
  const [grnNo, setGrnNo] = useState('')
  const [location, setLocation] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const showForm = canShowReceiveForm(user, po.status)
  const receipts = po.receipts || []

  function setQty(lineId, value) {
    setQuantities((prev) => ({ ...prev, [lineId]: value }))
  }

  async function receive(e) {
    e.preventDefault()
    setError('')
    const payload = buildReceivePayload({ quantities, grnNo, location })
    const validationError = validateReceipt(payload, rows)
    if (validationError) { setError(validationError); return }
    setBusy(true)
    try {
      await api.post(`/api/purchase-orders/${id}/receive`, payload)
      setQuantities({})
      setGrnNo('')
      setLocation('')
      reload()
    } catch (e2) {
      if (e2.status === 401) setUser(null)
      else setError(e2.message) // 409 bad state / 400 over-receipt surface here
    } finally {
      setBusy(false)
    }
  }

  const draft = buildReceivePayload({ quantities, grnNo, location })

  return (
    <section className="card">
      <h2>
        Receiving{' '}
        <span className="muted small">
          {receipts.length} GRN{receipts.length === 1 ? '' : 's'} booked
        </span>
      </h2>

      {showForm ? (
        <form onSubmit={receive}>
          <table className="table">
            <thead>
              <tr>
                <th>SKU</th><th>Material</th>
                <th className="r">Ordered</th><th className="r">Received</th>
                <th className="r">Outstanding</th><th className="r">Receive now</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.sku}</td>
                  <td>{r.name}</td>
                  <td className="r">{num(r.ordered)}</td>
                  <td className="r">{num(r.received)}</td>
                  <td className="r">{num(r.outstanding)}</td>
                  <td className="r">
                    <input
                      className="input qty"
                      type="number" min="0" step="any"
                      max={r.outstanding}
                      disabled={r.fully_received}
                      value={quantities[r.id] ?? ''}
                      placeholder={r.fully_received ? 'done' : '0'}
                      onChange={(ev) => setQty(r.id, ev.target.value)}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="form-row" style={{ marginTop: 12 }}>
            <label className="field">
              <span className="field-label">GRN no <span className="muted">(optional — auto if blank)</span></span>
              <input className="input" value={grnNo} onChange={(e) => setGrnNo(e.target.value)} placeholder="GRN-YYYYMMDD-…" />
            </label>
            <label className="field">
              <span className="field-label">Location <span className="muted">(optional)</span></span>
              <input className="input" value={location} onChange={(e) => setLocation(e.target.value)} placeholder="e.g. MAIN" />
            </label>
          </div>

          {error && <div className="error">{error}</div>}

          <div className="form-actions">
            <button className="btn btn-primary" type="submit" disabled={busy || !hasReceiptLines(draft)}>
              {busy ? 'Receiving…' : 'Receive goods'}
            </button>
          </div>
          <p className="muted small">
            One GRN books several lines at once and posts a receipt to Business Central via the
            outbox. Stock is then re-read from Kiwiplan/Accura (they own the on-hand increment) — in
            demo the source figures are static, so quantities won't visibly change.
          </p>
        </form>
      ) : (
        <p className="muted">
          {po.status === 'RECEIVED' || po.status === 'MATCHED'
            ? 'Fully received.'
            : 'No goods can be received against this purchase order in its current state.'}
        </p>
      )}

      {receipts.length > 0 && (
        <table className="table" style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th>GRN</th><th>BC GRN ref</th><th>SKU</th>
              <th className="r">Qty</th><th>Location</th><th>Match</th><th>Received</th>
            </tr>
          </thead>
          <tbody>
            {receipts.map((rc, i) => (
              <tr key={i}>
                <td>{rc.grn_no}</td>
                <td>{rc.bc_grn_no || <span className="muted small">pending post</span>}</td>
                <td>{rc.sku || '—'}</td>
                <td className="r">{num(rc.quantity)}</td>
                <td>{rc.location || <span className="muted small">—</span>}</td>
                <td>
                  {rc.match_status
                    ? <span className={`badge ${matchBadge(rc.match_status)}`}>{rc.match_status}</span>
                    : (po.match_status === 'MATCHED'
                      ? <span className={`badge ${matchBadge('MATCHED')}`}>MATCHED</span>
                      : <span className="muted small">—</span>)}
                </td>
                <td className="muted small">{relativeTime(rc.received_at || rc.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p className="muted small">
        Business Central owns the 3-way match (PO · GRN · invoice). This app only reflects the match
        state BC reports; it never fabricates money.
      </p>
    </section>
  )
}

function renderDetail(detail) {
  if (detail == null) return null
  if (typeof detail === 'string') return detail
  if (typeof detail === 'object') {
    if (detail.reason) return `Reason: ${detail.reason}`
    return Object.entries(detail).map(([k, v]) => `${k}: ${v}`).join(' · ')
  }
  return String(detail)
}

function Meta({ label, value }) {
  return (
    <div className="meta">
      <div className="meta-label">{label}</div>
      <div className="meta-value">{value}</div>
    </div>
  )
}
