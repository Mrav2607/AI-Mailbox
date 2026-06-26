// Shapes returned by the AI Mailbox API. Hand-written for now; once the typed
// data layer lands these can be generated from the API's /openapi.json.

export interface User {
  id: string
  email: string
  display_name: string | null
}

export interface DemoLoginResponse {
  access_token: string
  token_type: string
  user: User
}

export interface AnalyticsOverview {
  summary: {
    threads: number
    messages: number
    classified: number
  }
}
