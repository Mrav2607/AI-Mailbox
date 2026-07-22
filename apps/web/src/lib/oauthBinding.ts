export type OauthBinding = {
  mode: "login" | "connect";
  state: string;
  startedAt: number;
};

const OAUTH_BINDING_KEY = "oauth_binding";
const MAX_BINDING_AGE_MS = 15 * 60 * 1000;

/**
 * Bind an OAuth callback to the browser that started it and retain which
 * callback endpoint that browser is allowed to use.
 */
export function saveOauthBinding(binding: OauthBinding) {
  try {
    window.sessionStorage.setItem(OAUTH_BINDING_KEY, JSON.stringify(binding));
  } catch {
    // OAuth still has server-side state validation when browser storage is unavailable.
  }
}

export function takeOauthBinding(): OauthBinding | null {
  let raw: string | null;
  try {
    raw = window.sessionStorage.getItem(OAUTH_BINDING_KEY);
    window.sessionStorage.removeItem(OAUTH_BINDING_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;

  try {
    const binding: unknown = JSON.parse(raw);
    if (
      !binding ||
      typeof binding !== "object" ||
      !((binding as OauthBinding).mode === "login" || (binding as OauthBinding).mode === "connect") ||
      typeof (binding as OauthBinding).state !== "string" ||
      !(binding as OauthBinding).state ||
      typeof (binding as OauthBinding).startedAt !== "number" ||
      !Number.isFinite((binding as OauthBinding).startedAt) ||
      Date.now() - (binding as OauthBinding).startedAt > MAX_BINDING_AGE_MS
    ) {
      return null;
    }
    return binding as OauthBinding;
  } catch {
    return null;
  }
}

export function extractStateFromAuthUrl(authUrl: string): string | null {
  try {
    return new URL(authUrl).searchParams.get("state");
  } catch {
    return null;
  }
}
