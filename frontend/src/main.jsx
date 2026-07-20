import React from 'react'
import { createRoot } from 'react-dom/client'
import { ClerkProvider } from '@clerk/clerk-react'
import App from './App.jsx'
import { ClerkAuthProvider, LocalAuthProvider } from './auth.jsx'
import './index.css'

// Clerk is active when a publishable key is present at build time (Vercel). With
// no key — the backend's self-served fallback build, or local dev without Clerk —
// we mount the break-glass admin provider instead so the app still runs.
const clerkKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY

const tree = clerkKey ? (
  <ClerkProvider publishableKey={clerkKey} afterSignOutUrl="/">
    <ClerkAuthProvider>
      <App />
    </ClerkAuthProvider>
  </ClerkProvider>
) : (
  <LocalAuthProvider>
    <App />
  </LocalAuthProvider>
)

createRoot(document.getElementById('root')).render(
  <React.StrictMode>{tree}</React.StrictMode>,
)
