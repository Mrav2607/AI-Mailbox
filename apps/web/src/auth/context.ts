// Auth context + hook, kept separate from the AuthProvider component so the
// file exports only non-components (keeps React Fast Refresh happy).

import { createContext, useContext } from 'react'
import type { User } from '../lib/types'

export type AuthStatus = 'loading' | 'authed' | 'anon'

export interface AuthContextValue {
  status: AuthStatus
  user: User | null
  login: (token: string, user: User) => void
  logout: () => void
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
