import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { num, relativeTime } from '../format.js'
import { SHIPMENT_STATUSES, fmtTonnes, shipmentStatusBadge } from '../paperPlanning.js'

const STATUS_FILTERS = ['', ...SHIPMENT_STATUSES]

// All inbound paper shipments across purchase orders — open ones
// (CONFIRMED / ON_WATER) first by ETA. Recording and status updates happen on
// the PO detail page; this is the read-only fleet view.
export default function Shipments() {
  const { setUser } = useAuth()
  const [status, setStatus] = useState('')
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    const qs = status ? `?status=${encodeURIComponent(status)}` : ''
    api.get(`/api/shipments${qs}`)
      .then(setRows)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [status, setUser])

  useEffect(load, [load])

  return (
    <div>
      <div className="page-head">
        <h1>Shipping</h1>
        <span className="muted">inbound paper shipments — open ones first by ETA</span>
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
            <th>PO</th><th>Vendor</th><th>Vessel</th><th>ETD</th><th>ETA</th>
            <th className="r">Rolls</th><th className="r">Weight</th><th className="r">FCLs</th>
            <th>Status</th><th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {(rows || []).map((r) => (
            <tr key={r.id}>
              <td>
                <Link to={`/purchase-orders/${r.po_id}`}>{r.po_number}</Link>
              </td>
              <td>{r.vendor || '—'}</td>
              <td>{r.vessel || <span className="muted small">tba</span>}</td>
              <td className="nowrap">{r.etd || '—'}</td>
              <td className="nowrap">{r.eta || '—'}</td>
              <td className="r">{r.rolls != null ? num(r.rolls) : '—'}</td>
              <td className="r">{fmtTonnes(r.weight_kg)}</td>
              <td className="r">{r.fcl_count != null ? num(r.fcl_count) : '—'}</td>
              <td>
                <span className={`badge ${shipmentStatusBadge(r.status)}`}>
                  {r.status.replace(/_/g, ' ')}
                </span>
              </td>
              <td className="muted small">{relativeTime(r.updated_at)}</td>
            </tr>
          ))}
          {rows && rows.length === 0 && (
            <tr><td colSpan="10" className="muted center-cell">No shipments match this filter.</td></tr>
          )}
        </tbody>
      </table>
      <p className="muted small">
        Record a shipment from its purchase order page; ON_WATER weight shows as “in transit” on
        the Order Page coverage maths.
      </p>
    </div>
  )
}
