import React, { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { money, num, relativeTime } from '../format.js'
import { canPlanPaper } from '../paperPlanning.js'

export default function StockDetail() {
  const { sku } = useParams()
  const { setUser } = useAuth()
  const [v, setV] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Clear the error on SKU change and guard the async callback so a slow
  // response for a previous SKU can't cross-paint onto the new one.
  useEffect(() => {
    let live = true
    setError('')
    api.get(`/api/stock/${encodeURIComponent(sku)}`)
      .then((d) => { if (live) setV(d) })
      .catch((e) => {
        if (!live) return
        if (e.status === 401) setUser(null)
        else setError(e.message)
      })
    return () => { live = false }
  }, [sku, setUser])

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

      <BoardGradeSpec sku={v.sku} />
    </div>
  )
}

// SOP step 2 master data: the board grade spec — the top "kit" level of the BOM
// this app owns (CLAUDE.md §2). Officer/admin edit APP-owned bills inline (or
// create one for a purchased-leaf item); bills mirrored from Kiwiplan/Accura
// render read-only because production owns the material BOMs.
function BoardGradeSpec({ sku }) {
  const { user, setUser } = useAuth()
  const [bom, setBom] = useState(null) // null = no bill / purchased leaf
  const [loaded, setLoaded] = useState(false)
  const [editing, setEditing] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    setLoaded(false)
    setEditing(false)
    setError('')
    api.get(`/api/items/${encodeURIComponent(sku)}/bom`)
      .then((d) => { if (live) { setBom(d); setLoaded(true) } })
      .catch((e) => {
        if (!live) return
        if (e.status === 401) setUser(null)
        else setError(e.message)
        setLoaded(true)
      })
    return () => { live = false }
  }, [sku, setUser])

  const components = bom?.components || []
  const mirrored = components.some((c) => c.owner && c.owner !== 'APP')
  const canEdit = canPlanPaper(user) && !mirrored

  function onSaved(tree) {
    setBom(tree)
    setEditing(false)
    setError('')
  }

  async function retire() {
    if (!window.confirm(`Retire the board grade spec for ${sku}? The bill becomes OBSOLETE and planning treats this item as purchased.`)) return
    setError('')
    setBusy(true)
    try {
      await api.del(`/api/items/${encodeURIComponent(sku)}/bom`)
      setBom(null)
      setEditing(false)
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message) // 409 mirrored / 404 already retired
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card">
      <h2>Board grade spec (BOM)</h2>
      {error && <div className="error">{error}</div>}
      {!loaded ? (
        <p className="muted">Loading spec…</p>
      ) : (
        <>
          {components.length > 0 ? (
            <table className="table">
              <thead>
                <tr>
                  <th>SKU</th><th>Component</th>
                  <th className="r">Qty per unit</th><th className="r">Scrap / trim</th><th>Owner</th>
                </tr>
              </thead>
              <tbody>
                <SpecRows nodes={components} depth={0} />
              </tbody>
            </table>
          ) : (
            <p className="muted">No bill of materials — this is a purchased material.</p>
          )}

          {mirrored && (
            <p className="muted small">
              mirrored read-only from KIWIPLAN/ACCURA — production owns material BOMs
            </p>
          )}

          {canEdit && (
            <div className="form-actions" style={{ marginTop: 12 }}>
              <button className="btn" type="button" onClick={() => setEditing((e) => !e)} disabled={busy}>
                {editing ? 'Close' : bom ? 'Edit spec' : 'Create spec'}
              </button>
              {bom && (
                <button className="btn-link warn" type="button" onClick={retire} disabled={busy}>
                  {busy ? 'Retiring…' : 'Retire spec'}
                </button>
              )}
            </div>
          )}

          {canEdit && editing && (
            <SpecEditor sku={sku} components={components} yieldQtyInit={bom?.yield_qty} versionInit={bom?.version} onSaved={onSaved} setUser={setUser} />
          )}
        </>
      )}
    </section>
  )
}

// The component tree as table rows; children indent under their parent so a
// mirrored material sub-bill reads in place beneath the kit line that owns it.
function SpecRows({ nodes, depth }) {
  return (nodes || []).map((c) => (
    <React.Fragment key={`${depth}:${c.sku}`}>
      <tr>
        <td style={depth > 0 ? { paddingLeft: 10 + depth * 18 } : undefined}>
          <Link to={`/stock/${c.sku}`}>{c.sku}</Link>
        </td>
        <td>{c.name}</td>
        <td className="r">{c.qty_per != null ? num(c.qty_per) : '—'}</td>
        <td className="r">{c.scrap_pct ? `${num(Number(c.scrap_pct) * 100)}%` : '—'}</td>
        <td>{c.owner ? <span className="chip">{c.owner}</span> : '—'}</td>
      </tr>
      {(c.components || []).length > 0 && <SpecRows nodes={c.components} depth={depth + 1} />}
    </React.Fragment>
  ))
}

// Inline line editor for the APP-owned kit level: the stock typeahead adds
// component lines; PUT /api/items/:sku/bom versions the bill (old ACTIVE ->
// OBSOLETE) and returns the new tree. Scrap is entered as a percentage here;
// the API takes a 0..1 fraction.
function SpecEditor({ sku, components, yieldQtyInit, versionInit, onSaved, setUser }) {
  const [yieldQty, setYieldQty] = useState(() => (
    Number(yieldQtyInit) > 0 ? String(yieldQtyInit) : '1'
  ))
  // Capture the active bill's version when the editor opens; send it back as
  // base_version so the PUT 409s (optimistic concurrency) if another officer
  // has saved a new version since. 0 = no active bill when we opened.
  const [baseVersion] = useState(() => Number(versionInit) || 0)
  const [lines, setLines] = useState(() => components.map((c) => ({
    sku: c.sku,
    name: c.name,
    qty_per: c.qty_per != null ? String(c.qty_per) : '1',
    scrap_pct: c.scrap_pct ? String(Number((Number(c.scrap_pct) * 100).toFixed(4))) : '',
  })))
  const [q, setQ] = useState('')
  const [results, setResults] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!q.trim()) { setResults([]); return }
    const handle = setTimeout(() => {
      api.get(`/api/stock?q=${encodeURIComponent(q)}`)
        // An item can't be its own component (the backend 400s on it anyway).
        .then((d) => setResults((d?.results || []).filter((r) => r.sku !== sku)))
        .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
    }, 200)
    return () => clearTimeout(handle)
  }, [q, sku, setUser])

  function addLine(r) {
    setLines((prev) => {
      if (prev.some((l) => l.sku === r.sku)) return prev
      return [...prev, { sku: r.sku, name: r.name, qty_per: '1', scrap_pct: '' }]
    })
    setQ('')
    setResults([])
  }

  function updateLine(lineSku, patch) {
    setLines((prev) => prev.map((l) => (l.sku === lineSku ? { ...l, ...patch } : l)))
  }

  function removeLine(lineSku) {
    setLines((prev) => prev.filter((l) => l.sku !== lineSku))
  }

  async function submit(e) {
    e.preventDefault()
    setError('')
    if (lines.length === 0) { setError('Add at least one component line.'); return }
    if (!(Number(yieldQty) > 0)) { setError('Yield must be greater than zero.'); return }
    if (lines.some((l) => !(Number(l.qty_per) > 0))) {
      setError('Every component needs a qty per unit greater than zero.'); return
    }
    if (lines.some((l) => {
      const s = Number(l.scrap_pct || 0)
      return !Number.isFinite(s) || s < 0 || s > 100
    })) {
      setError('Scrap must be between 0 and 100%.'); return
    }
    setBusy(true)
    try {
      const body = {
        yield_qty: Number(yieldQty),
        base_version: baseVersion,
        lines: lines.map((l) => ({
          sku: l.sku,
          qty_per: Number(l.qty_per),
          scrap_pct: Number(l.scrap_pct || 0) / 100,
        })),
      }
      // On success onSaved() closes the editor; on any error we fall to the
      // catch (editor stays open) — including a 409 "Spec changed since you
      // opened it…" so the officer can read it and reload.
      onSaved(await api.put(`/api/items/${encodeURIComponent(sku)}/bom`, body))
    } catch (err) {
      if (err.status === 401) setUser(null)
      // 404 unknown component / 400 duplicate-or-own-parent / 409 mirrored,
      // cycle, or stale base_version (another officer saved since we opened)
      else setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit}>
      <div className="form-row" style={{ marginTop: 12 }}>
        <label className="field">
          <span className="field-label">Yield qty <span className="muted">(units made per build)</span></span>
          <input
            className="input qty"
            type="number" min="0" step="any"
            value={yieldQty}
            onChange={(e) => setYieldQty(e.target.value)}
          />
        </label>
      </div>

      <div className="field">
        <span className="field-label">Add component</span>
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
                <span className="muted small">{r.item_type || ''}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {lines.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>SKU</th><th>Component</th>
              <th className="r">Qty per unit</th><th className="r">Scrap %</th><th></th>
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
                    value={l.qty_per}
                    onChange={(e) => updateLine(l.sku, { qty_per: e.target.value })}
                  />
                </td>
                <td className="r">
                  <input
                    className="input qty"
                    type="number" min="0" max="100" step="any"
                    placeholder="0"
                    value={l.scrap_pct}
                    onChange={(e) => updateLine(l.sku, { scrap_pct: e.target.value })}
                  />
                </td>
                <td><button type="button" className="btn-link warn" onClick={() => removeLine(l.sku)}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {error && <div className="error">{error}</div>}

      <div className="form-actions" style={{ marginTop: 12 }}>
        <button className="btn btn-primary" type="submit" disabled={busy || lines.length === 0}>
          {busy ? 'Saving…' : 'Save spec'}
        </button>
      </div>
      <p className="muted small">
        Saving writes a new ACTIVE version of the kit-level bill (the old one becomes OBSOLETE).
        Scrap/trim is the percentage of the component consumed as waste on top of the net quantity.
      </p>
    </form>
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
