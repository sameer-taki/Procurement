import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { money, relativeTime } from '../format.js'
import { poStatusBadge } from '../purchaseOrders.js'

const STATUS_FILTERS = [
  '', 'DRAFT', 'PO_ISSUED', 'ACKNOWLEDGED', 'PARTIALLY_RECEIVED', 'RECEIVED', 'CANCELLED',
]

export default function PurchaseOrders() {
  const { setUser } = useAuth()
  const [status, setStatus] = useState('')
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    const qs = status ? `?status=${encodeURIComponent(status)}` : ''
    api.get(`/api/purchase-orders${qs}`)
      .then(setRows)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [status, setUser])

  useEffect(load, [load])

  return (
    <div>
      <div className="page-head">
        <h1>Purchase orders</h1>
        <span className="muted">raised from approved requisitions</span>
      </div>

      <div className="filters">
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUS_FILTERS.map((s) => (
            <option key={s} value={s}>{s ? s.replace(/_/g, ' ') : 'All statuses'}</option>
          ))}
        </select>
      </div>

      {error && <div className="error">{error}</div>}

      <table className="table">
        <thead>
          <tr>
            <th>Number</th><th>Vendor</th><th>Status</th>
            <th className="r">Total</th><th>Source req</th><th>BC PO no</th><th>Created</th>
          </tr>
        </thead>
        <tbody>
          {(rows || []).map((r) => (
            <tr key={r.id}>
              <td><Link to={`/purchase-orders/${r.id}`}>{r.number}</Link></td>
              <td>{r.vendor || '—'}</td>
              <td><span className={`badge ${poStatusBadge(r.status)}`}>{r.status.replace(/_/g, ' ')}</span></td>
              <td className="r">{money(r.total)}</td>
              <td>
                {r.requisition_id
                  ? <Link to={`/requisitions/${r.requisition_id}`}>{r.requisition_number || '—'}</Link>
                  : <span className="muted">{r.requisition_number || '—'}</span>}
              </td>
              <td>{r.bc_po_no || <span className="muted small">not posted</span>}</td>
              <td className="muted small">{relativeTime(r.created_at)}</td>
            </tr>
          ))}
          {rows && rows.length === 0 && (
            <tr><td colSpan="7" className="muted center-cell">No purchase orders match this filter.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
