import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/context'

// Gate for authed routes: wait while the stored token is validated, then either
// render the route or bounce to /login.
export default function ProtectedRoute() {
  const { status } = useAuth()

  if (status === 'loading') {
    return <div className="grid h-full place-items-center text-slate-500">Loading…</div>
  }
  if (status === 'anon') {
    return <Navigate to="/login" replace />
  }
  return <Outlet />
}
