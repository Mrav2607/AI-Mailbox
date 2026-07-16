export type TourPrecondition =
  | "show-sidebar"
  | "select-needs-reply"
  | "show-detail"
  | "show-prediction"
  | "open-ingest";

export type TourFallback =
  | { kind: "center" }
  | { kind: "skip" }
  | { kind: "target"; selector: string; otherwise: "center" | "skip" };

export interface TourStepDefinition {
  slug: string;
  title: string;
  body: string;
  emptyBody?: string;
  target: string | null;
  preferredTarget?: string;
  preconditions: TourPrecondition[];
  fallback: TourFallback;
  placement: "auto";
}

export type TourTargetResolution =
  | { kind: "target"; selector: string }
  | { kind: "center" }
  | { kind: "skip" };

export const TOUR_STEPS: TourStepDefinition[] = [
  {
    slug: "welcome",
    title: "Welcome to CortexMail",
    body: "Ingest brings in Gmail threads, while sync keeps an existing mailbox current. This walkthrough shows where everything lands.",
    target: '[data-tour="topbar-sync"]',
    preconditions: [],
    fallback: { kind: "center" },
    placement: "auto",
  },
  {
    slug: "buckets",
    title: "Buckets organize your mail",
    body: "CortexMail classifies each conversation into a focused bucket, with counts showing what needs your attention.",
    target: '[data-tour="bucket-sidebar"]',
    preconditions: ["show-sidebar"],
    fallback: { kind: "skip" },
    placement: "auto",
  },
  {
    slug: "focus-bucket",
    title: "Focus a bucket",
    body: "Needs reply is the default starting point. Select any bucket here, or use the number keys for a faster jump.",
    target: '[data-tour="bucket-needs_reply"]',
    preconditions: ["show-sidebar", "select-needs-reply"],
    fallback: {
      kind: "target",
      selector: '[data-tour="bucket-sidebar"]',
      otherwise: "skip",
    },
    placement: "auto",
  },
  {
    slug: "threads",
    title: "Your threads",
    body: "Threads in the selected bucket appear here. Move through them with j and k, then press Enter to open the focused conversation.",
    emptyBody: "Once you ingest Gmail, threads in the selected bucket land here. You can move through them with j and k.",
    target: '[data-tour="thread-list"]',
    preconditions: [],
    fallback: { kind: "center" },
    placement: "auto",
  },
  {
    slug: "search",
    title: "Search & filter",
    body: "Typing filters the loaded bucket immediately. Press Enter to search the whole mailbox across every bucket.",
    target: '[data-tour="search"]',
    preconditions: [],
    fallback: { kind: "skip" },
    placement: "auto",
  },
  {
    slug: "sort",
    title: "Sort",
    body: "Cycle between recent-first and confidence order here, or press c while the console has focus.",
    target: '[data-tour="sort"]',
    preconditions: [],
    fallback: { kind: "skip" },
    placement: "auto",
  },
  {
    slug: "reading-pane",
    title: "Reading pane",
    body: "The selected conversation opens here, with safe message previews and thread actions in the pane chrome.",
    emptyBody: "A selected conversation opens here after ingest. The pane is ready even before your first thread arrives.",
    target: '[data-tour="detail-pane"]',
    preconditions: ["show-detail"],
    fallback: { kind: "skip" },
    placement: "auto",
  },
  {
    slug: "predictions",
    title: "Predictions & reclassify",
    body: "The model's label and confidence appear here. Reclassifying a thread corrects the mailbox and supplies better feedback for future models.",
    target: '[data-tour="prediction"]',
    preconditions: ["show-detail", "show-prediction"],
    fallback: { kind: "skip" },
    placement: "auto",
  },
  {
    slug: "commands",
    title: "Command palette & shortcuts",
    body: "Press Cmd+K or Ctrl+K for every console command. Press ? for the complete keyboard shortcut sheet.",
    target: null,
    preconditions: [],
    fallback: { kind: "center" },
    placement: "auto",
  },
  {
    slug: "layout-theme",
    title: "Layout & theme",
    body: "Move the sidebar and reading pane with Layout. The adjacent theme control cycles system, light, and dark modes.",
    target: '[data-tour="layout"]',
    preconditions: [],
    fallback: { kind: "center" },
    placement: "auto",
  },
  {
    slug: "ingest",
    title: "Ingest your mail",
    body: "Choose how many Gmail threads to bring in and whether to classify them immediately. This is the starting point for a new mailbox.",
    target: '[data-tour="topbar-sync"]',
    preferredTarget: '[data-tour="ingest-panel"]',
    preconditions: ["open-ingest"],
    fallback: { kind: "center" },
    placement: "auto",
  },
];

export function resolveTourTarget(
  step: TourStepDefinition,
  targetExists: (selector: string) => boolean,
): TourTargetResolution {
  if (step.preferredTarget && targetExists(step.preferredTarget)) {
    return { kind: "target", selector: step.preferredTarget };
  }
  if (step.target && targetExists(step.target)) {
    return { kind: "target", selector: step.target };
  }

  if (step.fallback.kind === "target") {
    if (targetExists(step.fallback.selector)) {
      return { kind: "target", selector: step.fallback.selector };
    }
    return { kind: step.fallback.otherwise };
  }

  return { kind: step.fallback.kind };
}
