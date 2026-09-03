// The one row every model on /ai-models is drawn as — a cached repo, a curated
// recommendation, a Hub search result. Written once and composed three times,
// because a reader sweeping a list should see one kind of thing at several
// stages of its life rather than three differently-shaped rows that happen to
// be about models.
//
// Slots rather than props-per-fact: what this owns is the ORDER — status icon,
// name and its marks, the id line with whatever is happening right now, the
// size pinned right, the actions — which is exactly the part that must be
// identical between the three. What goes IN each slot is the caller's.
//
// The face states only what a reader gets by SWEEPING: is this model here, is
// it loaded, is it arriving, what does it cost, and — when the answer is no —
// why it cannot be loaded. Everything that is IDENTITY (engine, parameters,
// quantization, format) is behind the (i) — see ModelInfo.tsx.
import type { ReactNode, Ref } from "react";
import { BadgeCheckIcon } from "lucide-react";
import { StatusIcon } from "@platform/ui/flow/StatusIcon";
import { Identifier } from "@platform/ui/flow/Typography";
import { type StatusBucket } from "@platform/ui/status-colors";
import { cn } from "@platform/lib/utils";

/** The half of a repo id that names the MODEL, for the row's head. The owner is
 *  the same string on every row in a section; it leads the id line below. */
export function modelName(id: string): string {
  const cut = id.lastIndexOf("/");
  return cut === -1 ? id : id.slice(cut + 1);
}

/** The curation's mark, at the end of the name: a verified-style seal, not a
 *  bare ✓ (which on a page half about whether a download finished read as a
 *  second, quieter claim about the disk). It marks MEMBERSHIP of the curated
 *  shortlist for a capability. Focusable and hinted: what it means is
 *  hover-only prose nothing else on the row repeats. */
export function CuratedMark() {
  return (
    <span
      className="inline-flex shrink-0 text-foreground"
      tabIndex={0}
      data-hint="Curated by Fused — one of the models this app suggests for its capability."
      aria-label="Curated by Fused"
    >
      <BadgeCheckIcon className="size-3.5" aria-hidden="true" />
    </span>
  );
}

/** Where a model stands on the have/not-have axis, as the leading icon: green
 *  filled = on this disk, neutral outline = not here, yellow pulse = arriving or
 *  loading, orange = a download that stopped halfway, red = failed. */
export type RowStatus = "have" | "none" | "busy" | "partial" | "error";

const STATUS: Record<RowStatus, { bucket: StatusBucket; filled: boolean; pulse: boolean }> = {
  have: { bucket: "green", filled: true, pulse: false },
  none: { bucket: "neutral", filled: false, pulse: false },
  busy: { bucket: "yellow", filled: true, pulse: true },
  partial: { bucket: "orange", filled: false, pulse: false },
  error: { bucket: "red", filled: true, pulse: false },
};

export function ModelRow({
  status,
  statusLabel,
  hoverNote,
  rowRef,
  name,
  marked,
  badges,
  slug,
  size,
  info,
  meta,
  progress,
  actions,
  className,
}: {
  status: RowStatus;
  /** The icon's accessible name. */
  statusLabel: string;
  /** The row's own hover, where there is one thing to say about the whole of
   *  it (the curation's "why this one"). `data-hint`, not `title`, so it does
   *  not double up with a child control's own hint. */
  hoverNote?: string;
  /** For the one row that has to know whether it is on screen (the Hub
   *  result's lazy size). */
  rowRef?: Ref<HTMLDivElement>;
  /** The name, which always goes to the HUB — the licence, the model card and
   *  the discussions are there and none of them are here. */
  name: { href: string; text: string; title: string };
  /** Whether the curation names this model — the seal after the name. */
  marked?: boolean;
  /** Loaded, ✓ downloaded, a gate, a kind, a state tag — whatever the head
   *  has to state beside the name. */
  badges?: ReactNode;
  /** The repo id, in mono under the name. */
  slug: string;
  /** What it costs, pinned to the right edge where a column lines up. */
  size: { text: string; title?: string };
  /** The (i) at the row's end. */
  info?: ReactNode;
  /** Small facts on the id line — "used 4h ago", download counts. */
  meta?: ReactNode;
  /** What this model is doing RIGHT NOW, on the id line — absent when the
   *  answer is "sitting on disk". */
  progress?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  const s = STATUS[status];
  return (
    <div
      ref={rowRef}
      data-hint={hoverNote}
      className={cn(
        "flex items-center gap-3 border-b border-border px-4 py-2 text-sm last:border-b-0 min-w-0",
        className,
      )}
    >
      <StatusIcon bucket={s.bucket} filled={s.filled} pulse={s.pulse} label={statusLabel} />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex min-w-0 items-center gap-2">
          <a
            className="truncate font-medium underline-offset-4 hover:underline"
            href={name.href}
            target="_blank"
            rel="noopener noreferrer"
            data-hint={name.title}
          >
            {name.text}
          </a>
          {marked && <CuratedMark />}
          {badges}
        </div>
        <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
          {/* The hint is on the ID, not the line, so it fires over the text and
              not over the gap beside it. */}
          <Identifier className="truncate" data-hint={slug}>
            {slug}
          </Identifier>
          {meta}
          {progress}
        </div>
      </div>
      <Identifier className="shrink-0 tabular-nums" data-hint={size.title}>
        {size.text}
      </Identifier>
      <div className="flex shrink-0 items-center gap-1">
        {actions}
        {info}
      </div>
    </div>
  );
}
