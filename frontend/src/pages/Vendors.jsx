import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'

const EDITOR_ROLES = ['OFFICER', 'ADMIN']

// The vendor master, synced read-only from Business Central — except the ORDER
// EMAIL, which is an app-side contact override (GML's BC Vendor List page does
// not expose E-Mail). The email set here is where the PO-issued notification
// goes, and the sync will not wipe it.
export default function Vendors() {
  const { user, setUser } = useAuth()
  const [q, setQ] = useState('')
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const canEdit = EDITOR_ROLES.includes(user?.role)

  const load = useCallback(() => {
    const qs = q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ''
    api.get(`/api/vendors${qs}`)
      .then(setRows)
      .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
  }, [q, setUser])

  // Debounce the text filter (same 200 ms pattern as the stock search).
  useEffect(() => {
    const handle = setTimeout(load, 200)
    return () => clearTimeout(handle)
  }, [load])

  async function saveEmail(vendor, email) {
    setError('')
    setNotice('')
    try {
      const res = await api.patch(`/api/vendors/${vendor.id}`, { email })
      setNotice(`Email for ${res.name} ${res.email ? `set to ${res.email}` : 'cleared'}.`)
      setRows((rs) => (rs || []).map((r) => (r.id === res.id ? { ...r, email: res.email } : r)))
    } catch (e) {
      if (e.status === 401) setUser(null)
      else setError(e.message)
    }
  }

  return (
    <div>
      <div className="page-head">
        <h1>Vendors</h1>
        <span className="muted">vendor master — synced from Business Central; order email is set here</span>
      </div>

      <div className="filters">
        <input
          className="input"
          style={{ width: 320 }}
          placeholder="Search by name…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      {error && <div className="error">{error}</div>}
      {notice && <div className="banner">{notice}</div>}

      <table className="table">
        <thead>
          <tr>
            <th>Name</th><th>BC vendor no.</th><th>Order email</th>
            {canEdit && <th></th>}
          </tr>
        </thead>
        <tbody>
          {(rows || []).map((v) => (
            <VendorRow key={v.id} vendor={v} canEdit={canEdit} onSave={saveEmail} />
          ))}
          {rows && rows.length === 0 && (
            <tr>
              <td colSpan={canEdit ? 4 : 3} className="muted center-cell">
                No vendors match this search.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <p className="muted small">
        Vendors are owned by Business Central and refreshed automatically. The order email is kept
        in this app (BC doesn't expose it here) — it's the address the purchase-order notification
        is sent to, and syncs won't overwrite it.
      </p>
    </div>
  )
}

function VendorRow({ vendor, canEdit, onSave }) {
  const [editing, setEditing] = useState(false)
  const [email, setEmail] = useState(vendor.email || '')

  function submit(e) {
    e.preventDefault()
    onSave(vendor, email.trim())
    setEditing(false)
  }

  return (
    <tr>
      <td>{vendor.name}</td>
      <td className="nowrap">{vendor.bc_vendor_no || '—'}</td>
      <td>
        {editing ? (
          <form onSubmit={submit} style={{ display: 'flex', gap: 8 }}>
            <input
              className="input"
              style={{ width: 260 }}
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="orders@vendor.example"
              autoFocus
            />
            <button className="btn btn-primary" type="submit">Save</button>
            <button
              type="button"
              className="btn-link"
              onClick={() => { setEmail(vendor.email || ''); setEditing(false) }}
            >
              cancel
            </button>
          </form>
        ) : (
          vendor.email || <span className="muted small">not set</span>
        )}
      </td>
      {canEdit && (
        <td>
          {!editing && (
            <button type="button" className="btn-link" onClick={() => setEditing(true)}>
              {vendor.email ? 'Edit' : 'Set email'}
            </button>
          )}
        </td>
      )}
    </tr>
  )
}
