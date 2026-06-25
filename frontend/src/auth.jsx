import React, { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api } from './api.js'

const AuthCtx = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)
  const [providers, setProviders] = useState({ entra: false, admin_login: true })

  const refresh = useCallback(async () => {
    try {
      setUser(await api.get('/api/me'))
    } catch (_) {
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    api.get('/auth/providers').then(setProviders).catch(() => {})
    refresh()
  }, [refresh])

  const logout = useCallback(async () => {
    try {
      await api.post('/auth/logout')
    } finally {
      setUser(null)
    }
  }, [])

  return (
    <AuthCtx.Provider value={{ user, loading, providers, refresh, logout, setUser }}>
      {children}
    </AuthCtx.Provider>
  )
}

export function useAuth() {
  return useContext(AuthCtx)
}
