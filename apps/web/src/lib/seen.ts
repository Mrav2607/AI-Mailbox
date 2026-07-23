// Per-user "threads I've opened" store for unread/seen dimming (feature 9).
// Purely local — no backend, no unread counts. Keyed separately from the
// auto-sync new-mail watermark (`ai_mailbox_seen:<userId>` in
// use-auto-sync.ts): that key tracks the newest mail acknowledged for the
// sync pill, this one tracks per-thread open state for row styling.

export const SEEN_CAP = 2000;
export const THREAD_SEEN_KEY_PREFIX = "ai_mailbox_thread_seen:";

const storageKey = (userId: string) => `${THREAD_SEEN_KEY_PREFIX}${userId}`;

function isStringPair(v: unknown): v is [string, string] {
  return (
    Array.isArray(v) &&
    v.length === 2 &&
    typeof v[0] === "string" &&
    typeof v[1] === "string"
  );
}

// Map insertion order doubles as LRU order (oldest-touched first), so a plain
// `for...of` over entries visits least-recently-seen first.
export function loadSeen(userId: string): Map<string, string> {
  if (typeof window === "undefined") return new Map();
  let raw: unknown;
  try {
    raw = JSON.parse(window.localStorage.getItem(storageKey(userId)) ?? "");
  } catch {
    return new Map();
  }
  if (!Array.isArray(raw)) return new Map();

  const map = new Map<string, string>();
  for (const entry of raw) {
    if (isStringPair(entry)) map.set(entry[0], entry[1]);
  }
  return map;
}

function persist(userId: string, map: Map<string, string>) {
  try {
    window.localStorage.setItem(
      storageKey(userId),
      JSON.stringify(Array.from(map.entries())),
    );
  } catch {
    // Storage unavailable/full — the in-memory map still works for this
    // session, it just won't survive a reload.
  }
}

export function markSeen(
  map: Map<string, string>,
  userId: string,
  threadId: string,
  lastMessageAt: string | null,
): void {
  // Delete-then-set moves the entry to the end, touching its LRU position.
  map.delete(threadId);
  map.set(threadId, lastMessageAt ?? "");

  while (map.size > SEEN_CAP) {
    const oldest = map.keys().next().value;
    if (oldest === undefined) break;
    map.delete(oldest);
  }

  persist(userId, map);
}

export function isUnseen(
  map: Map<string, string>,
  threadId: string,
  lastMessageAt: string | null,
): boolean {
  const stored = map.get(threadId);
  if (stored === undefined) return true;
  if (lastMessageAt === null) return false;
  return stored < lastMessageAt;
}
