import React, { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { money, num, relativeTime } from '../format.js'
import { availableActions, statusBadge } from '../requisitions.js'

export default function RequisitionDetail() {
  const { id } = useParams()
  const { user, setUser } = useAuth()
  const [req, setReq] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(() => {
    api.get(`/api/requisitions/${id}`)
      .then(setReq)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [id, setUser])

  useEffect(load, [load])

  async function act(action, body) {
    setBusy(action)
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

  function reject() {
    const reason = window.prompt('Reason for rejection?')
    if (reason == null) return // cancelled the prompt
    act('reject', { reason })
  }

  function cancel() {
    if (!window.confirm('Cancel this requisition? This cannot be undone.')) return
    act('cancel')
  }

  if (error && !req) return <div className="error">{error}</div>
  if (!req) return <div className="muted">Loading requisition…</div>

  const actions = availableActions(user, req.status, req.estimated_amount, req.requester)
  const anyAction = actions.submit || actions.cancel || actions.approve || actions.reject

  return (
    <div>
      <div className="page-head">
        <div>
          <Link to="/requisitions" className="back">← Requisitions</Link>
          <h1>
            {req.number}{' '}
            <span className={`badge ${statusBadge(req.status)}`}>{req.status.replace('_', ' ')}</span>
          </h1>
        </div>
        {anyAction && (
          <div className="form-actions">
            {actions.submit && (
              <button className="btn btn-primary" disabled={!!busy} onClick={() => act('submit')}>
                {busy === 'submit' ? 'Submitting…' : 'Submit for approval'}
              </button>
            )}
            {actions.approve && (
              <button className="btn btn-primary" disabled={!!busy} onClick={() => act('approve')}>
                {busy === 'approve' ? 'Approving…' : 'Approve'}
              </button>
            )}
            {actions.reject && (
              <button className="btn" disabled={!!busy} onClick={reject}>
                {busy === 'reject' ? 'Rejecting…' : 'Reject'}
              </button>
            )}
            {actions.cancel && (
              <button className="btn" disabled={!!busy} onClick={cancel}>
                {busy === 'cancel' ? 'Cancelling…' : 'Cancel'}
              </button>
            )}
          </div>
        )}
      </div>

      {error && <div className="error">{error}</div>}

      <div className="meta-row">
        <Meta label="Requester" value={req.requester} />
        <Meta label="Cost centre" value={req.cost_center || '—'} />
        <Meta label="Lines" value={num(req.lines?.length || 0)} />
        <Meta label="Estimated amount" value={<span>{money(req.estimated_amount)} <span className="muted small">est.</span></span>} />
        <Meta label="Created" value={relativeTime(req.created_at)} />
      </div>

      <section className="card">
        <h2>Lines</h2>
        <table className="table">
          <thead>
            <tr>
              <th>SKU</th><th>Material</th>
              <th className="r">Qty</th><th className="r">Unit (est.)</th>
              <th className="r">Line total</th><th>Needed by</th>
            </tr>
          </thead>
          <tbody>
            {(req.lines || []).map((l, i) => (
              <tr key={i}>
                <td><Link to={`/stock/${l.sku}`}>{l.sku}</Link></td>
                <td>{l.name}</td>
                <td className="r">{num(l.quantity)}</td>
                <td className="r">{l.unit_price != null ? money(l.unit_price) : '—'}</td>
                <td className="r">{money(l.line_total)}</td>
                <td className="muted small">{l.needed_by || '—'}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan="4" className="r muted">Estimated total</td>
              <td className="r"><strong>{money(req.estimated_amount)}</strong> <span className="muted small">est.</span></td>
              <td></td>
            </tr>
          </tfoot>
        </table>
      </section>

      <section className="card">
        <h2>History</h2>
        {(!req.events || req.events.length === 0) ? (
          <p className="muted">No events recorded yet.</p>
        ) : (
          <ul className="timeline">
            {req.events.map((ev, i) => (
              <li key={i} className="timeline-item">
                <div className="timeline-dot" />
                <div className="timeline-body">
                  <div>
                    <strong>{ev.event_type}</strong>
                    {ev.from_status && (
                      <span className="muted small">
                        {' '}· {ev.from_status.replace('_', ' ')} → {ev.to_status?.replace('_', ' ')}
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
