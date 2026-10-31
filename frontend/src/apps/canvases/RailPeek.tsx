// THE CARD THAT OPENS OFF THE CLAUDE RAIL (owner, 2026-10-30: "when it is
// collapsed can we show a card preview of it when I hover over the chat
// items; I should be able to interact with it as well — scroll and add
// something, click something"). Behind `canvas_rail_peek_enabled`
// (rail-peek-flag.ts); the rings' side of the wiring is ClaudeRail.tsx, the
// timings and the geometry are rail-peek-lib.ts. A hover on a ring opens it;
// a press pins it (ClaudeRail header) — the card itself draws the same
// either way.
//
// WHAT IT IS: the chat itself. Not a caption, not a picture — the same
// `ChatMount` the Tasks page's side peek frames (`peek`, `chatOnly`), on the
// hovered chat's session, in a 420×640 card a gap off the rail. The reader
// scrolls the transcript, answers a question, types a message, follows a
// link, as in the pane; "Open in panel →" in the card's head is the door the
// ring's own press opens (`taskHref`), for when a preview turns into a
// session.
//
// WHY IT NEVER FLASHES:
//   - ONE CARD, kept in the DOM, shown and hidden by a class: the open is an
//     opacity fade and a 6px slide (var(--dur-med)); the close the reverse.
//     Moving from ring to ring moves the card and swaps what is in it; the
//     card itself never closes and reopens.
//   - PLACED, THEN SHOWN, on two frames: the box is set while the card is
//     hidden (no transition runs on a hidden card's position), and the
//     `is-on` class lands on the next frame — so a card that closed at the
//     bottom of the rail and opens at the top does not fly between the two.
//     With the card up, a position change DOES glide, which is the swap.
//   - `position: fixed` in a body portal, with no transform on the box: a
//     transformed ancestor would re-root every fixed-position popover inside
//     the chat (the model menu, the attach picker). The slide is on an inner
//     wrapper and ends at `transform: none` once the card is up, which is no
//     containing block — the 6px offset exists only while hidden or fading,
//     when the card is inert and nothing in it can open a popover.
//
// WHY IT IS FAST: every previewed chat stays mounted after its card closes
// (`kept`, newest first, PEEK_KEEP of them) — `visibility: hidden`, not
// `display: none`, so its scroll position and layout survive — and the
// hovered one's mount starts on the pointer's arrival, before the open wait
// has run (ClaudeRail `warm`). The chat's chunk is fetched when the rail
// mounts with the flag on (`preloadChat`).
//
// A PRESS ON A RING LANDS IN THE COMPOSER (owner, 2026-10-31: "when we click
// the chat item we focus on the card composer so user can type — instead of
// opening the sidebar"): `focusComposer`, retried until the chat has mounted.
//
// ACCESSIBILITY: the card is a non-modal `dialog` named for its chat; the ring
// that opened it says `aria-expanded` and `aria-controls` (ClaudeRail). The
// keyboard's way in is ArrowLeft on a focused ring (`focusChat` lands in the
// composer, or on the card's first button while the chat is still loading);
// Escape anywhere in the card hands focus back to the ring and closes. A card
// that holds focus never closes under the pointer (PeekIntent `hold`).
import {
  forwardRef,
  useImperativeHandle,
  useLayoutEffect,
  useEffect,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MutableRefObject,
} from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import type { Task } from "@platform/lib/api";
import { shortTaskId } from "@platform/lib/task-id";
import { ChatMount, draftHref } from "@apps/claude";
import { Kebab } from "@apps/claude/ui/Kebab";
import { AgentProvider } from "@apps/claude/ui/agent-context";
import { timeAgo } from "@platform/lib/format";
import { chatDraftKey, peekDraftSyncer } from "@platform/lib/drafts";
import {
  cardTitleLine,
  isDraftTask,
  messageStamp,
  nextRunAt,
  relativeWhen,
  taskColumn,
  taskHref,
} from "@shell/tasks-lib";
import { railScope, type RailRow } from "./claude-rail-lib";
import {
  PEEK_MAX_HEIGHT,
  PEEK_PLAIN_HEIGHT,
  PEEK_WIDTH,
  inSafeCone,
  peekPlacement,
  statusWords,
  type Box,
} from "./rail-peek-lib";

/** What one seat is, for the head and the body: the pulse row, the full row
 *  once held, and the answers both halves ask (RailPeek `seatFacts`). */
interface SeatFacts {
  t: RailRow["pulse"];
  row: Task | null;
  /** The pulse's live status over the full row (or the pulse alone). */
  merged: Task | RailRow["pulse"];
  label: string;
  /** Where its press goes: a draft's New task card, else the chat's door. */
  href: string | null;
  draft: boolean;
  /** `#T12` — "" on a draft, which has no task number yet. */
  id: string;
  scope: ReturnType<typeof railScope>;
  words: string;
}

function seatFacts(seat: RailRow, dir: string | null): SeatFacts {
  const { pulse: t, row } = seat;
  const merged = row ? { ...row, status: t.status } : t;
  const line = cardTitleLine(merged);
  const draft = !!row && isDraftTask(row);
  const href = (row && draft ? draftHref(row) : null) ?? taskHref(merged);
  return {
    t,
    row,
    merged,
    label: line.text || t.title || "Chat",
    href,
    draft,
    id: draft ? "" : shortTaskId(t.task_id),
    scope: railScope(t, dir),
    words: draft ? "draft" : statusWords(t, !!row?.failed),
  };
}

export interface RailPeekHandle {
  /** Put the keyboard in the shown chat's composer (or on the card's first
   *  button while the chat is still loading). False when nothing is shown. */
  focusChat(): boolean;
  /** Put the keyboard in the shown chat's composer and nowhere else: false
   *  until the composer has mounted (the chat chunk may still be loading), so
   *  a press that wants to type is never parked on the card's × meanwhile. */
  focusComposer(): boolean;
}

export interface RailPeekProps {
  /** The card's DOM id — what the ring's `aria-controls` names. */
  id: string;
  /** The seat the card shows, or null while it is closed (the last shown
   *  seat stays drawn through the fade-out). */
  shown: RailRow | null;
  /** Every seat whose chat stays mounted: the shown one and the recently
   *  previewed ones (rail-peek-lib `keepAlive`). */
  kept: readonly RailRow[];
  /** The ring element for a seat (its anchor), or null if it is not drawn. */
  anchorOf: (key: string) => HTMLElement | null;
  /** The rail, for its edge and for the row it stands in (its parent). */
  railEl: HTMLElement | null;
  /** The clone dir: what a legacy row's target is read against to name its
   *  node (claude-rail-lib `railScope`). */
  dir: string | null;
  /** The ⋮ filed or deleted a task: re-read the listing now rather than on
   *  the pulse's next move. */
  onReload: () => void;
  onPointerEnter: () => void;
  onPointerLeave: () => void;
  /** The pointer entered (`true`) or left (`false`) the safe cone between
   *  the shown ring and the card, or the bridge over the gap (rail-peek-lib
   *  `inSafeCone`, PeekIntent `enterCone`/`leaveCone`). */
  onCone: (inside: boolean) => void;
  /** A pointer went down inside the card — on the card itself or on one of
   *  the chat's portaled popovers (React's capture phase crosses portals,
   *  the DOM's `contains` does not): hold it through the gesture, and tell
   *  the host's outside-press watcher this press was ours. */
  onPressInside: () => void;
  /** The card took (`true`) or lost (`false`) the keyboard focus. */
  onHold: (on: boolean) => void;
  /** Escape inside the card, or its close button. */
  onDismiss: () => void;
  /** The row left no room for a card (rail-peek-lib `peekPlacement` null —
   *  a window under ~140px tall or ~170px wide): the host drops the show so
   *  the ring is not left saying `aria-expanded` for a card that never drew. */
  onNoRoom: () => void;
  /** "Open in panel →", and any hop the chat itself makes. */
  onOpenChat: (href: string) => void;
  /** The head's node chip: select that node in the workbench (its name, as
   *  the workbench names it — the file's stem). */
  onSelectNode: (udfName: string) => void;
  /** The node the canvas page has selected (its file), if any: that node's
   *  chip is a label, not a door — it is already selected. */
  selectedFile: string | null;
}

/** A seat with no conversation yet (header of the body above). */
function PlainBody({ facts }: { facts: SeatFacts }) {
  const { row, draft, label } = facts;
  const form = (row?.form ?? {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === "string" ? v.trim() : "");
  // The words: a task draft's description, a chat draft's composer preview.
  // Never the title again — it is in the head.
  const preview = str(row?.draft?.preview);
  const description = str(form.description);
  const words = (draft ? description || preview : "") || "";
  const same = words.toLowerCase() === label.trim().toLowerCase();
  const saved = draft ? timeAgo(row?.draft?.updated_at) : null;
  // The run itself, not the List's chip: `nextRunChip` is null for an
  // Upcoming row because the row's own stamp already says the time there —
  // this card has no such stamp (CodeRabbit on #2029).
  const at = !draft && row ? nextRunAt(row) : null;
  const next = at ? `Runs ${relativeWhen(at)} · ${messageStamp(at)}` : null;
  const meta = draft
    ? [str(form.model), str(form.effort), str(form.permission)].filter(Boolean)
    : [str(row?.model), str(row?.effort)].filter(Boolean);
  return (
    <div className="canvas-rail-peek-plain">
      <div className="canvas-rail-peek-plain-row">
        <span className={"canvas-rail-peek-pill" + (draft ? " is-draft" : "")}>
          {draft ? "Draft" : "Scheduled"}
        </span>
        <span className="canvas-rail-peek-plain-when">
          {draft
            ? saved
              ? `Not sent · saved ${saved}`
              : "Not sent"
            : next ?? "Not started yet"}
        </span>
      </div>
      <p className={"canvas-rail-peek-plain-words" + (!words || same ? " is-empty" : "")}>
        {words && !same ? words : draft ? "No description yet." : "No message yet."}
      </p>
      {meta.length > 0 && <p className="canvas-rail-peek-plain-meta">{meta.join(" · ")}</p>}
    </div>
  );
}

export const RailPeek = forwardRef<RailPeekHandle, RailPeekProps>(function RailPeek(
  {
    id,
    shown,
    kept,
    anchorOf,
    railEl,
    dir,
    onReload,
    onPointerEnter,
    onPointerLeave,
    onCone,
    onPressInside,
    onHold,
    onDismiss,
    onNoRoom,
    onOpenChat,
    onSelectNode,
    selectedFile,
  },
  ref,
) {
  const cardRef = useRef<HTMLDivElement | null>(null);
  // The last seat shown, so the fade-out has something to fade — the head
  // AND the chat: both key on `view`, so the transcript stays visible through
  // the fade rather than vanishing under a fading empty frame (Bugbot).
  const last = useRef<RailRow | null>(null);
  if (shown) last.current = shown;
  const view = shown ?? last.current;
  const viewKey = view?.pulse.key ?? null;

  const [box, setBox] = useState<Box | null>(null);
  const [on, setOn] = useState(false);
  const shownKey = shown?.pulse.key ?? null;

  // One composer ref per kept mount (ChatMount `focusRef`).
  const focusRefs = useRef(new Map<string, MutableRefObject<HTMLTextAreaElement | null>>());
  const focusRefFor = (key: string) => {
    let r = focusRefs.current.get(key);
    if (!r) {
      r = { current: null };
      focusRefs.current.set(key, r);
    }
    return r;
  };
  for (const key of [...focusRefs.current.keys()]) {
    if (!kept.some((s) => s.pulse.key === key)) focusRefs.current.delete(key);
  }

  // A seat with no session gets the short card (PEEK_PLAIN_HEIGHT).
  const maxHeightFor = (key: string) =>
    kept.find((s) => s.pulse.key === key)?.pulse.session_id ? PEEK_MAX_HEIGHT : PEEK_PLAIN_HEIGHT;
  // The rail's left edge as of the last placement: where the bridge below
  // ends. A ref, not state — `place` runs before every `setBox`, so the
  // render that draws the new box reads a fresh value.
  const railLeft = useRef(0);
  const place = (key: string): Box | null => {
    const anchor = anchorOf(key);
    const row = railEl?.parentElement;
    if (!anchor || !railEl || !row) return null;
    const rail = railEl.getBoundingClientRect();
    railLeft.current = rail.left;
    return peekPlacement(anchor.getBoundingClientRect(), rail, row.getBoundingClientRect(), maxHeightFor(key));
  };

  // PLACED, THEN SHOWN (header). Hidden → shown sets the box on this frame and
  // the class on the next; shown → shown (a swap) moves the box under the
  // class, which glides; shown → hidden drops the class and keeps the box.
  useLayoutEffect(() => {
    if (!shownKey) {
      setOn(false);
      return;
    }
    const next = place(shownKey);
    if (!next) {
      onNoRoom();
      return;
    }
    setBox(next);
    const id = requestAnimationFrame(() => setOn(true));
    return () => cancelAnimationFrame(id);
    // `place` reads the DOM; the key is what decides when to re-read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey]);

  // A WARM MOUNT NEEDS A REAL BOX: the first chat mounts before anything is
  // shown, and a chat laid out at 0×0 would do its first paint and its
  // scroll-to-end in a box it is about to leave. Place the hidden card for
  // the warmed ring, so the mount lays out at the size it will be shown at.
  const warmKey = kept[0]?.pulse.key ?? null;
  useLayoutEffect(() => {
    if (box || !warmKey) return;
    setBox(place(warmKey) ?? { left: -PEEK_WIDTH - 50, top: 0, width: PEEK_WIDTH, height: PEEK_MAX_HEIGHT });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [box, warmKey]);

  // FOLLOW THE RING while up: a window resize, the rail's own list scrolling
  // under the pointer on a short window, and the row changing shape (the
  // sidebar folding, a pane opening); plus every render, cheaply — the pulse
  // re-sorts the rail, and a ring that moved takes its card with it.
  useEffect(() => {
    if (!shownKey || !railEl) return;
    const re = () => {
      const next = place(shownKey);
      // The row shrank under the card (a window resized down to nothing):
      // the card leaves rather than standing clamped over the canvas.
      if (!next) {
        onNoRoom();
        return;
      }
      setBox((prev) =>
        next && prev && next.left === prev.left && next.top === prev.top && next.width === prev.width && next.height === prev.height
          ? prev
          : next,
      );
    };
    window.addEventListener("resize", re);
    const list = railEl.querySelector(".canvas-rail-chats");
    list?.addEventListener("scroll", re, { passive: true });
    const row = railEl.parentElement;
    const ro = row && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (row && ro) ro.observe(row);
    return () => {
      window.removeEventListener("resize", re);
      list?.removeEventListener("scroll", re);
      ro?.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey, railEl]);
  useEffect(() => {
    if (!shownKey) return;
    const next = place(shownKey);
    if (next && box && (next.top !== box.top || next.left !== box.left || next.height !== box.height)) setBox(next);
  });

  // LEAVING A CHAT SAVES ITS DRAFT (owner, 2026-10-30: "if I make the draft
  // in the card it doesn't update instantly"). A session chat's composer
  // writes its unsent words on window blur, page hide or its own UNMOUNT —
  // never per keystroke (Composer, "one rule for every composer") — and a
  // kept-alive mount never unmounts: the card hid, the words sat unwritten,
  // the ring's draft dot waited for the pane to open. So the card hiding, or
  // swapping to another chat, is the leave: the chat that just left view
  // flushes its draft syncer now, and the listing feed carries the dot back
  // to the ring (ClaudeRail `onDraftChange` → `refreshListing`).
  const lastShownKey = useRef<string | null>(null);
  useEffect(() => {
    const prev = lastShownKey.current;
    lastShownKey.current = shownKey;
    if (!prev || prev === shownKey) return;
    const seat = kept.find((s) => s.pulse.key === prev);
    const sid = seat?.pulse.session_id;
    if (!sid) return;
    const file = seat.row?.target || seat.pulse.target || seat.pulse.project;
    peekDraftSyncer(chatDraftKey(sid, file))?.flushNow();
    // `kept` only matters for the seat that just left; the key is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey]);

  // THE SAFE CONE (header; rail-peek-lib `inSafeCone`): while the card is up,
  // every pointer move is tested against the triangle from the shown ring's
  // centre to the card's padded right edge. Inside → `onCone(true)`: the
  // card holds and a ring crossed on the way does not swap; outside →
  // `onCone(false)`: the intent re-arms for whatever the pointer rests on.
  // The bridge's own enter/leave feed the same switch, so one clock
  // (PEEK_CONE_MS) covers both.
  const coneOn = useRef(false);
  const setCone = (inside: boolean) => {
    if (coneOn.current === inside) return;
    coneOn.current = inside;
    onCone(inside);
  };
  useEffect(() => {
    if (!on || !box || !shownKey) return;
    const anchor = anchorOf(shownKey);
    if (!anchor) return;
    const r = anchor.getBoundingClientRect();
    const apex = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    const onMove = (e: PointerEvent) => {
      const inCard = cardRef.current?.contains(e.target as Node) ?? false;
      setCone(!inCard && inSafeCone({ x: e.clientX, y: e.clientY }, apex, box));
    };
    document.addEventListener("pointermove", onMove, true);
    return () => {
      document.removeEventListener("pointermove", onMove, true);
      setCone(false);
    };
    // `onCone` is the host's stable callback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [on, box, shownKey]);

  // INERT WHILE HIDDEN (React 18 has no `inert` prop): the fading card and
  // the warm mounts behind it take no focus, no hit, and are out of the
  // accessibility tree — `aria-hidden` on a subtree that might still hold
  // focus is the thing this avoids.
  const visible = on && box !== null && shownKey !== null;
  useLayoutEffect(() => {
    cardRef.current?.toggleAttribute("inert", !visible);
  }, [visible]);

  useImperativeHandle(
    ref,
    () => ({
      focusChat() {
        if (!shownKey) return false;
        const box = focusRefs.current.get(shownKey)?.current;
        const target = box ?? cardRef.current?.querySelector<HTMLElement>("button");
        if (!target) return false;
        target.focus({ preventScroll: true });
        return true;
      },
      focusComposer() {
        const box = shownKey ? focusRefs.current.get(shownKey)?.current : null;
        if (!box) return false;
        box.focus({ preventScroll: true });
        return true;
      },
    }),
    [shownKey],
  );

  // WHICH FOCUS HOLDS THE CARD. Typing (an editable), a popup of the chat's
  // (its ⋮ menu, a picker, a dialog), or focus that the KEYBOARD brought here
  // (Tab, an arrow — the ArrowLeft way in) — those are a reader using the
  // card, and it must not close under them. A plain button focused by a
  // click, or by a menu handing focus back to its trigger on Escape, is not:
  // a reader who closed the ⋮ and moved the pointer away expects the card to
  // follow the pointer rule again, not to stand until a second Escape.
  const lastKeyAt = useRef(0);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" || e.key.startsWith("Arrow")) lastKeyAt.current = Date.now();
    };
    // A pointer press focuses by mouse: a click right after a Tab is not
    // keyboard focus (CodeRabbit on #2029).
    const onPointer = () => {
      lastKeyAt.current = 0;
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onPointer, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onPointer, true);
    };
  }, []);
  const onFocusCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const t = e.target as HTMLElement;
    const editable =
      t.matches("textarea, input, select, [contenteditable=''], [contenteditable='true']");
    // NOT the card itself, which is a `dialog` too: the nearest popup role
    // above the target has to be something INSIDE the card.
    const pop = t.closest("[role='menu'], [role='listbox'], [role='dialog'], [role='alertdialog']");
    const popup = !!pop && pop !== cardRef.current;
    const keyboard = Date.now() - lastKeyAt.current < 400;
    onHold(editable || popup || keyboard);
  };
  const onBlurCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget as Node | null;
    if (next && e.currentTarget.contains(next)) return;
    onHold(false);
  };
  // Escape ON THE HEAD only. The chat hands its own Escape up through
  // `onEscape` once nothing of its own (a menu, the shot viewer) is open; a
  // card-wide handler would close the card under an open menu and, having
  // prevented default, stop the chat's handler from closing the menu itself.
  const onHeadKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onDismiss();
    }
  };

  const head = view ? seatFacts(view, dir) : null;

  // THE ⋮ IS THE CHAT'S OWN (owner, 2026-10-30: "it is not the same kebab
  // menu — fix that"): `@apps/claude/ui/Kebab`, the menu the pane's chat
  // wears — continue in terminal, copy the command, download the transcript,
  // archive, delete — with its own labels, refusals and erase dialog. Its
  // popup is a portal; the card HOLDS through it because the popup takes the
  // keyboard focus and React's focus events cross portals (`onFocusCapture`
  // → `onHold(true)`), and a press on one of its rows is "inside" by the
  // same route (`onPressInside`). Filing or erasing the task re-reads the
  // listing; the pulse then takes the ring, and the card with it (prune).
  const onFiled = () => onReload();

  // THE BRIDGE (owner, 2026-10-30: "an invisible [strip] between the item I
  // am hovering and the preview, so the user doesn't have to move fast or
  // time it"): a transparent band over the gap between the card's right edge
  // and the rail, the card's full height, that counts as the card for the
  // pointer — resting in the gap holds the card open past the grace, and
  // the canvas wrapper under the gap never sees the pointer (it focuses its
  // frame on enter). It stops 3px short of the rail so the rail's seam (the
  // drag that opens the pane) keeps its own hit area. Only while the card is
  // up (`.is-on`), like the card itself.
  const bridge =
    box && railLeft.current > box.left + box.width + 1
      ? { left: box.left + box.width, top: box.top, width: Math.max(0, railLeft.current - 3 - (box.left + box.width)), height: box.height }
      : null;

  return createPortal(
    <>
    {bridge && (
      <div
        className={"canvas-rail-peek-bridge" + (visible ? " is-on" : "")}
        aria-hidden="true"
        style={bridge}
        onPointerEnter={() => setCone(true)}
        onPointerLeave={() => setCone(false)}
      />
    )}
    <div
      ref={cardRef}
      id={id}
      className={"canvas-rail-peek" + (visible ? " is-on" : "") + (view && !view.pulse.session_id ? " is-plain" : "")}
      role="dialog"
      aria-label={head ? `${head.label} — preview` : "Chat preview"}
      style={
        box
          ? view?.pulse.session_id
            ? { left: box.left, top: box.top, width: box.width, height: box.height }
            : { left: box.left, top: box.top, width: box.width }
          : undefined
      }
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      onPointerDownCapture={onPressInside}
      onFocusCapture={onFocusCapture}
      onBlurCapture={onBlurCapture}
    >
      <div className="canvas-rail-peek-card">
        {head && (
          // THE HEAD (owner, 2026-10-30): the task's number where the Tasks
          // page prints it, its title, WHERE it lives (the canvas, or one
          // node — the pane's chats do not say, and a reader could not tell a
          // node's chat from the canvas's), then the door as a word, the ⋮,
          // and the close.
          <div className="canvas-rail-peek-head" onKeyDown={onHeadKeyDown}>
            {/* The close leads (owner, 2026-10-30: "move the cross to the top
                left") — the pane's own Hide sits at ITS top row's left end
                too (ClaudeChat `onHidePane`), so the two dismissals share a
                corner. */}
            <button
              type="button"
              className="canvas-rail-peek-btn"
              title="Close the preview (Esc)"
              aria-label="Close the preview"
              onClick={onDismiss}
            >
              <X size={14} aria-hidden="true" />
            </button>
            {head.id && (
              <span className="canvas-rail-peek-id" title={head.words}>
                {head.id}
              </span>
            )}
            <span className="canvas-rail-peek-title" title={head.label}>
              {head.label}
            </span>
            {head.scope.kind === "node" && head.scope.file !== selectedFile ? (
              // The node's chip is a door: it selects the node in the
              // workbench (which zooms to it and shows its code). The card
              // STAYS (owner, 2026-10-31: "when I click on the tag, shouldn't
              // the chat preview still stay open") — the press is inside the
              // card, so it pins it like any other; the rail then narrows to
              // that node, whose rings still hold this chat, and the card
              // follows its ring.
              <button
                type="button"
                className="canvas-rail-peek-scope is-node"
                title={`Select the node ${head.scope.name} (${head.scope.file}) in the workbench`}
                onClick={() => {
                  if (head.scope.kind === "node") onSelectNode(head.scope.name);
                  // Back to the composer: the reader is still in this chat,
                  // and WebKit leaves a pressed button unfocused — focus on
                  // the body, where a later press into the canvas frame is
                  // not heard as the window's blur and so never closes the
                  // pinned card (ClaudeRail's frame-press rule).
                  if (shownKey) focusRefs.current.get(shownKey)?.current?.focus({ preventScroll: true });
                }}
              >
                {head.scope.name}
              </button>
            ) : head.scope.kind === "node" ? (
              // Already the selected node: nothing to go to.
              <span
                className="canvas-rail-peek-scope is-node"
                title={`On the node ${head.scope.name} (${head.scope.file}), selected`}
              >
                {head.scope.name}
              </span>
            ) : (
              <span className="canvas-rail-peek-scope" title="On the canvas as a whole">
                Canvas
              </span>
            )}
            {head.href && (
              <button
                type="button"
                className="btn canvas-rail-peek-open"
                title={head.draft ? "Open the draft" : "Open this chat in the Claude panel"}
                onClick={() => {
                  onOpenChat(head.href!);
                  // The door closes the card too: a hop that keeps the rail
                  // mounted (a node switch) must not leave it up.
                  onDismiss();
                }}
              >
                Open
              </button>
            )}
            {head.t.session_id && shownKey && (
              // Mounted only while a seat is SHOWN (not through the fade): the
              // menu listens for task announcements and re-reads the listing
              // on each, which a hidden card has no business doing. Inside the
              // chat's AgentProvider, so its terminal hand-off names the
              // chat's own agent, not the default one.
              <span className="canvas-rail-peek-kebab">
                <AgentProvider backend={head.row?.backend ?? head.t.backend ?? ""}>
                  <Kebab
                    // Keyed per chat: the menu caches its labels per session.
                    key={head.t.key}
                    file={head.row?.target || head.t.target || head.t.project}
                    sessionId={head.t.session_id}
                    running={taskColumn(head.t) === "in_progress" || taskColumn(head.t) === "queued"}
                    asking={taskColumn(head.t) === "needs_attention"}
                    queued={taskColumn(head.t) === "queued"}
                    onErased={onFiled}
                    onArchived={onFiled}
                  />
                </AgentProvider>
              </span>
            )}
          </div>
        )}
        <div className="canvas-rail-peek-body">
          {kept.map((seat) => {
            const { pulse: t, row } = seat;
            const facts = seatFacts(seat, dir);
            return (
              <div
                key={t.key}
                className={"canvas-rail-peek-chat" + (t.key === viewKey ? " is-shown" : "")}
              >
                {t.session_id ? (
                  <ChatMount
                    file={row?.target || t.target || t.project}
                    sessionId={t.session_id}
                    // The task's own settings, a seed (TaskPeek passes the same).
                    {...(row?.model ? { model: row.model } : {})}
                    {...(row?.effort ? { effort: row.effort } : {})}
                    {...(row?.backend ?? t.backend ? { backend: (row?.backend ?? t.backend)! } : {})}
                    chatOnly
                    peek
                    // A hover must never take the keyboard: the reader is pointing,
                    // not typing, until they click into the card or ArrowLeft in.
                    noFocus
                    // No annotate seats: the card has no canvas of its own to mark.
                    noAnnTools
                    paramsSource="memory"
                    focusRef={focusRefFor(t.key)}
                    onEscape={onDismiss}
                    // A hop out of the chat (a Recent row, a link) is the ring's
                    // own navigation: the canvas page's hop reader does the rest.
                    onNavigate={onOpenChat}
                  />
                ) : (
                  // NO SESSION YET — a draft, or a scheduled task that has not
                  // run: no conversation to show, so the card is SHORT and
                  // says what the row is in the List's own vocabulary — the
                  // `Draft` pill, when it was last touched, the words it holds
                  // (the form's description or the composer's preview; the
                  // title is already in the head), and the settings it would
                  // run with. The head's Open is its one door.
                  <PlainBody facts={facts} />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
    </>,
    document.body,
  );
});
