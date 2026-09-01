// The (i) in a card's head, and the panel it opens: everything about a model
// that is IDENTITY rather than STATE.
//
// The card's face used to carry all of it — engine, task, parameters,
// quantization, library — on a row of five chips under the name. Every one of
// those is read once, by somebody deciding about ONE model; none of them is
// read by sweeping a grid, which is what the face is for. So they moved behind
// a control, and the face kept the three facts a sweep actually asks for: is it
// here, is it loaded, is it arriving (Akshil, 2026-08-24).
//
// **Fixed, never inline.** The panel is `position: fixed` and rendered outside
// the card's flow, so opening it cannot change the card's box — these cards sit
// in a horizontal carousel, and a card that grew on click would shove every
// card to the right of it mid-scroll. That was the explicit ask: "it shouldn't
// change card sizes or layouts, it is a popover".
//
// **One grey for everything in it.** No engine hue, no accent, no dashed
// warning. Those colours are signals, and a signal belongs where a reader can
// see it without asking — on the face, or on the disabled button's own reason.
// This is a fact sheet, and a fact sheet in five colours reads as five kinds of
// urgency.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { InfoIcon } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { type AiModelRepo } from "@platform/lib/api";
import { formatParams, timeAgo } from "@platform/lib/format";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { noEngineReason } from "@apps/ai_models/lib/aiModelGroups";

/** Gap between the trigger and the panel, and the margin the panel keeps from
 *  the viewport edge when it has to flip or slide. */
const GAP = 6;
const EDGE = 8;

/** One line of the panel. */
export interface InfoRow {
  label: string;
  /** `null` for a row that has nothing to say — the row then does not exist. An
   *  empty value beside a label is a fact the panel claims to know and does
   *  not. */
  value: string | null;
  /** The long version, on the app's instant hint rather than a native `title`. */
  hint?: string;
}

function Row({ label, value, hint }: InfoRow) {
  if (!value) return null;
  return (
    <div className="am-info-row">
      <span className="am-info-label">{label}</span>
      <span className="am-info-value" data-hint={hint}>
        {value}
      </span>
    </div>
  );
}

/** What the footer link does, for a card that has somewhere local to send you.
 *  A recommendation has nowhere — the model is not on this disk yet — so it
 *  simply omits this and the panel ends at its last row. */
interface InfoMore {
  label: string;
  href: string;
  onOpen: () => void;
}

function Panel({
  name,
  rows,
  more,
  anchor,
  onClose,
}: {
  name: string;
  rows: InfoRow[];
  more?: InfoMore;
  anchor: DOMRect;
  onClose: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  // Placed once the real size is known, like ContextMenu's clamp: a panel whose
  // width is decided by its longest engine name cannot be positioned from the
  // trigger's rect alone. Hidden (not merely unpositioned) until then, so it
  // never paints at 0,0 and jumps.
  const [at, setAt] = useState<{ left: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const el = panel.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    // RIGHT-ALIGNED to the trigger, because the trigger is the card's top-right
    // corner: a panel growing rightwards from it leaves the card immediately.
    let left = anchor.right - width;
    let top = anchor.bottom + GAP;
    if (left < EDGE) left = EDGE;
    if (left + width > window.innerWidth - EDGE) left = Math.max(EDGE, window.innerWidth - EDGE - width);
    // Flips above the trigger rather than clamping into the fold: a panel
    // pinned to the bottom edge covers the card it belongs to.
    if (top + height > window.innerHeight - EDGE) top = Math.max(EDGE, anchor.top - GAP - height);
    setAt({ left, top });
  }, [anchor]);

  // Every way the anchor can go stale is a close, for ContextMenu's reason: a
  // panel pinned to a point the page has moved out from under is worse than no
  // panel. Scroll is captured because the card sits in its own scroller.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    const onDown = (e: PointerEvent) => {
      if (!panel.current?.contains(e.target as Node)) onClose();
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("scroll", onClose, true);
    window.addEventListener("resize", onClose);
    window.addEventListener("blur", onClose);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("scroll", onClose, true);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("blur", onClose);
    };
  }, [onClose]);

  return (
    <div
      className="am-info-panel"
      ref={panel}
      role="dialog"
      aria-label={`About ${name}`}
      style={at ? { left: at.left, top: at.top } : { visibility: "hidden" }}
    >
      {rows.map((row) => (
        <Row key={row.label} {...row} />
      ))}
      {more && (
        <a
          className="am-info-more"
          href={more.href}
          onClick={(e) => {
            if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
              return;
            e.preventDefault();
            onClose();
            more.onOpen();
          }}
        >
          {more.label}
        </a>
      )}
    </div>
  );
}

/** The (i) and its panel, owned together.
 *
 *  State lives here rather than in the card because nothing else on the card
 *  reacts to it: the panel is fixed, so an open one changes no layout the card
 *  would have to know about. That is also what makes it safe for the card to
 *  re-render underneath an open panel — a poll landing mid-read does not close
 *  it, and the anchor rect is re-measured only when the panel opens.
 *
 *  **Takes rows rather than a repo**, because three different cards open one now
 *  and only one of them has an `AiModelRepo` behind it (2026-08-25). A
 *  recommendation and a Hub result know their engine and nothing else about the
 *  weights — there are none on this disk to read — so they pass the one row they
 *  can answer, and the panel is however tall that makes it.
 */
export function InfoButton({
  name,
  rows,
  more,
}: {
  /** What the panel is ABOUT, for its accessible name and the trigger's. */
  name: string;
  rows: InfoRow[];
  more?: InfoMore;
}) {
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="am-card-info"
        data-hint={`About ${name}`}
        aria-label={`About ${name}`}
        aria-haspopup="dialog"
        aria-expanded={!!anchor}
        onClick={(e) =>
          setAnchor(anchor ? null : (e.currentTarget as HTMLElement).getBoundingClientRect())
        }
      >
        <InfoIcon />
      </Button>
      {anchor && (
        <Panel name={name} rows={rows} more={more} anchor={anchor} onClose={() => setAnchor(null)} />
      )}
    </>
  );
}

/** The same control, for a repo that is actually ON this disk — which is the
 *  only card that can answer more than "which engine". */
export function ModelInfoButton({ repo }: { repo: AiModelRepo }) {
  const added = timeAgo(repo.added);
  return (
    <InfoButton
      name={repo.id}
      rows={[
        // The ENGINE first and always: it is the one fact this page could not
        // answer from the model's name, and "nothing here reads this" is its
        // most useful state rather than an edge case worth hiding. The
        // hardware-qualified build (`shortLabel`), not the family — the tag that
        // had to stay narrow is gone, so there is room for the whole answer.
        {
          label: "Engine",
          value: repo.engine ? repo.engine.shortLabel : "None",
          hint: repo.engine ? undefined : noEngineReason(repo),
        },
        {
          label: "Parameters",
          value:
            repo.params === null
              ? null
              : `${repo.paramsEstimated ? "≈" : ""}${formatParams(repo.params)}`,
          hint: repo.paramsEstimated
            ? "Recovered from packed weights, so it rests on the width the checkpoint declares"
            : undefined,
        },
        { label: "Quantization", value: repo.quantization },
        // Only where no engine claimed the repo. An engine tag IS a format claim
        // — "MLX LM" is the statement that these weights are mlx — so printing
        // both says the same thing twice.
        { label: "Format", value: repo.engine ? null : repo.library },
        { label: "Added", value: added ? added : null },
      ]}
      /* The local door: this folder's own model card (SPEC §38). The card's NAME
         still goes to the Hub — two different destinations, so they get two
         different controls rather than competing for one click. */
      more={{
        label: "Know more",
        href: urlForFsPath(repo.path, "?_mode=model_card"),
        onOpen: () => navigate(repo.path, { isDir: true, mode: "model_card" }),
      }}
    />
  );
}
