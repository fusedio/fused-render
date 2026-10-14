// The App-Store action pill: Get → progress ring → Open, Update, Retry.
//
// FIXED WIDTH PER SIZE, whatever the state: a pill that grew from "Get" to
// "Open" or shrank to a ring would shove its row about mid-install, which is
// exactly the moment the reader is watching it. The label swaps inside a box
// that never moves.
//
// The pill sits INSIDE clickable things (a card that is an anchor, a list row),
// so it swallows its own click — `stopPropagation` and `preventDefault` — and
// the host never also navigates.
//
// Presentational only: the caller decides the state (marketplace.ts's
// `actionFor` for the catalog; the phase-2 file install page for a `.fused`).
import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { Check, RotateCw } from "lucide-react";

export type ActionPillState = "get" | "progress" | "open" | "update" | "retry" | "disabled";
export type ActionPillSize = "sm" | "md" | "lg";

export interface ActionPillProps {
  state: ActionPillState;
  /** 0..1 while `progress`; null/undefined = indeterminate (a spinning arc). */
  progress?: number | null;
  /** Override the state's default label ("Get", "Open", "Update", "Retry"). */
  label?: ReactNode;
  size?: ActionPillSize;
  onClick?: () => void;
  /** Tooltip; for `retry` this is where the error text goes. */
  title?: string;
  /** Accessible name when the visible label is not enough. */
  ariaLabel?: string;
  /** `filled` paints Get as a solid accent pill (a hero's one call to
   *  action); the default `tinted` Get is the quieter grid version. */
  emphasis?: "tinted" | "filled";
  className?: string;
}

const DEFAULT_LABEL: Record<ActionPillState, string> = {
  get: "Get",
  progress: "",
  open: "Open",
  update: "Update",
  retry: "Retry",
  disabled: "Get",
};

/** The determinate/indeterminate ring alone — reusable wherever a compact
 *  progress mark is wanted (an installed row's status cell). */
export function ProgressRing({ progress, size = 18, stroke = 2.25 }: { progress?: number | null; size?: number; stroke?: number }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const determinate = typeof progress === "number";
  const p = determinate ? Math.max(0.02, Math.min(1, progress as number)) : 0.28;
  return (
    <svg
      className={"as-ring" + (determinate ? "" : " is-indeterminate")}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      aria-hidden="true"
    >
      <circle className="as-ring-track" cx={size / 2} cy={size / 2} r={r} strokeWidth={stroke} fill="none" />
      <circle
        className="as-ring-fill"
        cx={size / 2}
        cy={size / 2}
        r={r}
        strokeWidth={stroke}
        fill="none"
        strokeLinecap="round"
        strokeDasharray={`${c * p} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
    </svg>
  );
}

export function ActionPill({
  state,
  progress,
  label,
  size = "sm",
  onClick,
  title,
  ariaLabel,
  emphasis = "tinted",
  className,
}: ActionPillProps) {
  // THE CHECK POP: progress → open plays a short check before settling on
  // "Open", so a finished install reads as a moment, not a silent swap.
  const prev = useRef(state);
  const [popped, setPopped] = useState(false);
  useEffect(() => {
    if (prev.current === "progress" && state === "open") {
      setPopped(true);
      const id = setTimeout(() => setPopped(false), 1100);
      prev.current = state;
      return () => clearTimeout(id);
    }
    prev.current = state;
  }, [state]);

  const pct = typeof progress === "number" ? Math.round(Math.max(0, Math.min(1, progress)) * 100) : null;
  const click = (e: MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    if (state === "progress" || state === "disabled") return;
    onClick?.();
  };
  const ringSize = size === "lg" ? 20 : 16;

  return (
    <button
      type="button"
      className={
        `as-pill as-pill-${size} is-${state}` + (emphasis === "filled" ? " is-filled" : "") + (popped ? " is-popped" : "") + (className ? " " + className : "")
      }
      onClick={click}
      disabled={state === "disabled"}
      aria-disabled={state === "progress" ? true : undefined}
      title={title}
      aria-label={
        ariaLabel ?? (state === "progress" ? (pct === null ? "Preparing install" : `Installing, ${pct}%`) : undefined)
      }
    >
      {state === "progress" ? (
        <span className="as-pill-inner">
          <ProgressRing progress={pct === null ? null : pct / 100} size={ringSize} />
          {size !== "sm" || pct !== null ? (
            <span className="as-pill-pct">{pct === null ? "Waiting" : `${pct}%`}</span>
          ) : null}
        </span>
      ) : (
        <span className="as-pill-inner" key={state + (popped ? "-pop" : "")}>
          {popped && state === "open" && <Check className="as-pill-check" size={size === "lg" ? 15 : 13} strokeWidth={3} aria-hidden="true" />}
          {state === "retry" && <RotateCw size={size === "lg" ? 14 : 12} strokeWidth={2.4} aria-hidden="true" />}
          <span>{label ?? DEFAULT_LABEL[state]}</span>
        </span>
      )}
    </button>
  );
}

export default ActionPill;
