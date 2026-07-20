import React, { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { useAuth as useClerkAuth, useUser } from '@clerk/clerk-react'
import { api, setTokenProvider } from './api.js'

// One context shape, two providers. main.jsx picks:
//   • ClerkAuthProvider — cloud auth (Clerk + federated Microsoft). Identity comes
//     from Clerk; role/limit come from GET /api/me (the backend maps the Clerk
//     token's role claim to a local role code).
//   • LocalAuthProvider — no Clerk configured: the break-glass admin session-cookie
//     login (and legacy Entra OIDC). Same behaviour the app shipped with.
// Every page consumes useAuth(); it never needs to know which provider is active.
const AuthCtx = createContext(null)

export function useAuth() {
  return useContext(AuthCtx)
}

const JWT_TEMPLATE = import.meta.env.VITE_CLERK_JWT_TEMPLATE || undefined

export function LocalAuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)
  const [providers, setProviders] = useState({ clerk: false, entra: false, admin_login: true })

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
    setTokenProvider(null)                 // no Bearer token in local mode
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

export function ClerkAuthProvider({ children }) {
  const { isLoaded, isSignedIn, getToken, signOut } = useClerkAuth()
  const { user: clerkUser } = useUser()
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)
  const providers = { clerk: true, entra: false, admin_login: false }

  // Attach the Clerk session JWT to every api call. Uses a JWT template when one
  // is configured (recommended: adds email/name/role claims), else the default token.
  useEffect(() => {
    setTokenProvider(async () =>
      isSignedIn ? await getToken(JWT_TEMPLATE ? { template: JWT_TEMPLATE } : undefined) : null,
    )
  }, [isSignedIn, getToken])

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
    if (!isLoaded) return
    if (!isSignedIn) {
      setUser(null)
      setLoading(false)
      return
    }
    setLoading(true)
    refresh()
  }, [isLoaded, isSignedIn, clerkUser?.id, refresh])

  const logout = useCallback(async () => {
    setUser(null)
    await signOut()
  }, [signOut])

  return (
    <AuthCtx.Provider value={{ user, loading, providers, refresh, logout, setUser }}>
      {children}
    </AuthCtx.Provider>
  )
}
