import React, { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { num, relativeTime } from '../format.js'
import {
  ROLE_CODES, fmtLimit, gradePreviewVerdict, integrationBadge, isLastActiveAdmin,
  outboxBadge, parseLimit, purgeSummaryLines,
} from '../admin.js'

// Admin panel: user/role management + system health. The backend enforces the
// rules (ADMIN-only endpoints, last-admin guard); this screen is the workflow
// for promoting an SSO-provisioned user, deactivating a leaver, tuning the
// approval limits the tiered engine routes by, and recovering FAILED outbox
// rows without a shell.
export default function Admin() {
  const { user, setUser, refresh } = useAuth()
  const navigate = useNavigate()
  const [users, setUsers] = useState(null)
  const [roles, setRoles] = useState(null)
  const [system, setSystem] = useState(null)
  const [limits, setLimits] = useState({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(() => {
    const fail = (e) => (e.status === 401 ? setUser(null) : setError(e.message))
    api.get('/api/admin/users').then(setUsers).catch(fail)
    api.get('/api/admin/roles').then((rs) => {
      setRoles(rs)
      setLimits(Object.fromEntries(rs.map((r) => [r.code, r.approval_limit ?? ''])))
    }).catch(fail)
    api.get('/api/admin/system').then(setSystem).catch(fail)
  }, [setUser])

  useEffect(load, [load])

  async function patchUser(u, body) {
    // Editing your own row can strip your ADMIN or deactivate you: refresh the
    // auth context and bounce home afterwards, or a stale context leaves every
    // subsequent admin call 403-ing with no explanation.
    const isSelf = !!user && (u.id === user.id || (!!u.email && u.email === user.email))
    if (isSelf && !window.confirm('This changes your own account — you may lose admin access. Continue?')) return
    setBusy(`user-${u.id}`)
    setError('')
    setNotice('')
    try {
      await api.patch(`/api/admin/users/${u.id}`, body)
      if (isSelf) {
        await refresh()
        navigate('/')
        return
      }
      setNotice(`Updated ${u.email}.`)
      load()
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function saveLimit(role) {
    const value = parseLimit(limits[role.code])
    if (value === undefined) {
      setError(`Bad approval limit for ${role.code}: enter a non-negative number or leave blank for unlimited.`)
      return
    }
    setBusy(`role-${role.code}`)
    setError('')
    setNotice('')
    try {
      await api.patch(`/api/admin/roles/${role.code}`, { approval_limit: value })
      setNotice(`${role.code} approval limit set to ${fmtLimit(value)}.`)
      load()
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function retryOutbox(row) {
    setBusy(`outbox-${row.id}`)
    setError('')
    setNotice('')
    try {
      await api.post(`/api/admin/outbox/${row.id}/retry`)
      setNotice(`Outbox row #${row.id} re-queued and processed.`)
      load()
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message)
    } finally {
      setBusy('')
    }
  }

  if (error && !users) return <div className="error">{error}</div>
  if (!users || !roles || !system) return <div className="muted">Loading admin…</div>

  const failed = system.outbox.failed_rows || []

  return (
    <div>
      <div className="page-head">
        <h1>Admin</h1>
        <span className="muted">users, roles & system health</span>
      </div>

      {error && <div className="error">{error}</div>}
      {notice && <div className="banner">{notice}</div>}

      <section className="card">
        <h2>Users</h2>
        <table className="table">
          <thead>
            <tr><th>Email</th><th>Name</th><th>Sign-in</th><th>Role</th><th>Active</th></tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const locked = isLastActiveAdmin(u, users)
              return (
                <tr key={u.id}>
                  <td>{u.email}</td>
                  <td>{u.name || '—'}</td>
                  <td className="muted small">{u.entra_linked ? 'Entra SSO' : 'local'}</td>
                  <td>
                    <select
                      className="select"
                      value={u.role || ''}
                      disabled={!!busy || locked}
                      title={locked ? 'The last active admin cannot be demoted' : undefined}
                      onChange={(e) => patchUser(u, { role: e.target.value })}
                    >
                      {ROLE_CODES.map((code) => (
                        <option key={code} value={code}>{code}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <button
                      className="btn small-btn"
                      disabled={!!busy || (u.active && locked)}
                      title={u.active && locked ? 'The last active admin cannot be deactivated' : undefined}
                      onClick={() => patchUser(u, { active: !u.active })}
                    >
                      {u.active ? 'Deactivate' : 'Reactivate'}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="muted small">
          New users arrive via Entra SSO as {`VIEWER`} (DEFAULT_ROLE) — promote them here.
          Every change is audited.
        </p>
      </section>

      <div className="grid-2">
        <section className="card">
          <h2>Approval limits <span className="muted small thin">tiered approval routes by these</span></h2>
          <table className="table">
            <thead><tr><th>Role</th><th className="r">Limit (FJD)</th><th /></tr></thead>
            <tbody>
              {roles.map((r) => (
                <tr key={r.code}>
                  <td>{r.code} <span className="muted small">{r.name}</span></td>
                  <td className="r">
                    <input
                      className="input qty"
                      value={limits[r.code] ?? ''}
                      placeholder="unlimited"
                      onChange={(e) => setLimits({ ...limits, [r.code]: e.target.value })}
                    />
                  </td>
                  <td className="r">
                    <button className="btn small-btn" disabled={!!busy}
                            onClick={() => saveLimit(r)}>Save</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">Blank = unlimited. Current: {roles.map((r) => `${r.code} ${fmtLimit(r.approval_limit)}`).join(' · ')}</p>
        </section>

        <section className="card">
          <h2>Integrations & schedulers</h2>
          <table className="table">
            <thead><tr><th>System</th><th>Mode</th><th>Configured</th></tr></thead>
            <tbody>
              {system.integrations.map((s) => (
                <tr key={s.system}>
                  <td>{s.system}</td>
                  <td><span className={`badge ${integrationBadge(s.mode)}`}>{s.mode}</span></td>
                  <td>{s.configured ? 'yes' : 'no'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            {system.schedulers.map((j) =>
              `${j.job} ${j.enabled ? `every ${num(j.interval_seconds)}s` : 'off'}`
            ).join(' · ')}
          </p>
        </section>
      </div>

      <GradePreviewCard setUser={setUser} />
      <PurgeCard setUser={setUser} onPurged={load} />

      <section className="card">
        <h2>
          Integration outbox{' '}
          <span className="muted small thin">
            {Object.entries(system.outbox.counts).map(([k, v]) => `${k} ${num(v)}`).join(' · ')}
          </span>
        </h2>
        {failed.length === 0 ? (
          <p className="ok-text">No failed rows — every BC post has landed or is queued.</p>
        ) : (
          <table className="table">
            <thead>
              <tr><th>#</th><th>Action</th><th>Ref</th><th className="r">Attempts</th>
                  <th>Last error</th><th>Age</th><th /></tr>
            </thead>
            <tbody>
              {failed.map((row) => (
                <tr key={row.id} className="row-warn">
                  <td>{row.id}</td>
                  <td><span className={`badge ${outboxBadge('FAILED')}`}>{row.action}</span></td>
                  <td className="muted small">{row.entity_ref || '—'}</td>
                  <td className="r">{num(row.attempts)}</td>
                  <td className="muted small">{row.last_error || '—'}</td>
                  <td className="muted small">{relativeTime(row.created_at)}</td>
                  <td className="r">
                    <button className="btn small-btn" disabled={!!busy}
                            onClick={() => retryOutbox(row)}>
                      {busy === `outbox-${row.id}` ? 'Retrying…' : 'Retry'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}

// Dry-run a candidate BC_PAPER_SKU_REGEX against the real synced master before
// touching env. The verdict line calls out the classic trap: a pattern that
// matches but captures no grade (missing parentheses) grades nothing on resync.
function GradePreviewCard({ setUser }) {
  const [regex, setRegex] = useState('^([A-Z]{2,4}\\d{2,3})$')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function run(e) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      setResult(await api.get(`/api/planning/grade-preview?regex=${encodeURIComponent(regex)}`))
    } catch (err) {
      if (err.status === 401) setUser(null)
      else { setResult(null); setError(err.message) }
    } finally {
      setBusy(false)
    }
  }

  const graded = result ? (result.match_count ?? 0) - (result.ungraded_matches ?? 0) : 0

  return (
    <section className="card">
      <h2>
        Paper grade preview{' '}
        <span className="muted small thin">
          test a BC_PAPER_SKU_REGEX against the synced item master before setting it
        </span>
      </h2>
      <form onSubmit={run} className="filters">
        <input
          className="input"
          style={{ maxWidth: 420, fontFamily: 'ui-monospace, Menlo, monospace' }}
          value={regex}
          onChange={(e) => setRegex(e.target.value)}
          placeholder={'e.g. ^([A-Z]{2,4}\\d{2,3})$'}
        />
        <button className="btn btn-primary" type="submit" disabled={busy || !regex.trim()}>
          {busy ? 'Scanning…' : 'Preview'}
        </button>
      </form>
      {error && <div className="error">{error}</div>}
      {result && (
        <>
          <p className={graded === 0 ? 'warn' : ''}>{gradePreviewVerdict(result)}</p>
          {result.grades.length > 0 && (
            <p>
              {result.grades.slice(0, 30).map((g) => <span key={g} className="chip">{g}</span>)}
              {result.grades.length > 30 && (
                <span className="muted small"> …and {num(result.grades.length - 30)} more</span>
              )}
            </p>
          )}
          {result.sample.length > 0 && (
            <table className="table">
              <thead>
                <tr><th>SKU</th><th>Name</th><th>Grade</th><th className="r">Deckle (mm)</th></tr>
              </thead>
              <tbody>
                {result.sample.slice(0, 15).map((m) => (
                  <tr key={m.sku}>
                    <td>{m.sku}</td>
                    <td>{m.name}</td>
                    <td><span className="chip">{m.grade}</span></td>
                    <td className="r">{m.deckle_mm ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="muted small">
            Looks right? Set <code>BC_PAPER_SKU_REGEX</code> to this pattern in Portainer and
            redeploy — the next item sync assigns the grades and the Order Page picks them up.
            Currently graded items: {num(result.items_currently_graded)} / {num(result.total_items)}.
          </p>
        </>
      )}
    </section>
  )
}

// One-shot demo-catalog cleanup once live BC data is synced. The backend
// refuses in demo mode and never touches rows referenced by real orders.
function PurgeCard({ setUser, onPurged }) {
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function purge() {
    if (!window.confirm(
      'Remove the demo catalog (demo items, vendors, customers, prices, BOMs, forecasts)? '
      + 'Rows referenced by real requisitions or POs are kept. This cannot be undone.',
    )) return
    setError('')
    setBusy(true)
    try {
      const res = await api.post('/api/admin/purge-demo-data')
      setSummary(res)
      onPurged()
    } catch (err) {
      if (err.status === 401) setUser(null)
      else setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card">
      <h2>
        Demo data{' '}
        <span className="muted small thin">
          remove the built-in demo catalog once live BC data is synced
        </span>
      </h2>
      {error && <div className="error">{error}</div>}
      {summary ? (
        <div className="banner">
          {purgeSummaryLines(summary).map((line) => <div key={line}>{line}</div>)}
        </div>
      ) : (
        <div className="form-actions">
          <button className="btn btn-danger" onClick={purge} disabled={busy}>
            {busy ? 'Purging…' : 'Purge demo data'}
          </button>
        </div>
      )}
      <p className="muted small">
        Only works with live BC configured (in demo mode the seed would put it straight back).
        Audited; safe to run more than once.
      </p>
    </section>
  )
}
