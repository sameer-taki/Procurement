import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { money, num, relativeTime } from '../format.js'
import { canRequest, statusBadge } from '../requisitions.js'

const STATUS_FILTERS = [
  '', 'DRAFT', 'SUBMITTED', 'IN_APPROVAL', 'APPROVED', 'REJECTED', 'CANCELLED',
]

export default function Requisitions() {
  const { user, setUser } = useAuth()
  const [status, setStatus] = useState('')
  const [mine, setMine] = useState(false)
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)

  const load = useCallback(() => {
    const params = new URLSearchParams()
    if (status) params.set('status', status)
    if (mine) params.set('mine', 'true')
    const qs = params.toString()
    api.get(`/api/requisitions${qs ? `?${qs}` : ''}`)
      .then(setRows)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [status, mine, setUser])

  useEffect(load, [load])

  function onCreated() {
    setShowForm(false)
    load()
  }

  return (
    <div>
      <div className="page-head">
        <h1>Requisitions</h1>
        {canRequest(user) && (
          <button className="btn btn-primary" onClick={() => setShowForm((s) => !s)}>
            {showForm ? 'Close' : 'New requisition'}
          </button>
        )}
      </div>

      {showForm && canRequest(user) && (
        <NewRequisition onCreated={onCreated} onCancel={() => setShowForm(false)} setUser={setUser} />
      )}

      <div className="filters">
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUS_FILTERS.map((s) => (
            <option key={s} value={s}>{s ? s.replace('_', ' ') : 'All statuses'}</option>
          ))}
        </select>
        <label className="check">
          <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} />
          Raised by me
        </label>
      </div>

      {error && <div className="error">{error}</div>}

      <table className="table">
        <thead>
          <tr>
            <th>Number</th><th>Requester</th><th>Status</th>
            <th className="r">Lines</th><th className="r">Estimated</th><th>Created</th>
          </tr>
        </thead>
        <tbody>
          {(rows || []).map((r) => (
            <tr key={r.id}>
              <td><Link to={`/requisitions/${r.id}`}>{r.number}</Link></td>
              <td>{r.requester}</td>
              <td><span className={`badge ${statusBadge(r.status)}`}>{r.status.replace('_', ' ')}</span></td>
              <td className="r">{num(r.line_count)}</td>
              <td className="r">{money(r.estimated_amount)} <span className="muted small">est.</span></td>
              <td className="muted small">{relativeTime(r.created_at)}</td>
            </tr>
          ))}
          {rows && rows.length === 0 && (
            <tr><td colSpan="6" className="muted center-cell">No requisitions match this filter.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

function NewRequisition({ onCreated, onCancel, setUser }) {
  const [costCenter, setCostCenter] = useState('')
  const [q, setQ] = useState('')
  const [results, setResults] = useState([])
  const [lines, setLines] = useState([]) // {sku, name, sales_price, quantity, needed_by}
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!q.trim()) { setResults([]); return }
    const handle = setTimeout(() => {
      api.get(`/api/stock?q=${encodeURIComponent(q)}`)
        .then((d) => setResults(d?.results || []))
        .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
    }, 200)
    return () => clearTimeout(handle)
  }, [q, setUser])

  function addLine(r) {
    setLines((prev) => {
      if (prev.some((l) => l.sku === r.sku)) return prev
      const sales_price = r.price?.unit_price ?? r.sales_price ?? null
      return [...prev, { sku: r.sku, name: r.name, sales_price, quantity: 1, needed_by: '' }]
    })
    setQ('')
    setResults([])
  }

  function updateLine(sku, patch) {
    setLines((prev) => prev.map((l) => (l.sku === sku ? { ...l, ...patch } : l)))
  }

  function removeLine(sku) {
    setLines((prev) => prev.filter((l) => l.sku !== sku))
  }

  const estimated = lines.reduce(
    (sum, l) => sum + Number(l.quantity || 0) * Number(l.sales_price || 0), 0,
  )

  async function submit(e) {
    e.preventDefault()
    setError('')
    if (lines.length === 0) { setError('Add at least one line.'); return }
    if (lines.some((l) => !(Number(l.quantity) > 0))) {
      setError('Every line needs a quantity greater than zero.'); return
    }
    setBusy(true)
    try {
      const body = {
        cost_center: costCenter || undefined,
        lines: lines.map((l) => ({
          sku: l.sku,
          quantity: Number(l.quantity),
          needed_by: l.needed_by || undefined,
        })),
      }
      const created = await api.post('/api/requisitions', body)
      onCreated(created)
    } catch (err) {
      if (err.status === 401) setUser(null)
      else setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card" onSubmit={submit}>
      <h2>New requisition</h2>
      <div className="form-row">
        <label className="field">
          <span className="field-label">Cost centre <span className="muted">(optional)</span></span>
          <input className="input" value={costCenter} onChange={(e) => setCostCenter(e.target.value)} placeholder="e.g. CC-100" />
        </label>
      </div>

      <div className="field">
        <span className="field-label">Add material</span>
        <input
          className="input"
          placeholder="Search any SKU or material name…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {results.length > 0 && (
        <ul className="suggest">
          {results.slice(0, 8).map((r) => (
            <li key={r.sku}>
              <button type="button" className="suggest-item" onClick={() => addLine(r)}>
                <span><strong>{r.sku}</strong> · {r.name}</span>
                <span className="muted small">
                  {r.price?.unit_price != null ? money(r.price.unit_price) : 'no price'}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {lines.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>SKU</th><th>Material</th>
              <th className="r">Qty</th><th className="r">Unit (est.)</th>
              <th className="r">Line total</th><th>Needed by</th><th></th>
            </tr>
          </thead>
          <tbody>
            {lines.map((l) => (
              <tr key={l.sku}>
                <td>{l.sku}</td>
                <td>{l.name}</td>
                <td className="r">
                  <input
                    className="input qty"
                    type="number" min="0" step="any"
                    value={l.quantity}
                    onChange={(e) => updateLine(l.sku, { quantity: e.target.value })}
                  />
                </td>
                <td className="r">{l.sales_price != null ? money(l.sales_price) : '—'}</td>
                <td className="r">{money(Number(l.quantity || 0) * Number(l.sales_price || 0))}</td>
                <td>
                  <input
                    className="input"
                    type="date"
                    value={l.needed_by}
                    onChange={(e) => updateLine(l.sku, { needed_by: e.target.value })}
                  />
                </td>
                <td><button type="button" className="btn-link warn" onClick={() => removeLine(l.sku)}>Remove</button></td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan="4" className="r muted">Estimated total</td>
              <td className="r"><strong>{money(estimated)}</strong> <span className="muted small">est.</span></td>
              <td colSpan="2"></td>
            </tr>
          </tfoot>
        </table>
      )}

      {error && <div className="error">{error}</div>}

      <div className="form-actions">
        <button className="btn" type="button" onClick={onCancel} disabled={busy}>Cancel</button>
        <button className="btn btn-primary" type="submit" disabled={busy || lines.length === 0}>
          {busy ? 'Creating…' : 'Create draft'}
        </button>
      </div>
      <p className="muted small">
        Amounts use the BC selling price as an estimate; the buying price is set when a vendor is
        chosen in Phase 3.
      </p>
    </form>
  )
}
