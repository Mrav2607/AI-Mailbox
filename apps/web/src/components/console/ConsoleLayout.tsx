import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import type {
  CSSProperties,
  PointerEvent as ReactPointerEvent,
  ReactNode,
} from "react";
import { Group, Panel, Separator } from "react-resizable-panels";
import { GripVertical, PanelLeftOpen, PanelRightOpen } from "lucide-react";
import {
  READING_BOTTOM_INSET,
  READING_ZONE_H,
  READING_ZONE_W,
  SIDEBAR_ZONE_W,
  hitTestZone,
  sameArrangement,
} from "@/lib/layout";
import type {
  Arrangement,
  DragSource,
  DropZone,
  PaneLayout,
  PaneSizes,
} from "@/lib/layout";

interface Props {
  arrangement: Arrangement;
  onArrangementChange: (a: Arrangement) => void;
  sidebarVisible: boolean;
  detailVisible: boolean;
  onExpandSidebar: () => void;
  paneSizes: PaneSizes;
  onPaneSizesChange: (key: string, layout: PaneLayout) => void;
  sidebar: ReactNode;
  list: ReactNode;
  detail: ReactNode;
}

type DragState = {
  source: DragSource;
  x: number;
  y: number;
  over: DropZone | null;
};

const LayoutDragContext = createContext<{
  startDrag: (source: DragSource, e: ReactPointerEvent) => void;
} | null>(null);

// Grip that pane headers render to start a move. It reaches the layout's drag
// machinery through context because the panes render inside ConsoleLayout.
export function PaneDragHandle({ source }: { source: DragSource }) {
  const ctx = useContext(LayoutDragContext);
  if (!ctx) return null;
  return (
    <button
      onPointerDown={(e) => ctx.startDrag(source, e)}
      aria-label={`Move ${source === "sidebar" ? "buckets" : "reading"} pane`}
      title="drag to move · layouts in top bar"
      className="cursor-grab active:cursor-grabbing touch-none text-muted-foreground hover:text-foreground transition-colors"
    >
      <GripVertical className="h-3.5 w-3.5" />
    </button>
  );
}

const pct = (f: number) => `${f * 100}%`;

type ZoneDef = { zone: DropZone; style: CSSProperties; label: string };

// Candidate drop targets for a drag, minus wherever the pane already is.
// Geometry comes from the same constants hitTestZone uses. The bottom band
// is listed last so it paints over the side strips where they overlap,
// matching the hit-test's precedence.
function zonesFor(source: DragSource, current: Arrangement): ZoneDef[] {
  const defs: ZoneDef[] =
    source === "sidebar"
      ? [
          {
            zone: { kind: "sidebar", side: "left" },
            style: { top: 0, bottom: 0, left: 0, width: pct(SIDEBAR_ZONE_W) },
            label: "buckets → left",
          },
          {
            zone: { kind: "sidebar", side: "right" },
            style: { top: 0, bottom: 0, right: 0, width: pct(SIDEBAR_ZONE_W) },
            label: "buckets → right",
          },
        ]
      : [
          {
            zone: { kind: "reading", side: "left" },
            style: { top: 0, bottom: 0, left: 0, width: pct(READING_ZONE_W) },
            label: "reading → left",
          },
          {
            zone: { kind: "reading", side: "right" },
            style: { top: 0, bottom: 0, right: 0, width: pct(READING_ZONE_W) },
            label: "reading → right",
          },
          {
            zone: { kind: "reading", side: "bottom" },
            style: {
              bottom: 0,
              left: pct(READING_BOTTOM_INSET),
              right: pct(READING_BOTTOM_INSET),
              height: pct(READING_ZONE_H),
            },
            label: "reading → bottom",
          },
        ];
  return defs.filter((d) =>
    d.zone.kind === "sidebar"
      ? d.zone.side !== current.sidebar
      : d.zone.side !== current.reading,
  );
}

const zonesMatch = (a: DropZone, b: DropZone) =>
  a.kind === b.kind && a.side === b.side;

// Panel classNames land on the library's inner fill div; making it a column
// flexbox lets pane roots size with flex-1/h-full. overflow:hidden overrides
// the library's inline `overflow: auto` so panes own their scroll regions.
const paneClass = "flex flex-col min-w-0 min-h-0";
const paneStyle: CSSProperties = { overflow: "hidden" };

export function ConsoleLayout({
  arrangement,
  onArrangementChange,
  sidebarVisible,
  detailVisible,
  onExpandSidebar,
  paneSizes,
  onPaneSizesChange,
  sidebar,
  list,
  detail,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<DragState | null>(null);

  // Refs so the window-level drag listeners always see current values.
  const arrangementRef = useRef(arrangement);
  arrangementRef.current = arrangement;
  const cleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => cleanupRef.current?.(), []);

  const isVert = arrangement.reading === "bottom";
  const innerKey = isVert ? "inner:v" : "inner:h";

  const startDrag = useCallback(
    (source: DragSource, e: ReactPointerEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      cleanupRef.current?.();
      const startX = e.clientX;
      const startY = e.clientY;
      let live = false;

      const zoneAt = (ev: PointerEvent) => {
        const rect = containerRef.current?.getBoundingClientRect();
        return rect ? hitTestZone(source, ev.clientX, ev.clientY, rect) : null;
      };
      const cleanup = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", cleanup);
        window.removeEventListener("keydown", onKey, true);
        document.body.classList.remove("pane-dragging");
        cleanupRef.current = null;
        setDrag(null);
      };
      const onMove = (ev: PointerEvent) => {
        // A sub-4px wiggle is a click, not a drag — no overlay flash.
        if (!live) {
          if (Math.hypot(ev.clientX - startX, ev.clientY - startY) < 4) return;
          live = true;
          document.body.classList.add("pane-dragging");
        }
        setDrag({ source, x: ev.clientX, y: ev.clientY, over: zoneAt(ev) });
      };
      const onUp = (ev: PointerEvent) => {
        const zone = live ? zoneAt(ev) : null;
        cleanup();
        if (!zone) return;
        const a = arrangementRef.current;
        const next: Arrangement =
          zone.kind === "sidebar"
            ? { ...a, sidebar: zone.side }
            : { ...a, reading: zone.side };
        if (!sameArrangement(a, next)) onArrangementChange(next);
      };
      const onKey = (ev: KeyboardEvent) => {
        if (ev.key === "Escape") {
          ev.stopPropagation();
          cleanup();
        }
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", cleanup);
      window.addEventListener("keydown", onKey, true);
      cleanupRef.current = cleanup;
    },
    [onArrangementChange],
  );

  // Pixel minimums keep every pane usable; their sum (160+260+260) stays
  // under a 700px window so the row never overflows sideways.
  const listPanel = (
    <Panel key="list" id="list" minSize={260} className={paneClass} style={paneStyle}>
      {list}
    </Panel>
  );
  // A Panel re-added after being toggled away mounts at its defaultSize (the
  // library only knows sizes of live panels), so the persisted size doubles
  // as the default to bring panes back exactly where the user left them.
  const savedInner = paneSizes[innerKey]?.detail;
  const detailPanel = detailVisible ? (
    <Panel
      key="detail"
      id="detail"
      minSize={isVert ? 180 : 260}
      maxSize="70%"
      defaultSize={savedInner != null ? `${savedInner}%` : isVert ? "45%" : "47%"}
      className={`${paneClass} bg-[var(--color-panel)]/30`}
      style={paneStyle}
    >
      {detail}
    </Panel>
  ) : null;
  const innerSep = (
    <Separator key="inner-sep" id="inner-sep" className={isVert ? "h-px" : "w-px"} />
  );
  const innerChildren = !detailPanel
    ? [listPanel]
    : arrangement.reading === "left"
      ? [detailPanel, innerSep, listPanel]
      : [listPanel, innerSep, detailPanel];

  // The outer solver can't see the inner group's minimums, so the main panel
  // declares them: room for list + detail side by side, or just the list.
  const mainMin = detailPanel && !isVert ? 260 + 260 + 1 : 260;
  const mainPanel = (
    <Panel key="main" id="main" minSize={mainMin} className={paneClass} style={paneStyle}>
      {/* key remounts the inner group when its orientation flips — the
          library doesn't support live orientation changes. */}
      <Group
        key={innerKey}
        id="inner"
        orientation={isVert ? "vertical" : "horizontal"}
        // A persisted layout only fits when both panels are present.
        defaultLayout={detailVisible ? paneSizes[innerKey] : undefined}
        onLayoutChanged={(l, meta) => {
          if (meta.isUserInteraction) onPaneSizesChange(innerKey, l);
        }}
        className="flex-1 min-w-0 min-h-0"
      >
        {innerChildren}
      </Group>
    </Panel>
  );
  const savedOuter = paneSizes["outer"]?.sidebar;
  const sidebarPanel = sidebarVisible ? (
    <Panel
      key="sidebar"
      id="sidebar"
      minSize={160}
      maxSize={320}
      defaultSize={savedOuter != null ? `${savedOuter}%` : 208}
      groupResizeBehavior="preserve-pixel-size"
      className={paneClass}
      style={paneStyle}
    >
      {sidebar}
    </Panel>
  ) : null;
  const outerSep = <Separator key="outer-sep" id="outer-sep" className="w-px" />;
  const outerChildren = !sidebarPanel
    ? [mainPanel]
    : arrangement.sidebar === "left"
      ? [sidebarPanel, outerSep, mainPanel]
      : [mainPanel, outerSep, sidebarPanel];

  const rail = !sidebarVisible && (
    <button
      onClick={onExpandSidebar}
      aria-label="Show buckets"
      title="Show buckets ( [ )"
      className={`w-8 shrink-0 ${
        arrangement.sidebar === "left" ? "border-r" : "border-l"
      } border-border bg-[var(--color-panel)] flex items-start justify-center pt-2.5 text-muted-foreground hover:text-foreground cursor-pointer transition-colors`}
    >
      {arrangement.sidebar === "left" ? (
        <PanelLeftOpen className="h-4 w-4" />
      ) : (
        <PanelRightOpen className="h-4 w-4" />
      )}
    </button>
  );

  return (
    <LayoutDragContext.Provider value={{ startDrag }}>
      <div ref={containerRef} className="relative flex-1 min-h-0 flex">
        {arrangement.sidebar === "left" && rail}
        <Group
          id="outer"
          orientation="horizontal"
          defaultLayout={sidebarVisible ? paneSizes["outer"] : undefined}
          onLayoutChanged={(l, meta) => {
            if (meta.isUserInteraction) onPaneSizesChange("outer", l);
          }}
          className="flex-1 min-w-0 min-h-0"
        >
          {outerChildren}
        </Group>
        {arrangement.sidebar === "right" && rail}

        {drag && (
          <div className="pointer-events-none absolute inset-0 z-40">
            {zonesFor(drag.source, arrangement).map((d) => {
              const hot = drag.over && zonesMatch(drag.over, d.zone);
              return (
                <div
                  key={d.label}
                  style={d.style}
                  className={`absolute rounded border flex items-center justify-center transition-colors ${
                    hot
                      ? "border-primary/70 bg-primary/20 phosphor"
                      : "border-primary/40 bg-primary/10"
                  }`}
                >
                  <span className="font-mono text-[11px] text-primary bg-[var(--color-panel)]/80 px-2 py-0.5 rounded">
                    {d.label}
                  </span>
                </div>
              );
            })}
          </div>
        )}
        {drag && (
          <div
            className="fixed z-50 pointer-events-none px-2 py-1 rounded border border-primary/50 bg-[var(--color-panel-hi)] elevated font-mono text-[11px] text-primary"
            style={{ left: drag.x + 12, top: drag.y + 12 }}
          >
            {drag.source === "sidebar" ? "buckets" : "reading pane"}
          </div>
        )}
      </div>
    </LayoutDragContext.Provider>
  );
}
