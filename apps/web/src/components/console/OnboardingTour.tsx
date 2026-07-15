import { useEffect, useMemo, useState, type KeyboardEvent } from "react";
import {
  ACTIONS,
  EVENTS,
  Joyride,
  STATUS,
  type EventData,
  type Step,
  type TooltipRenderProps,
} from "react-joyride";

import { TOUR_STEPS, type TourTargetResolution } from "@/lib/tour-steps";

interface Props {
  run: boolean;
  stepIndex: number;
  targetResolution: TourTargetResolution | null;
  emptyThreadList: boolean;
  emptyDetail: boolean;
  onStepChange: (nextIndex: number, direction: 1 | -1) => void;
  onFinish: () => void;
  onSkip: () => void;
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() =>
    window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  return reduced;
}

function TourTooltip({
  backProps,
  closeProps,
  controls,
  index,
  isLastStep,
  primaryProps,
  size,
  skipProps,
  step,
  tooltipProps,
}: TooltipRenderProps) {
  const handleKeys = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowRight") {
      event.preventDefault();
      controls.next();
    } else if (event.key === "ArrowLeft" && index > 0) {
      event.preventDefault();
      controls.prev();
    }
  };

  return (
    <div
      {...tooltipProps}
      aria-label={`${String(step.title)}. Step ${index + 1} of ${size}.`}
      onKeyDown={handleKeys}
      onMouseDown={(event) => event.stopPropagation()}
      className="relative w-[min(380px,calc(100vw-30px))] rounded-md border border-border bg-[var(--color-panel)] p-4 text-foreground elevated"
    >
      <button
        type="button"
        aria-label={closeProps["aria-label"]}
        data-action={closeProps["data-action"]}
        title={closeProps.title}
        onClick={closeProps.onClick}
        className="absolute right-2 top-2 h-7 w-7 rounded text-lg leading-none text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span aria-hidden="true">×</span>
      </button>

      <div aria-live="polite" className="pr-7">
        <div className="font-mono text-[11px] text-muted-foreground">
          {index + 1} / {size}
        </div>
        <h2 className="mt-1 text-base font-semibold tracking-tight">{step.title}</h2>
        <div className="mt-2 text-sm leading-6 text-foreground/85">{step.content}</div>
      </div>

      <div className="mt-4 flex items-center gap-2">
        {!isLastStep && (
          <button
            type="button"
            aria-label={skipProps["aria-label"]}
            data-action={skipProps["data-action"]}
            title={skipProps.title}
            onClick={skipProps.onClick}
            className="mr-auto rounded px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            skip tour
          </button>
        )}
        {index > 0 && (
          <button
            type="button"
            aria-label={backProps["aria-label"]}
            data-action={backProps["data-action"]}
            title={backProps.title}
            onClick={backProps.onClick}
            className="rounded border border-border bg-background px-3 py-1.5 font-mono text-[11px] text-foreground hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            back
          </button>
        )}
        <button
          type="button"
          aria-label={primaryProps["aria-label"]}
          data-action={primaryProps["data-action"]}
          title={primaryProps.title}
          onClick={primaryProps.onClick}
          className="rounded border border-primary bg-primary px-3 py-1.5 font-mono text-[11px] text-primary-foreground hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {isLastStep ? "finish" : "next"}
        </button>
      </div>
    </div>
  );
}

export default function OnboardingTour({
  run,
  stepIndex,
  targetResolution,
  emptyThreadList,
  emptyDetail,
  onStepChange,
  onFinish,
  onSkip,
}: Props) {
  const reducedMotion = usePrefersReducedMotion();

  const steps = useMemo<Step[]>(
    () =>
      TOUR_STEPS.map((definition, index) => {
        const isCurrent = index === stepIndex;
        const resolution = isCurrent ? targetResolution : null;
        const content =
          (definition.slug === "threads" && emptyThreadList) ||
          (definition.slug === "reading-pane" && emptyDetail)
            ? (definition.emptyBody ?? definition.body)
            : definition.body;

        return {
          id: definition.slug,
          title: definition.title,
          content,
          target:
            resolution?.kind === "target"
              ? resolution.selector
              : (definition.target ?? "body"),
          placement: resolution?.kind === "center" ? "center" : definition.placement,
          skipBeacon: true,
        };
      }),
    [emptyDetail, emptyThreadList, stepIndex, targetResolution],
  );

  if (!run || !targetResolution || targetResolution.kind === "skip") return null;

  const handleJoyrideCallback = (data: EventData) => {
    if (data.type === EVENTS.TARGET_NOT_FOUND) {
      const direction = data.action === ACTIONS.PREV ? -1 : 1;
      onStepChange(data.index + direction, direction);
      return;
    }

    if (data.type === EVENTS.STEP_AFTER) {
      if (data.action === ACTIONS.PREV) {
        onStepChange(Math.max(0, data.index - 1), -1);
      } else if (data.action === ACTIONS.NEXT && data.index < TOUR_STEPS.length - 1) {
        onStepChange(data.index + 1, 1);
      } else if (data.action === ACTIONS.NEXT) {
        onFinish();
      }
      return;
    }

    if (data.type === EVENTS.TOUR_END) {
      if (data.status === STATUS.SKIPPED) onSkip();
      if (data.status === STATUS.FINISHED) onFinish();
    }
  };

  return (
    <Joyride
      run={run}
      stepIndex={stepIndex}
      steps={steps}
      continuous
      scrollToFirstStep
      onEvent={handleJoyrideCallback}
      tooltipComponent={TourTooltip}
      locale={{ last: "Finish", next: "Next", skip: "Skip tour" }}
      options={{
        arrowColor: "var(--color-panel)",
        backgroundColor: "var(--color-panel)",
        buttons: ["back", "close", "primary", "skip"],
        closeButtonAction: "skip",
        dismissKeyAction: false,
        overlayClickAction: false,
        primaryColor: "var(--color-primary)",
        scrollDuration: reducedMotion ? 0 : 300,
        showProgress: true,
        textColor: "var(--color-foreground)",
        zIndex: 1000,
      }}
      styles={{
        arrow: { color: "var(--color-panel)" },
        floater: { transition: reducedMotion ? "none" : "opacity 0.2s ease" },
        spotlight: {
          stroke: "var(--color-primary)",
          strokeWidth: 2,
        },
      }}
    />
  );
}
