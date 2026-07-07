import React from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth.jsx'
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
import Shipments from './pages/Shipments.jsx'
import Analytics from './pages/Analytics.jsx'
import Reports from './pages/Reports.jsx'
import Admin from './pages/Admin.jsx'
import { canAdmin } from './admin.js'

export default function App() {
  return (
    <AuthProvider>
      <Root />
    </AuthProvider>
  )
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
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo">◆</span> Golden Procurement
        </div>
        <nav className="side-nav">
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/stock">Stock</NavLink>
          <NavLink to="/requisitions">Requisitions</NavLink>
          <NavLink to="/approvals">Approvals</NavLink>
          <NavLink to="/purchase-orders">Purchase Orders</NavLink>
          <NavLink to="/planning">Planning</NavLink>
          <NavLink to="/paper-planning">Order Page</NavLink>
          <NavLink to="/forecasts">Forecasts</NavLink>
          <NavLink to="/customers">Customers</NavLink>
          <NavLink to="/shipments">Shipping</NavLink>
          <NavLink to="/analytics">Analytics</NavLink>
          <NavLink to="/reports">Reports</NavLink>
          {canAdmin(user) && <NavLink to="/admin">Admin</NavLink>}
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="user">
            <span className="role-pill">{user.role}</span>
            <span className="muted">{user.name || user.email}</span>
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
            <Route path="/shipments" element={<Shipments />} />
            <Route path="/analytics" element={<Analytics />} />
            <Route path="/reports" element={<Reports />} />
            <Route path="/admin" element={canAdmin(user) ? <Admin /> : <Navigate to="/" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
        <footer className="footer muted">
          Phase 6 · Paper planning · 3-month cover by grade & deckle; forecasts explode to KG, orders consolidate into 40 ft FCLs
        </footer>
      </div>
    </div>
  )
}
