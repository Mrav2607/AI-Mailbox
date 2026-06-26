// AuthProvider: the bearer token in localStorage is the source of truth; on
// load we validate it against /auth/me and drop it if it's stale.

import { useEffect, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { clearToken, getMe, getToken, setToken } from '../lib/api'
import type { User } from '../lib/types'
import { AuthContext, type AuthStatus } from './context'

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<AuthStatus>('loading')
  const [user, setUser] = useState<User | null>(null)

  useEffect(() => {
    if (!getToken()) {
      setStatus('anon')
      return
    }
    // Validate the stored token and hydrate the user.
    getMe()
      .then((me) => {
        setUser(me)
        setStatus('authed')
      })
      .catch(() => {
        clearToken()
        setUser(null)
        setStatus('anon')
      })
  }, [])

  function login(token: string, nextUser: User) {
    setToken(token)
    setUser(nextUser)
    setStatus('authed')
  }

  function logout() {
    clearToken()
    setUser(null)
    setStatus('anon')
    // Drop all cached server state so the next user never sees stale data.
    queryClient.clear()
  }

  return (
    <AuthContext.Provider value={{ status, user, login, logout }}>{children}</AuthContext.Provider>
  )
}
