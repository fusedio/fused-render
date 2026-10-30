// THE CLAUDE RAIL: what stands at the canvas page's right edge while the Claude
// pane is shut (owner, 2026-10-26: "a very min width column, like we have for
// the fused render left sidebar, just a single width column with the button";
// "rail stays on only when claude panel is collapsed").
//
// The same 48px column the global sidebar collapses to, in the same vocabulary
// (sidebar.css: .sidebar-rail-btn, .sidebar-rail-items, .sidebar-rail-scroll),
// so the two edges of the window read as one idea. Top to bottom:
//   - the panel glyph (platform/ui/PanelIcon) — the way back IN. Its other
//     half, the Hide, is the first seat of the chat's own top row while the
//     pane is up (ClaudeChat `onHidePane`): exactly one of the two is on
//     screen at any moment, the explorer sidebar's own rule (SideChrome.tsx).
//   - NEW TASK, at the head of the list (owner, 2026-10-28: "the + icon
//     should be at the top of the list and it should show a new task modal
//     when we click it, instead of opening the sidebar"). It opens the Tasks
//     page's own card (shell/NewJobModal) over the canvas, prefilled and
//     locked on the clone the way the project page's Tasks tab opens it —
//     the pane stays shut, and the task lands on the rail through the pulse
//     like any other.
//   - EVERY LIVE CHAT ON THIS CANVAS, in the List view's order (owner,
//     2026-10-27: "show all chats we have here, and when we hover we see
//     recap, title — whatever we show in list view"; 2026-10-28: no archived
//     rows, the List's sort — claude-rail-lib.ts). One status ring each — the
//     Tasks page's own (`StatusIcon`: lane colour, unread fill, "!" when it is
//     waiting on you) — and the hover is the list row's own caption: the same
//     `data-hint-title` / `data-hint-reply` the row's title carries
//     (platform/lib/hints), built from the same `cardTitleLine`. A press opens
//     that chat where the Tasks page's door would (`taskHref`): the canvas
//     chat on its session, or the node selected with its chat on its session.
//     Scrolls on a short window.
//
// A SELECTED NODE NARROWS THE RAIL the way it narrows the open pane (owner,
// 2026-10-29: "collapsed sidebar should behave exact same way as expanded
// sidebar but in a collapsed view"): the chats are that node's alone
// (claude-rail-lib `onRail`), and the + makes a task ON the node — the card
// locked on the node's file, the target the node pane's own chats carry, so
// the new task is filed under the node (`task.canvas.file`) and shows in the
// node's Recent list once the pane opens.
//
// THE + WAITS WHERE THE PANE WAITS: the open pane has no chat to start one
// from until the clone exists ("Loading editor…") or while the selected
// node's file is still being written ("Saving <file>…"), so the + is held
// for the same two waits. A task made before the node's file is on disk
// would carry no pane record and miss the node's Recent list.
//
// A HOVER OPENS THE CHAT ITSELF, behind `canvas_rail_peek_enabled` (owner,
// 2026-10-30): with the flag on, resting on a ring — or focusing it — opens
// that chat as a live card beside the rail (RailPeek.tsx): the transcript to
// scroll, the composer to type into, every seat the pane's chat has. The
// timings (open after 150ms, swap between rings without closing, 250ms of
// grace to cross to the card, never close under a card that holds the
// keyboard) are rail-peek-lib's `PeekIntent`; this file only reports the
// rings' pointer and focus to it and draws what it says. Escape or a press
// outside the rail and the card dismiss it; a click into the workbench frame
// closes it on the grace like any other leave. ArrowLeft on a focused ring is the keyboard's way into the
// card. Off, the rings keep their one-line pointer caption (`data-hint`); on,
// the card replaces it — two captions for one hover would be noise.
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Plus } from "lucide-react";
import PanelIcon from "@platform/ui/PanelIcon";
import { getSchedule, type Task } from "@platform/lib/api";
import {
  onDraftChange,
  readListing,
  refreshListing,
  subscribeListing,
  useTasksPulseRows,
} from "@shell/tasksPulse";
import {
  cardTitleLine,
  draftRing,
  isDraftTask,
  liveJobRow,
  recapTitlesOn,
  taskColumn,
  taskHref,
} from "@shell/tasks-lib";
import { StatusIcon } from "@shell/ScheduleTaskViews";
import NewJobModal from "@shell/NewJobModal";
import { draftHref, preloadChat } from "@apps/claude";
import { railRows } from "./claude-rail-lib";
import { PeekIntent, keepAlive, statusWords } from "./rail-peek-lib";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { RailPeek, type RailPeekHandle } from "./RailPeek";
import type { WorkbenchEnv } from "./api";

/** The hover card's DOM id (one rail per page; the ring's `aria-controls`). */
const PEEK_ID = "canvas-rail-peek";

/** What the New task card needs from the schedule before it can open: the
 *  permission modes the server offers and the folders existing tasks point at
 *  (NewJobModal `permissionModes`, `recentTargets` — what Scheduled.tsx hands
 *  it off the schedule it already holds; the rail has no schedule, so it asks
 *  on the press). `seq` keys the mount: every press is a fresh card. */
interface Card {
  seq: number;
  /** The locked target as it was at the press: a node switched while the
   *  schedule read is out must not move the task (Bugbot on #2015). */
  target: string | null;
  modes: string[];
  recents: string[];
}

/** How long the + waits on the schedule before opening the card without it
 *  (CodeRabbit on #1998): a read that hangs — not fails — would otherwise
 *  hold the button disabled until the browser gave up on the request. */
const SCHEDULE_WAIT_MS = 8000;

/** The schedule's answer for the card, or empty if the read fails or outlasts
 *  `SCHEDULE_WAIT_MS` — either way the card opens (see `openNewTask`). */
function cardSeed(): Promise<{ modes: string[]; recents: string[] }> {
  const empty = { modes: [] as string[], recents: [] as string[] };
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => resolve(empty), SCHEDULE_WAIT_MS);
    getSchedule().then(
      (s) => {
        window.clearTimeout(timer);
        resolve({ modes: s.permission_modes, recents: s.entries.map((e) => e.target) });
      },
      () => {
        window.clearTimeout(timer);
        resolve(empty);
      },
    );
  });
}

export function ClaudeRail({
  env,
  name,
  dir,
  node,
  nodeReady,
  activeSessionId,
  onOpen,
  onOpenChat,
  onPullStart,
}: {
  env: WorkbenchEnv;
  name: string;
  /** The clone dir (task rows that predate `task.canvas` name it as their
   *  project; the New task card's locked target). Null until the clone
   *  exists. */
  dir: string | null;
  /** The selected node (null = the canvas as a whole): its name for the
   *  rail's words, its file (basename in the clone) for which chats show and
   *  where a new task points. */
  node: { udfName: string; file: string } | null;
  /** The selected node's file is on disk (the pane's own "Saving…" gate);
   *  true with no node selected. */
  nodeReady: boolean;
  /** The session the page's URL names, drawn as the current row. */
  activeSessionId: string | null;
  /** Open the pane as it is. */
  onOpen: () => void;
  /** Open one of the canvas's chats: its Tasks-page door (`taskHref`). */
  onOpenChat: (href: string) => void;
  /** A pointer down on the rail's inner edge: the start of a pull that opens
   *  the pane at the width the pointer drags it to (CanvasWorkspace
   *  `onRailPull`). */
  onPullStart: (e: ReactPointerEvent<HTMLDivElement>) => void;
}) {
  const rows = useTasksPulseRows();
  const nodeFile = node?.file ?? null;
  const peekOn = useCanvasRailPeek();
  // Drafts and scheduled rows ride the rail whatever the card flag says
  // (owner, 2026-10-30: "the flag is only for the preview thing; the fixes
  // we made — order etc. — should be there without the flag"): the rail is
  // the List's order in rings, drafts included (claude-rail-lib `sessionless`).
  const place = useMemo(
    () => ({ env, name, dir, file: nodeFile, sessionless: true }),
    [env, name, dir, nodeFile],
  );
  // A new task's locked target: the node's file under the clone while a node
  // is selected, else the clone. Null until the clone exists.
  const target = dir && nodeFile ? `${dir.replace(/\/+$/, "")}/${nodeFile}` : dir;
  const scope = node ? node.udfName : "this canvas";
  // THE CAPTION'S WORDS ARE THE LISTING'S, not the pulse's: the pulse carries
  // status and a title, but the list row's caption is built from the full row
  // (recap, last reply, last message, the draft) — and so is the List's ORDER
  // (sortForList reads the full row).
  //
  // FOLLOWED LIVE, not read once (owner, 2026-10-30: "if I make the draft in
  // the card it doesn't update instantly … the sidebar should get updates
  // instantly as well"): the rail subscribes to the listing feed the Tasks
  // page and the pane's own Recent list follow (`subscribeListing`, the
  // `/api/tasks/changes` long-poll, refcounted to one transport per
  // document), so a draft typed into the card's composer, a rename, a chip
  // — anything the listing carries — reaches the rings the moment the
  // server announces it. The read used to be a one-shot `getTasks` repeated
  // when a row's STATUS moved, which a draft never moves: the dot waited for
  // the next pane open. The pulse poll stands down while the feed is live,
  // as it does for every other follower. A failed read keeps the rows on
  // screen (the feed says `failed`; the last good rows are still the best
  // answer).
  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  useEffect(
    () =>
      subscribeListing((ev) => {
        if (!ev.failed) setListing(ev.rows);
      }),
    [],
  );
  // A DRAFT RECORD MOVED (the feed's draft channel: a composer's unsent words
  // saved or cleared, a New task card's form) — the rows' `draft` join is a
  // listing fact, so re-read it now rather than on the next row movement.
  // Collapsed to one read per tick by `refreshListing` itself.
  useEffect(() => onDraftChange(() => refreshListing()), []);
  const full = useMemo(() => new Map((listing ?? []).map((t) => [t.key, t])), [listing]);

  // THE RAIL'S SEATS, in the List's order (claude-rail-lib). `now` is read
  // once per derivation, as `sortForList` asks; the derivation re-runs when a
  // row moves (the pulse) or the listing re-reads, which is when a rank can
  // change.
  const seats = useMemo(() => railRows(rows, full, place), [rows, full, place]);

  // THE HOVER CARD (header; RailPeek.tsx). `peekKey` is what the card shows,
  // `kept` the chats that stay mounted behind it (newest first), both written
  // by the intent machine and nothing else. The ring elements are kept by key
  // for the card's placement and for handing focus back on Escape.
  const [peekKey, setPeekKey] = useState<string | null>(null);
  const [kept, setKept] = useState<readonly string[]>([]);
  const intentRef = useRef<PeekIntent | null>(null);
  if (!intentRef.current) {
    intentRef.current = new PeekIntent({
      show: setPeekKey,
      warm: (key) => setKept((s) => keepAlive(s, key)),
    });
  }
  const intent = intentRef.current;
  useEffect(() => () => intent.dispose(), [intent]);
  const railRef = useRef<HTMLElement | null>(null);
  const peekRef = useRef<RailPeekHandle | null>(null);
  const ringEls = useRef(new Map<string, HTMLElement>());
  const anchorOf = useCallback((key: string) => ringEls.current.get(key) ?? null, []);
  // The chat's chunk, ahead of the first hover (ChatMount `preloadChat`).
  const anySeat = seats.length > 0;
  useEffect(() => {
    if (peekOn && anySeat) preloadChat();
  }, [peekOn, anySeat]);
  // The flag turned off: nothing to show. The seats changing is the
  // intent's to hear (`prune`): a ring that unmounts under the pointer never
  // fires a leave, and a chat that left the rail (archived, the node changed)
  // takes its card with it; the kept set drops the same keys so a stale chat
  // never holds one of the three warm slots.
  const peekSeat = peekOn ? seats.find((s) => s.pulse.key === peekKey) ?? null : null;
  useEffect(() => {
    if (!peekOn && peekKey) intent.dismiss();
  }, [peekOn, peekKey, intent]);
  // JSON, not a joined string: a key is free to contain any delimiter.
  const seatKeys = JSON.stringify(seats.map((s) => s.pulse.key));
  useEffect(() => {
    const live = new Set(JSON.parse(seatKeys) as string[]);
    intent.prune(live);
    setKept((k) => (k.every((key) => live.has(key)) ? k : k.filter((key) => live.has(key))));
  }, [seatKeys, intent]);
  const keptSeats = useMemo(
    () =>
      peekOn
        ? kept.map((k) => seats.find((s) => s.pulse.key === k)).filter((s): s is NonNullable<typeof s> => !!s)
        : [],
    [peekOn, kept, seats],
  );
  // While the card is up: a press anywhere but the rail and the card closes
  // it. A click INTO the workbench frame is not a press this document sees,
  // and it needs no rule of its own: the pointer is on the canvas, off the
  // ring and the card, so the grace close runs — and a card that was holding
  // the keyboard loses it to the frame (`onBlurCapture` → hold(false)), which
  // starts the same grace. There used to be a window-`blur` dismiss here; the
  // canvas wrapper focuses its frame on pointer ENTER (`focusCanvasOnEnter`),
  // and the 8px gap between rail and card is over that wrapper, so crossing
  // to the card blurred the window and killed the card mid-crossing (Bugbot).
  //
  // "INSIDE THE CARD" IS REACT'S ANSWER, NOT THE DOM'S: the chat's menus and
  // pickers are portals — React descendants of the card, DOM children of
  // body — so `contains` would call a press on a menu item an outside press
  // and close the card under it. The card's own capture handler
  // (RailPeek `onPressInside`) runs for those too; this native listener runs
  // first, so it defers its ruling by a tick and reads the flag then.
  const insidePress = useRef(false);
  const onPressInside = useCallback(() => {
    insidePress.current = true;
    intent.pressStart();
  }, [intent]);
  useEffect(() => {
    if (!peekKey) return;
    let tick = 0;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      if (railRef.current?.contains(t)) return;
      insidePress.current = false;
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        if (!insidePress.current) intent.dismiss();
      }, 0);
    };
    const onUp = () => intent.pressEnd();
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("pointerup", onUp, true);
    window.addEventListener("pointercancel", onUp, true);
    return () => {
      window.clearTimeout(tick);
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("pointerup", onUp, true);
      window.removeEventListener("pointercancel", onUp, true);
    };
  }, [peekKey, intent]);
  // NO ROOM FOR A CARD (RailPeek `onNoRoom`: a window too short or narrow):
  // the rings keep their one-line caption until a resize gives the card room
  // again — a hover that shows nothing at all is the one outcome to avoid.
  const [noRoom, setNoRoom] = useState(false);
  const onNoRoom = useCallback(() => {
    intent.dismiss();
    setNoRoom(true);
  }, [intent]);
  useEffect(() => {
    if (!noRoom) return;
    const re = () => setNoRoom(false);
    window.addEventListener("resize", re);
    return () => window.removeEventListener("resize", re);
  }, [noRoom]);
  const dismissToRing = useCallback(() => {
    const key = intent.current;
    intent.dismiss(true);
    if (key) ringEls.current.get(key)?.focus({ preventScroll: true });
  }, [intent]);
  // Focus into the card once it is drawn: the class lands a frame after the
  // show, and a focus on a `visibility: hidden` box does nothing, so try for
  // a few frames rather than guess the one.
  const focusCardSoon = useCallback(() => {
    let tries = 0;
    const attempt = () => {
      const card = document.getElementById(PEEK_ID);
      if (peekRef.current?.focusChat() && card?.contains(document.activeElement)) return;
      if (++tries < 12) requestAnimationFrame(attempt);
    };
    requestAnimationFrame(attempt);
  }, []);
  const onRingKey = (key: string) => (e: ReactKeyboardEvent<HTMLAnchorElement>) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      intent.showNow(key);
      focusCardSoon();
    } else if (e.key === "Escape" && intent.current) {
      e.preventDefault();
      intent.dismiss();
    }
  };

  // THE NEW TASK CARD. Opened on the press, once the schedule has answered
  // what the card needs (`Card`, `cardSeed`); the press is inert while that
  // read is out, so a double-click cannot stack two cards. A schedule read
  // that fails or hangs still opens the card — the modal falls back to "auto" on an empty modes
  // list, and an empty recents list only means fewer suggestions — because a
  // + that does nothing is worse than a card with one fewer dropdown option.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [card, setCard] = useState<Card | null>(null);
  const [opening, setOpening] = useState(false);
  const waiting = dir === null ? "Loading the canvas…" : !nodeReady && node ? `Saving ${node.file}…` : null;
  const openNewTask = () => {
    if (opening || card || waiting) return;
    setOpening(true);
    const at = target;
    void cardSeed().then((got) => {
      if (!alive.current) return;
      setOpening(false);
      setCard({ seq: Date.now(), target: at, ...got });
    });
  };

  return (
    <aside ref={railRef} className="canvas-rail" aria-label="Claude">
      {/* THE RAIL'S INNER EDGE IS A SEAM: pulled left it opens the pane at the
          width the pointer takes it to — the explorer sidebar's reopen edge
          (SideReopenEdge) and the global sidebar's rail, the same gesture on
          the same kind of edge. The Show button stays the click. */}
      <div
        className="canvas-rail-seam"
        role="separator"
        aria-orientation="vertical"
        aria-label="Drag to show the Claude panel"
        onPointerDown={onPullStart}
      />
      <button
        type="button"
        className="sidebar-rail-btn"
        title="Show the Claude panel"
        aria-label="Show the Claude panel"
        aria-expanded={false}
        onClick={onOpen}
      >
        <PanelIcon side="right" />
      </button>
      <div className="sidebar-rail-items">
        <button
          type="button"
          className="sidebar-rail-btn canvas-rail-item"
          title={waiting ?? (node ? `New task on ${node.udfName}` : "New task")}
          aria-label={waiting ?? (node ? `New task on ${node.udfName}` : "New task")}
          aria-haspopup="dialog"
          aria-busy={opening || waiting !== null || undefined}
          disabled={opening || waiting !== null}
          onClick={openNewTask}
        >
          <Plus size={16} aria-hidden="true" />
        </button>
        {seats.length > 0 && (
          <div className="sidebar-rail-scroll canvas-rail-chats" role="list" aria-label={`Chats on ${scope}`}>
            {seats.map(({ pulse: t, row }) => {
              // The live status is the pulse's (fresher than the listing).
              const merged = row ? { ...row, status: t.status } : t;
              const line = cardTitleLine(merged);
              const reply = row && !recapTitlesOn(row) ? row.last_reply || "" : "";
              const label = line.text || t.title || "Chat";
              // A DRAFT'S DOOR is the New task card (`draftHref`, as the List's
              // row press); every other row's is the chat's (`taskHref`). A
              // pulse-only draft (the listing not held yet) has neither for a
              // moment: it still gets its ring, the press waits for the
              // listing.
              const draft = !!row && isDraftTask(row);
              const href = (row && draft ? draftHref(row) : null) ?? taskHref(merged);
              // No door, no ring (as before): a form-scheduled row with no
              // session, or a pulse-only draft the listing has not named yet.
              if (!href) return null;
              const words = draft ? "draft" : statusWords(t, !!row?.failed);
              const peeked = peekOn && peekKey === t.key;
              // The list's item is a wrapper, the link is a link: `aria-haspopup`
              // and `aria-expanded` are not allowed on `listitem`, and a link
              // that opens a dialog is the shape they describe.
              return (
                <div key={t.key} role="listitem" className="canvas-rail-seat">
                <a
                  ref={(el) => {
                    if (el) ringEls.current.set(t.key, el);
                    else ringEls.current.delete(t.key);
                  }}
                  href={href}
                  className={
                    "sidebar-rail-btn canvas-rail-chat" +
                    (activeSessionId && t.session_id === activeSessionId ? " active" : "") +
                    (peeked ? " peeked" : "")
                  }
                  aria-label={`${label} — ${words}`}
                  // The list row's own caption (platform/lib/hints): the
                  // title's line, then the reply's — what the row clamps.
                  // With the hover card on, an EMPTY hint is the opt-out
                  // (hints.ts `hintOf`): the card is the caption.
                  data-hint={peekOn && !noRoom ? "" : line.hint || label}
                  data-hint-title={line.hint || label}
                  data-hint-reply={reply}
                  // Beside the ring, not at the pointer (platform/lib/hints
                  // `placeBeside`): a 48px column at the window's edge.
                  data-hint-side="left"
                  {...(peekOn
                    ? {
                        "aria-haspopup": "dialog" as const,
                        "aria-expanded": peeked,
                        "aria-keyshortcuts": "ArrowLeft",
                        ...(peeked ? { "aria-controls": PEEK_ID } : {}),
                        onPointerEnter: () => intent.enterRing(t.key, "pointer"),
                        onPointerLeave: () => intent.leaveRing("pointer"),
                        onFocus: () => intent.enterRing(t.key, "focus"),
                        onBlur: () => intent.leaveRing("focus"),
                        onKeyDown: onRingKey(t.key),
                      }
                    : {})}
                  onClick={(e) => {
                    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                    e.preventDefault();
                    onOpenChat(href);
                  }}
                >
                  <StatusIcon
                    // The full row's column where it is held: a draft row is
                    // `draft` only once `kind` is known (the pulse has no kind).
                    status={taskColumn(merged)}
                    // With the card on, the ring's own native `title` tooltip
                    // ("Done", "Draft") would float over the card a second
                    // later; "" is no tooltip. The link's aria-label still
                    // names the chat and its state.
                    {...(peekOn ? { label: "" } : {})}
                    unread={t.unread > 0}
                    background={liveJobRow(t)}
                    // The red centre dot the List gives a draft row and a
                    // settled row holding unsent words (tasks-lib.draftRing).
                    draftHeld={!!row && draftRing(merged as Task)}
                    {...(row?.failed ? { failed: true } : {})}
                  />
                </a>
                </div>
              );
            })}
          </div>
        )}
      </div>
      {peekOn && keptSeats.length > 0 && (
        <RailPeek
          ref={peekRef}
          id={PEEK_ID}
          shown={peekSeat}
          kept={keptSeats}
          anchorOf={anchorOf}
          railEl={railRef.current}
          dir={dir}
          onReload={refreshListing}
          onPointerEnter={() => intent.enterCard()}
          onPointerLeave={() => intent.leaveCard()}
          onCone={(inside) => (inside ? intent.enterCone() : intent.leaveCone())}
          onPressInside={onPressInside}
          onHold={(on) => intent.hold(on)}
          onDismiss={dismissToRing}
          onNoRoom={onNoRoom}
          onOpenChat={onOpenChat}
        />
      )}
      {card && (
        <NewJobModal
          // Keyed on the press: the card reads its props in `useState`
          // initialisers, so every opening must be a fresh mount
          // (Scheduled.tsx `openSeq`).
          key={card.seq}
          initialTime={null}
          // SCOPED TO THE CLONE — or to the selected node's file in it — the
          // project page's Tasks tab's way
          // (CanvasPage.tsx `scope.project`, Scheduled.tsx `lockTarget`): a
          // task made from the canvas is a task about the canvas, so the path
          // is stated, not asked (the + is held until the clone exists).
          initialTarget={card.target}
          lockTarget={card.target !== null}
          // From the List, not the calendar: a task typed here is one to run
          // now, and the when-row folds into More options.
          planning={false}
          permissionModes={card.modes}
          recentTargets={card.recents}
          // Closed without creating: the card may have left a DRAFT behind,
          // which the pulse never carries — re-read so its ring shows now.
          onClose={() => {
            setCard(null);
            refreshListing();
          }}
          // "Something changed, re-read": the pulse will move the new row
          // onto the rail; the listing is re-read now so its caption and
          // rank are not 600ms behind its ring.
          onCreated={() => refreshListing()}
        />
      )}
    </aside>
  );
}
