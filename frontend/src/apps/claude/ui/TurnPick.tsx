// Breakout pick mode on a turn: each exchange (a prompt plus its answers) is
// one click target, with a check circle in the left lane of the prompt.
import type { KeyboardEvent, MouseEvent } from "react";
import { Circle, CircleCheck } from "lucide-react";

export interface TurnPick {
  /** The turn's exchange is in the selection. */
  checked: boolean;
  /** The exchange is still streaming or pending: not pickable. */
  locked: boolean;
  /** This is the exchange's prompt turn: it carries the check circle. */
  lead: boolean;
  /** Names the prompt for the check's accessible label. */
  label: string;
  onToggle: () => void;
}

/** Clicks on these keep their own meaning inside a picked exchange. */
const INTERACTIVE = "a, button, input, textarea, summary, [contenteditable]";

function hasSelection(): boolean {
  try {
    return (window.getSelection?.()?.toString() ?? "") !== "";
  } catch {
    return false;
  }
}

/** The root element's props for a turn in pick mode. */
export function pickRootProps(pick: TurnPick | undefined) {
  if (!pick) return { cls: [] as string[], onClick: undefined };
  const cls = ["is-pick", pick.checked && "is-pick-on", pick.locked && "is-pick-locked"].filter(Boolean) as string[];
  const onClick = (e: MouseEvent) => {
    if (pick.locked) return;
    const t = e.target as Element | null;
    if (t && typeof t.closest === "function" && t.closest(INTERACTIVE)) return;
    if (hasSelection()) return;
    pick.onToggle();
  };
  return { cls, onClick };
}

/** The check circle, drawn only on an exchange's prompt turn. */
export function PickCheck({ pick }: { pick: TurnPick }) {
  if (!pick.lead) return null;
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      if (!pick.locked) pick.onToggle();
    }
  };
  return (
    <span
      className="turn-pick-check"
      role="checkbox"
      aria-checked={pick.checked}
      aria-disabled={pick.locked || undefined}
      aria-label={`Pick "${pick.label}" for the breakout`}
      tabIndex={pick.locked ? -1 : 0}
      onKeyDown={onKeyDown}
    >
      {pick.checked ? <CircleCheck size={18} aria-hidden="true" /> : <Circle size={18} aria-hidden="true" />}
    </span>
  );
}

interface PlanTurn {
  key: string;
  role: string;
  text?: string;
  streaming?: boolean;
  pending?: unknown;
}

/** One `TurnPick` per turn key. Every turn of an exchange shares its picked
 *  and locked state; the prompt turn is the lead. An exchange with a streaming
 *  or pending turn is locked. */
export function pickPlan(
  turns: readonly PlanTurn[],
  keys: ReadonlySet<string>,
  onToggle: (key: string) => void,
): Map<string, TurnPick> {
  const out = new Map<string, TurnPick>();
  let i = 0;
  while (i < turns.length) {
    let end = i + 1;
    while (end < turns.length && turns[end]!.role !== "user") end++;
    const group = turns.slice(i, end).filter((t) => t.role === "user" || t.role === "assistant");
    const lead = group.find((t) => t.role === "user") ?? group[0];
    const locked = group.some((t) => !!t.streaming || !!t.pending);
    const checked = group.length > 0 && group.every((t) => keys.has(t.key));
    const label = lead && lead.role === "user" ? (lead.text ?? "").trim().slice(0, 60) : "this exchange";
    for (const t of group) out.set(t.key, { checked, locked, lead: t === lead, label, onToggle: () => onToggle(t.key) });
    i = end;
  }
  return out;
}
