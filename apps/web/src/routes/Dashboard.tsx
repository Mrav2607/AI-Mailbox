import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../auth/context'
import { getOverview } from '../lib/api'

// Placeholder home for the authed app. Proves the full authed data path works
// (token -> /analytics/overview). The triage list view is the next phase.
export default function Dashboard() {
  const { user, logout } = useAuth()
  // Scope the cache to the user so one account's stats can't surface in another
  // session (the cache outlives a logout/login within the same tab).
  const overview = useQuery({
    queryKey: ['overview', user?.id],
    queryFn: getOverview,
    enabled: !!user,
  })

  return (
    <div className="mx-auto max-w-3xl p-6">
      <header className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">AI Mailbox</h1>
          <p className="text-sm text-slate-500">{user?.email}</p>
        </div>
        <button
          onClick={logout}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
        >
          Sign out
        </button>
      </header>

      <section className="rounded-xl border border-slate-200 bg-white p-5">
        <h2 className="mb-3 text-sm font-medium text-slate-700">Overview</h2>
        {overview.isPending && <p className="text-sm text-slate-500">Loading…</p>}
        {overview.isError && (
          <p className="text-sm text-red-600">Could not load overview.</p>
        )}
        {overview.data && (
          <dl className="grid grid-cols-3 gap-4">
            <Stat label="Threads" value={overview.data.summary.threads} />
            <Stat label="Messages" value={overview.data.summary.messages} />
            <Stat label="Classified" value={overview.data.summary.classified} />
          </dl>
        )}
      </section>

      <p className="mt-6 text-sm text-slate-400">Triage view coming next.</p>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg bg-slate-50 p-4">
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold text-slate-900">{value}</dd>
    </div>
  )
}
