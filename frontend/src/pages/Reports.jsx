import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { num, relativeTime } from '../format.js'
import { PREVIEW_ROWS, REPORTS, csvHref, fmtCell } from '../reports.js'

// Operational reports: the registers behind the Analytics KPIs. Each report
// previews here (first rows) and downloads as CSV with the same columns —
// one backend serializer, so screen and file can never disagree.
export default function Reports() {
  const { setUser } = useAuth()
  const [data, setData] = useState({})
  const [error, setError] = useState('')

  const load = useCallback(() => {
    for (const report of REPORTS) {
      api.get(report.path)
        .then((res) => setData((prev) => ({ ...prev, [report.key]: res })))
        .catch((e) => (e.status === 401 ? setUser(null) : setError(e.message)))
    }
  }, [setUser])

  useEffect(load, [load])

  return (
    <div>
      <div className="page-head">
        <h1>Reports</h1>
        <span className="muted">operational registers · download as CSV for Excel</span>
      </div>
      {error && <div className="error">{error}</div>}

      {REPORTS.map((report) => {
        const res = data[report.key]
        return (
          <section className="card" key={report.key}>
            <div className="page-head" style={{ marginBottom: 8 }}>
              <div>
                <h2 style={{ margin: 0 }}>{report.title}</h2>
                <span className="muted small">{report.description}</span>
              </div>
              <a className="btn nowrap" href={csvHref(report.path)} download>
                Download CSV{res ? ` (${num(res.count)} rows)` : ''}
              </a>
            </div>
            {!res ? (
              <p className="muted">Loading…</p>
            ) : res.rows.length === 0 ? (
              <p className="muted">No rows yet.</p>
            ) : (
              <>
                <table className="table">
                  <thead>
                    <tr>{res.columns.map((c) => (
                      <th key={c} className={typeof res.rows[0][c] === 'number' ? 'r' : ''}>
                        {c.replaceAll('_', ' ')}
                      </th>
                    ))}</tr>
                  </thead>
                  <tbody>
                    {res.rows.slice(0, PREVIEW_ROWS).map((row, i) => (
                      <tr key={i}>
                        {res.columns.map((c) => (
                          <td key={c} className={typeof row[c] === 'number' ? 'r' : ''}>
                            {fmtCell(c, row[c])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="muted small">
                  {res.rows.length > PREVIEW_ROWS
                    ? `Showing ${num(PREVIEW_ROWS)} of ${num(res.count)} rows — the CSV has all of them. `
                    : ''}
                  As of {relativeTime(res.as_of)}.
                </p>
              </>
            )}
          </section>
        )
      })}
    </div>
  )
}
