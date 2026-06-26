// Thin fetch wrapper around the AI Mailbox API: attaches the bearer token,
// parses the JSON error envelope, and surfaces a typed ApiError.

import type { AnalyticsOverview, DemoLoginResponse, User } from './types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api/v1'
const TOKEN_KEY = 'ai_mailbox_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
  // Most calls need the token; login does not.
  auth?: boolean
}

const REQUEST_TIMEOUT_MS = 15_000

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, auth = true } = options
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (auth) {
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
  }

  // Abort hung requests and funnel transport failures into ApiError too, so
  // every caller sees one consistent error shape.
  const controller = new AbortController()
  const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)

  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    })
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === 'AbortError'
    throw new ApiError(0, timedOut ? 'Request timed out' : 'Network error. Is the API running?')
  } finally {
    window.clearTimeout(timeoutId)
  }

  if (!res.ok) {
    // The API returns { detail, error_id? }; fall back to the status text.
    let detail = res.statusText
    try {
      const data = await res.json()
      if (data?.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new ApiError(res.status, detail)
  }

  // 204 / empty bodies
  if (res.status === 204) return undefined as T
  const text = await res.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError(res.status, 'Invalid JSON response from API')
  }
}

// --- Auth ---

export function demoLogin(email: string): Promise<DemoLoginResponse> {
  return apiFetch<DemoLoginResponse>('/auth/demo-login', {
    method: 'POST',
    body: { email },
    auth: false,
  })
}

export function getMe(): Promise<User> {
  return apiFetch<User>('/auth/me')
}

// --- Data ---

export function getOverview(): Promise<AnalyticsOverview> {
  return apiFetch<AnalyticsOverview>('/analytics/overview')
}
