import React, { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { money, num, relativeTime } from '../format.js'
import { availablePOActions, poStatusBadge } from '../purchaseOrders.js'

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
        <Meta label="Total" value={<strong>{money(po.total)}</strong>} />
        <Meta label="Created" value={relativeTime(po.created_at)} />
      </div>

      <section className="card">
        <h2>Lines</h2>
        <table className="table">
          <thead>
            <tr>
              <th>SKU</th><th>Material</th>
              <th className="r">Qty</th><th className="r">Unit price</th><th className="r">Line total</th>
            </tr>
          </thead>
          <tbody>
            {(po.lines || []).map((l, i) => (
              <tr key={i}>
                <td><Link to={`/stock/${l.sku}`}>{l.sku}</Link></td>
                <td>{l.name}</td>
                <td className="r">{num(l.quantity)}</td>
                <td className="r">{l.unit_price != null ? money(l.unit_price) : '—'}</td>
                <td className="r">{money(l.line_total)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan="4" className="r muted">Total</td>
              <td className="r"><strong>{money(po.total)}</strong></td>
            </tr>
          </tfoot>
        </table>
        <p className="muted small">
          Unit prices are the chosen vendor's buying price (cheapest vendor per material);
          order quantity is rounded up to the vendor MOQ.
        </p>
      </section>

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
