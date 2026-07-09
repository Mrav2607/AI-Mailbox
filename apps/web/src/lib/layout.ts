// Console layout model: the three panes arrange along two independent axes,
// giving six pre-configured layouts. `sidebar` picks which edge the bucket
// sidebar hugs; `reading` puts the detail pane beside or below the list.
export type SidebarSide = "left" | "right";
export type ReadingSide = "right" | "bottom" | "left";
export type Arrangement = { sidebar: SidebarSide; reading: ReadingSide };

// Panel-id -> flex-grow maps, exactly the shape react-resizable-panels emits
// and accepts, so persisted sizes round-trip untouched.
export type PaneLayout = Record<string, number>;
// Keyed by group: "outer" (sidebar vs main, shared by both sidebar sides
// since layouts are id-keyed) | "inner:h" | "inner:v" (list vs detail,
// remembered separately per orientation).
export type PaneSizes = Record<string, PaneLayout>;

export type DragSource = "sidebar" | "detail";
export type DropZone =
  | { kind: "sidebar"; side: SidebarSide }
  | { kind: "reading"; side: ReadingSide };

export const DEFAULT_ARRANGEMENT: Arrangement = {
  sidebar: "left",
  reading: "right",
};

// Picker order: sidebar-left row first, reading right/bottom/left within each.
export const ALL_ARRANGEMENTS: Arrangement[] = [
  { sidebar: "left", reading: "right" },
  { sidebar: "left", reading: "bottom" },
  { sidebar: "left", reading: "left" },
  { sidebar: "right", reading: "right" },
  { sidebar: "right", reading: "bottom" },
  { sidebar: "right", reading: "left" },
];

export function arrangementLabel(a: Arrangement): string {
  return `buckets ${a.sidebar} · reading pane ${a.reading}`;
}

export function sameArrangement(a: Arrangement, b: Arrangement): boolean {
  return a.sidebar === b.sidebar && a.reading === b.reading;
}

// Drop-zone geometry as fractions of the layout container. Shared by the
// hit-test and the overlay rendering so the two can't drift apart.
export const SIDEBAR_ZONE_W = 0.25;
export const READING_ZONE_W = 0.3;
export const READING_ZONE_H = 0.3;
export const READING_BOTTOM_INSET = 0.2; // bottom band spans x 20%..80%

export function hitTestZone(
  source: DragSource,
  x: number,
  y: number,
  rect: DOMRect,
): DropZone | null {
  const fx = (x - rect.left) / rect.width;
  const fy = (y - rect.top) / rect.height;
  if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return null;
  if (source === "sidebar") {
    if (fx < SIDEBAR_ZONE_W) return { kind: "sidebar", side: "left" };
    if (fx > 1 - SIDEBAR_ZONE_W) return { kind: "sidebar", side: "right" };
    return null;
  }
  // The bottom band wins where it overlaps the side strips — it's the
  // hardest zone to reach otherwise.
  if (
    fy > 1 - READING_ZONE_H &&
    fx > READING_BOTTOM_INSET &&
    fx < 1 - READING_BOTTOM_INSET
  ) {
    return { kind: "reading", side: "bottom" };
  }
  if (fx < READING_ZONE_W) return { kind: "reading", side: "left" };
  if (fx > 1 - READING_ZONE_W) return { kind: "reading", side: "right" };
  return null;
}

// ---- persisted UI state ----------------------------------------------------
// Extends the old `{ sidebar, detail }` blob under the same key, so existing
// users' panel toggles carry over untouched.

export type UiState = {
  sidebar: boolean; // bucket sidebar visible
  detail: boolean; // detail pane visible
  arrangement: Arrangement;
  paneSizes: PaneSizes;
};

export const UI_KEY = "ai_mailbox_ui";

export const DEFAULT_UI: UiState = {
  sidebar: true,
  detail: true,
  arrangement: DEFAULT_ARRANGEMENT,
  paneSizes: {},
};

function isPaneLayout(v: unknown): v is PaneLayout {
  return (
    typeof v === "object" &&
    v !== null &&
    Object.values(v).every((n) => typeof n === "number" && Number.isFinite(n))
  );
}

// Field-by-field validation: a malformed field (old blob, hand-edited
// storage) falls back to its default without poisoning the rest.
export function loadUi(): UiState {
  if (typeof window === "undefined") return DEFAULT_UI;
  let raw: unknown;
  try {
    raw = JSON.parse(window.localStorage.getItem(UI_KEY) ?? "");
  } catch {
    return DEFAULT_UI;
  }
  if (typeof raw !== "object" || raw === null) return DEFAULT_UI;
  const o = raw as Record<string, unknown>;

  const arr =
    typeof o.arrangement === "object" && o.arrangement !== null
      ? (o.arrangement as Record<string, unknown>)
      : {};
  const arrangement: Arrangement = {
    sidebar:
      arr.sidebar === "left" || arr.sidebar === "right"
        ? arr.sidebar
        : DEFAULT_ARRANGEMENT.sidebar,
    reading:
      arr.reading === "right" || arr.reading === "bottom" || arr.reading === "left"
        ? arr.reading
        : DEFAULT_ARRANGEMENT.reading,
  };

  const paneSizes: PaneSizes = {};
  if (typeof o.paneSizes === "object" && o.paneSizes !== null) {
    for (const [k, v] of Object.entries(o.paneSizes)) {
      if (isPaneLayout(v)) paneSizes[k] = v;
    }
  }

  return {
    sidebar: typeof o.sidebar === "boolean" ? o.sidebar : DEFAULT_UI.sidebar,
    detail: typeof o.detail === "boolean" ? o.detail : DEFAULT_UI.detail,
    arrangement,
    paneSizes,
  };
}
