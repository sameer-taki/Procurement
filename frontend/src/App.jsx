import React from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { useAuth } from './auth.jsx'
import Login from './pages/Login.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Stock from './pages/Stock.jsx'
import StockDetail from './pages/StockDetail.jsx'
import Requisitions from './pages/Requisitions.jsx'
import RequisitionDetail from './pages/RequisitionDetail.jsx'
import Approvals from './pages/Approvals.jsx'
import PurchaseOrders from './pages/PurchaseOrders.jsx'
import PurchaseOrderDetail from './pages/PurchaseOrderDetail.jsx'
import Planning from './pages/Planning.jsx'
import PaperPlanning from './pages/PaperPlanning.jsx'
import Forecasts from './pages/Forecasts.jsx'
import Customers from './pages/Customers.jsx'
import Vendors from './pages/Vendors.jsx'
import Shipments from './pages/Shipments.jsx'
import Analytics from './pages/Analytics.jsx'
import Reports from './pages/Reports.jsx'
import Admin from './pages/Admin.jsx'
import { canAdmin } from './admin.js'

// Sidebar structure: sections mirror how the team works — daily stock checks,
// the procure-to-receive flow, the paper plan, reference masters, reporting.
const NAV = [
  { section: 'Overview', links: [['/', 'Dashboard'], ['/stock', 'Stock']] },
  {
    section: 'Procure',
    links: [
      ['/requisitions', 'Requisitions'],
      ['/approvals', 'Approvals'],
      ['/purchase-orders', 'Purchase Orders'],
      ['/shipments', 'Shipping'],
    ],
  },
  {
    section: 'Plan',
    links: [
      ['/paper-planning', 'Order Page'],
      ['/forecasts', 'Forecasts'],
      ['/planning', 'Planning'],
    ],
  },
  { section: 'Masters', links: [['/vendors', 'Vendors'], ['/customers', 'Customers']] },
  { section: 'Insight', links: [['/analytics', 'Analytics'], ['/reports', 'Reports']] },
]

export function pageTitle(pathname, nav = NAV) {
  if (pathname.startsWith('/admin')) return 'Admin'
  let best = ['', 'Dashboard']
  for (const { links } of nav) {
    for (const [to, label] of links) {
      if (to === '/' ? pathname === '/' : pathname.startsWith(to)) {
        if (to.length > best[0].length) best = [to, label]
      }
    }
  }
  return best[1]
}

export default function App() {
  // The auth provider (Clerk vs local break-glass) is chosen in main.jsx.
  return <Root />
}

function Root() {
  const { user, loading } = useAuth()
  if (loading) return <div className="center muted">Loading…</div>
  if (!user) return <Login />
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  )
}

function Shell() {
  const { user, logout } = useAuth()
  const { pathname } = useLocation()
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo">◆</span> Golden Procurement
          <small>Golden Manufacturers</small>
        </div>
        <nav className="side-nav">
          {NAV.map(({ section, links }) => (
            <React.Fragment key={section}>
              <span className="nav-section">{section}</span>
              {links.map(([to, label]) => (
                <NavLink key={to} to={to} end={to === '/'}>{label}</NavLink>
              ))}
            </React.Fragment>
          ))}
          {canAdmin(user) && (
            <>
              <span className="nav-section">System</span>
              <NavLink to="/admin">Admin</NavLink>
            </>
          )}
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          <span className="topbar-title">{pageTitle(pathname)}</span>
          <div className="user">
            <span className="role-pill">{user.role}</span>
            <span>{user.name || user.email}</span>
            <button className="btn-link" onClick={logout}>Sign out</button>
          </div>
        </header>
        <main className="content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/stock" element={<Stock />} />
            <Route path="/stock/:sku" element={<StockDetail />} />
            <Route path="/requisitions" element={<Requisitions />} />
            <Route path="/requisitions/:id" element={<RequisitionDetail />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/purchase-orders" element={<PurchaseOrders />} />
            <Route path="/purchase-orders/:id" element={<PurchaseOrderDetail />} />
            <Route path="/planning" element={<Planning />} />
            <Route path="/paper-planning" element={<PaperPlanning />} />
            <Route path="/forecasts" element={<Forecasts />} />
            <Route path="/customers" element={<Customers />} />
            <Route path="/vendors" element={<Vendors />} />
            <Route path="/shipments" element={<Shipments />} />
            <Route path="/analytics" element={<Analytics />} />
            <Route path="/reports" element={<Reports />} />
            <Route path="/admin" element={canAdmin(user) ? <Admin /> : <Navigate to="/" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
        <footer className="footer">
          Golden Procurement · requisitions → approval → BC purchase orders → receiving · paper planned to 3-month cover
        </footer>
      </div>
    </div>
  )
}
