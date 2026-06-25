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
      <header className="topbar">
        <div className="brand">
          <span className="logo">◆</span> Golden Procurement
        </div>
        <nav className="nav">
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/stock">Stock</NavLink>
          <NavLink to="/requisitions">Requisitions</NavLink>
          <NavLink to="/approvals">Approvals</NavLink>
        </nav>
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
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <footer className="footer muted">
        Phase 2 · Requisitions &amp; approvals · estimated amounts use BC selling price
      </footer>
    </div>
  )
}
