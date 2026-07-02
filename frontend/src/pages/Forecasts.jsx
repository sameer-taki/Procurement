import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { num, relativeTime } from '../format.js'
import { buildForecastPayload, canPlanPaper, monthLabel } from '../paperPlanning.js'

// Customer carton forecasts — the FORECAST basis the Order Page explodes
// through the BOMs into paper usage. Anyone can read; officer/admin upsert
// (PUT is an idempotent write per customer+sku+period) and delete.
export default function Forecasts() {
  const { user, setUser } = useAuth()
  const [customer, setCustomer] = useState('')
  const [period, setPeriod] = useState('')
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const canPlan = canPlanPaper(user)

  const load = useCallback(() => {
    const params = new URLSearchParams()
    if (customer.trim()) params.set('customer', customer.trim())
    if (period) params.set('period', period)
    const qs = params.toString()
    api.get(`/api/forecasts${qs ? `?${qs}` : ''}`)
      .then(setRows)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [customer, period, setUser])

  // Debounce the text filter (same 200 ms pattern as the stock search).
  useEffect(() => {
    const handle = setTimeout(load, 200)
    return () => clearTimeout(handle)
  }, [load])

  async function remove(r) {
    if (!window.confirm(`Delete the ${monthLabel(r.period)} forecast for ${r.customer} · ${r.sku}?`)) return
    setError('')
    setNotice('')
    try {
      await api.del(`/api/forecasts/${r.id}`)
      load()
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message)
    }
  }

  function onSaved(written) {
    setNotice(`${num(written)} forecast line${written === 1 ? '' : 's'} saved.`)
    load()
  }

  return (
    <div>
      <div className="page-head">
        <h1>Forecasts</h1>
        <span className="muted">customer cartons per month — drives the Order Page paper plan</span>
      </div>

      {canPlan && <ForecastForm onSaved={onSaved} setUser={setUser} />}

      <div className="filters">
        <input
          className="input"
          style={{ width: 260 }}
          placeholder="Filter by customer…"
          value={customer}
          onChange={(e) => setCustomer(e.target.value)}
        />
        <input
          className="input"
          style={{ width: 170 }}
          type="month"
          value={period}
          onChange={(e) => setPeriod(e.target.value)}
        />
        {period && (
          <button className="btn-link" style={{ color: 'var(--muted)' }} onClick={() => setPeriod('')}>
            clear month
          </button>
        )}
      </div>

      {error && <div className="error">{error}</div>}
      {notice && <div className="banner">{notice}</div>}

      <table className="table">
        <thead>
          <tr>
            <th>Customer</th><th>SKU</th><th>Product</th><th>Period</th>
            <th className="r">Cartons</th><th>Updated by</th><th>Updated</th>
            {canPlan && <th></th>}
          </tr>
        </thead>
        <tbody>
          {(rows || []).map((r) => (
            <tr key={r.id}>
              <td>{r.customer}</td>
              <td><Link to={`/stock/${r.sku}`}>{r.sku}</Link></td>
              <td>{r.name}</td>
              <td className="nowrap">{monthLabel(r.period)}</td>
              <td className="r">{num(r.qty_cartons)}</td>
              <td>{r.updated_by || '—'}</td>
              <td className="muted small">{relativeTime(r.updated_at)}</td>
              {canPlan && (
                <td>
                  <button type="button" className="btn-link warn" onClick={() => remove(r)}>Delete</button>
                </td>
              )}
            </tr>
          ))}
          {rows && rows.length === 0 && (
            <tr>
              <td colSpan={canPlan ? 8 : 7} className="muted center-cell">
                No forecasts match this filter.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

// Inline upsert card: customer + finished good (typeahead) + period + cartons.
// PUT /api/forecasts overwrites the (customer, sku, period) line, so re-saving
// updates in place. The backend enforces RBAC (403) / bad period (400) /
// unknown sku (404).
function ForecastForm({ onSaved, setUser }) {
  const [customer, setCustomer] = useState('')
  const [q, setQ] = useState('')
  const [results, setResults] = useState([])
  const [item, setItem] = useState(null) // {sku, name}
  const [period, setPeriod] = useState(() => new Date().toISOString().slice(0, 7))
  const [qty, setQty] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!q.trim()) { setResults([]); return }
    const handle = setTimeout(() => {
      api.get(`/api/stock?q=${encodeURIComponent(q)}`)
        .then((d) => setResults(
          // Forecasts are for finished goods only (when the API says the type).
          (d?.results || []).filter((r) => !r.item_type || r.item_type === 'FINISHED'),
        ))
        .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
    }, 200)
    return () => clearTimeout(handle)
  }, [q, setUser])

  function pick(r) {
    setItem({ sku: r.sku, name: r.name })
    setQ('')
    setResults([])
  }

  async function submit(e) {
    e.preventDefault()
    setError('')
    if (!item) { setError('Choose a finished good.'); return }
    const payload = buildForecastPayload([
      { customer, sku: item.sku, period, qty_cartons: qty },
    ])
    if (payload.lines.length === 0) {
      setError('Enter a customer, a month, and a carton quantity of zero or more.')
      return
    }
    setBusy(true)
    try {
      const res = await api.put('/api/forecasts', payload)
      setQty('') // keep customer + period for fast month-by-month entry
      onSaved(res?.written ?? payload.lines.length)
    } catch (err) {
      if (err.status === 401) setUser(null)
      else setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card" onSubmit={submit}>
      <h2>Add / update forecast</h2>
      <div className="form-row">
        <label className="field">
          <span className="field-label">Customer</span>
          <input
            className="input"
            value={customer}
            onChange={(e) => setCustomer(e.target.value)}
            placeholder="e.g. Fiji Water"
          />
        </label>
        <label className="field">
          <span className="field-label">Month</span>
          <input className="input" type="month" value={period} onChange={(e) => setPeriod(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">Cartons</span>
          <input
            className="input qty"
            type="number" min="0" step="1"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            placeholder="0"
          />
        </label>
      </div>

      <div className="field">
        <span className="field-label">Finished good</span>
        {item ? (
          <div>
            <span className="chip"><strong>{item.sku}</strong> · {item.name}</span>
            <button type="button" className="btn-link warn" onClick={() => setItem(null)}>change</button>
          </div>
        ) : (
          <input
            className="input"
            placeholder="Search a finished good SKU or name…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        )}
      </div>
      {!item && results.length > 0 && (
        <ul className="suggest">
          {results.slice(0, 8).map((r) => (
            <li key={r.sku}>
              <button type="button" className="suggest-item" onClick={() => pick(r)}>
                <span><strong>{r.sku}</strong> · {r.name}</span>
                <span className="muted small">{r.item_type || ''}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {error && <div className="error">{error}</div>}

      <div className="form-actions">
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? 'Saving…' : 'Save forecast'}
        </button>
      </div>
      <p className="muted small">
        Saving overwrites the same customer + SKU + month; a quantity of 0 keeps the line but kills
        the demand for that month.
      </p>
    </form>
  )
}
