// THE TASKS CARD OFF THE RAIL'S TASKS DOOR (owner, 2026-11-07; design.md
// "Tasks card off the rail's Tasks door"): resting on the rail's Tasks icon
// previews the Tasks page as a card beside the rail; a press pins it; a press
// on the icon while pinned closes it, and so does the card's ×. Behind the
// rail flag, like the nav doors themselves (ClaudeRail `navDoors`).
//
// WHAT IT IS: the Tasks page itself (shell/Scheduled, `inCard`) — its toolbar,
// views, filters and New task — less the page's big heading, its side peek and
// its URL. A task's press inside it opens THAT TASK'S CHAT IN THE CARD (the
// same `ChatMount` window A frames: `peek`, `chatOnly`), and the head's Back
// returns to the list exactly where it was — the list stays mounted under the
// chat, hidden, its scroll and filters kept. The press reaches the card
// through `openPeek` (task-peek-store `CardPeekContext`, `notePressInCard`):
// a press in the card never touches the page's peek or the URL, and a press on
// the /tasks page under the card still opens the page's own.
//
// MOUNTED ONCE, IN THE SHELL (App.tsx, beside FavouritePeek), for the same
// reason the favourite's card is: the rail is a page's and comes and goes with
// it, and a card inside it would lose its view, filters and scroll on every
// hop. This one stands beside whichever rail is up (fav-peek-store's rail
// stack), level with that rail's Tasks door (tasks-peek-store
// `railTasksDoor`), and HIDES — never unmounts — when closed or when no rail
// stands. Its content mounts on the first opening and stays until a reload.
// THE OPEN CHAT DOES NOT SURVIVE A CLOSE (owner, 2026-11-08: "you close the
// window and click on the Tasks button again — it should open the Tasks page,
// not the last task I opened; a window change resets the selection"): the
// card going down by any road — its toggle, Escape, another card taking its
// place, a page hop — lets the chat go, and the next opening is the list.
//
// WINDOW A'S CHASSIS: the rail card's classes (`.claude-rail-peek`, claude-
// rail.css), its two-frame show and fade, bridge and safe cone, inert while
// hidden, and WINDOW A'S SIZE — the remembered `canvasRailPeek` (rail-peek-lib
// `railCardSize`), which this card's grips write too: one size for the cards
// at the rail's top. Its timings are window A's (tasks-peek-store's
// PeekIntent): a preview goes on the grace, Escape closes it; a pinned card
// closes by its ×, the door's toggle or Open only.
//
// IT IS WINDOW A (ClaudeRail; owner, 2026-11-08: "the Tasks window is like
// window A, it shouldn't affect anything else"): it opens wherever window A
// opens, pane open or shut; its opening puts down the list's card and the +
// popover, and either of those opening closes it — a ring's press beside the
// OPEN pane opens that chat in the pane and closes this card, and the pane's
// chat changing under it closes it too. Window B (the favourite's or the
// bot's card) is the other section's: never closed by this card, never
// closes it — this card keeps clear of it (`favBox`, rail-peek-lib
// `placeClear`), as the list's card does.
//
// THE HEAD is one row, the bot card's: the × in the top-left corner, Back
// beside it while a chat is open, the small title ("Tasks", or the chat's),
// and Open — the /tasks page itself — on the right.
import {
  Component,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ArrowUpRight, ChevronDown, ChevronUp, X } from "lucide-react";
import type { Task } from "@platform/lib/api";
import { breakoutOpener } from "@platform/lib/breakouts";
import { navigateUrl } from "@platform/lib/router";
import { getUiLayout, patchUiLayout, subscribeUiLayout } from "@platform/lib/ui-layout";
import { ChatMount } from "@apps/claude";
import { Kebab } from "@apps/claude/ui/Kebab";
import { AgentProvider } from "@apps/claude/ui/agent-context";
import { readListing, subscribeListing, useTasksPulseRows } from "@shell/tasksPulse";
import { cardTitleLine, taskChatTarget, taskColumn, taskHref } from "@shell/tasks-lib";
import { TaskPeekWho } from "@shell/TaskPeekWho";
import {
  CardPeekContext,
  PEEK_ITEM_ATTR,
  notePressInCard,
  peekVisibleOrder,
  registerPeekCard,
  stepPeekKey,
  type CardPeek,
} from "@shell/task-peek-store";
import { useAutoReadOpenTasks, useDocumentVisible } from "@shell/task-open-read";
import { useFavPeekState } from "./fav-peek-store";
import { useCanvasRailPeek } from "./rail-peek-flag";
import {
  PEEK_MIN_HEIGHT,
  PEEK_MIN_WIDTH,
  bottomClear,
  dragResize,
  inSafeCone,
  placeClear,
  railCardSize,
  rowOf,
  type Box,
  type ResizeEdge,
} from "./rail-peek-lib";
import {
  TASKS_PEEK_ID,
  closeTasksCard,
  railTasksDoor,
  registerTasksCard,
  registerTasksCardFocus,
  setTasksCardRoom,
  tasksCardIntent,
  tasksCardState,
  useTasksCard,
} from "./tasks-peek-store";

// The page's own chunk (App.tsx lazy-loads the same module): not the shell's
// to carry until the door is first hovered.
const Scheduled = lazy(() => import("@shell/Scheduled"));

const ALL_GRIPS: readonly ResizeEdge[] = ["l", "t", "b", "tl", "bl"];

/** Window A's size as remembered (header). */
function cardSize(): { width: number; height: number } {
  return railCardSize(getUiLayout().canvasRailPeek ?? null);
}

/** A chunk that fails to load (an app updated under this window) fails HERE,
 *  in the card, not at the shell's boundary — which would replace the page. */
class ListBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: unknown) {
    console.error("the Tasks card failed to render", error);
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return <p className="claude-rail-peek-note">The tasks didn&rsquo;t load. Reload the page to try again.</p>;
  }
}

export function TasksPeek() {
  const on = useCanvasRailPeek();
  const card = useTasksCard();
  // …and window B's box (`favBox`): this card is window A's and keeps CLEAR
  // of the favourite's or the bot's card — above it, else to its left
  // (rail-peek-lib `placeClear`) — never closes it (owner, 2026-11-08).
  const { rail, favBox } = useFavPeekState();
  const intent = tasksCardIntent();

  // A card stands behind every rail's door while the flag is on (tasks-peek-
  // store `ready`); off, nothing shows.
  useEffect(() => (on ? registerTasksCard() : undefined), [on]);
  useEffect(() => {
    if (!on) closeTasksCard();
  }, [on]);

  // NO ROOM beside this rail: the door is a plain link until the window, the
  // rail or the row it stands in changes shape (FavouritePeek's rule).
  const [noRoom, setNoRoom] = useState(false);
  useEffect(() => setTasksCardRoom(!noRoom), [noRoom]);
  useEffect(() => {
    setNoRoom(false);
    const re = () => setNoRoom(false);
    window.addEventListener("resize", re);
    const ro = rail && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (rail && ro) {
      ro.observe(rail);
      const r = rowOf(rail);
      if (r) ro.observe(r);
    }
    return () => {
      window.removeEventListener("resize", re);
      ro?.disconnect();
    };
  }, [rail]);
  // …and when window B moves or goes (its box is part of the room; ClaudeRail
  // does the same for the list's card).
  useEffect(() => {
    setNoRoom(false);
  }, [favBox]);
  // A PREVIEW NEVER OUTLIVES ITS RAIL; a pinned card follows the reader to the
  // next page's rail (header).
  const railWas = useRef(rail);
  useEffect(() => {
    if (railWas.current === rail) return;
    railWas.current = rail;
    if (!tasksCardState().pinned) closeTasksCard();
  }, [rail]);

  const up = on && card.up;
  // LAZY, THEN KEPT (header).
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    if (up) setMounted(true);
  }, [up]);

  // THE CHAT IN THE CARD (header): what `openPeek` hands over for a press in
  // the card; Back (or the views' own close) lets it go — and so does the
  // card going down (header): the next opening is the list.
  const [chat, setChat] = useState<{ key: string; anchor: string | null } | null>(null);
  useEffect(() => {
    if (!up) setChat(null);
  }, [up]);
  const token = useRef({});
  useEffect(
    () =>
      registerPeekCard(token.current, {
        open: (key, anchor) => setChat({ key, anchor }),
        close: () => setChat(null),
      }),
    [],
  );
  const peekCtx = useMemo<CardPeek>(() => ({ key: chat?.key ?? null }), [chat?.key]);
  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  useEffect(() => {
    if (!mounted) return;
    setListing(readListing());
    return subscribeListing((ev) => {
      if (!ev.failed) setListing(ev.rows);
    });
  }, [mounted]);
  const task = chat ? (listing?.find((t) => t.key === chat.key) ?? null) : null;
  // THE CHAT'S HEAD IS THE SIDE PEEK'S (owner, 2026-11-08: "the top bar in a
  // chat view is confusing, it only tells me the title — show the one we
  // already show there, just hide the × button"): the Tasks page's own panel
  // head (TaskPeek) — prev/next through the list behind, the status ring,
  // the star, the number and the title (TaskPeekWho), ↗ Open and the chat's ⋮
  // — with Back where its × stood. The walk is the list's PAINTED order, as
  // the page's is (task-peek-store `peekVisibleOrder`), read off this card's
  // own list.
  const chatOrder = useCallback(
    () => peekVisibleOrder(Array.from(listRef.current?.querySelectorAll(`[${PEEK_ITEM_ATTR}]`) ?? [])),
    [],
  );
  const stepChat = (delta: number) => {
    const next = stepPeekKey(chatOrder(), chat?.key ?? null, delta);
    if (next) setChat({ key: next, anchor: null });
  };
  const [chatEnds, setChatEnds] = useState({ prev: false, next: false });
  useEffect(() => {
    if (!chat) return;
    const order = chatOrder();
    setChatEnds({
      prev: stepPeekKey(order, chat.key, -1) !== null,
      next: stepPeekKey(order, chat.key, 1) !== null,
    });
  }, [chat, chatOrder, listing]);

  // WHERE IT STANDS (header): left of the rail, its top level with the rail's
  // Tasks door, at window A's size, clamped inside the row.
  const cardRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const backRef = useRef<HTMLButtonElement | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  const [shownOn, setShownOn] = useState(false);
  const row = useMemo(() => rowOf(rail), [rail]);
  const railLeft = useRef(0);
  const door = railTasksDoor(rail);
  const want = up && !!rail && !!door && !noRoom;
  const place = (): Box | null => {
    const d = railTasksDoor(rail);
    if (!rail || !d || !row) return null;
    const r = rail.getBoundingClientRect();
    railLeft.current = r.left;
    const size = cardSize();
    return placeClear(d.getBoundingClientRect(), r, row.getBoundingClientRect(), favBox, {
      width: size.width,
      maxHeight: size.height,
    });
  };
  // No room: the card goes and the door is a link (above) — and a PRESS that
  // found none takes the link's own way to /tasks.
  const lostRoom = (atOpen: boolean) => {
    const pressed = atOpen && tasksCardState().pinned;
    closeTasksCard();
    setNoRoom(true);
    if (pressed) void navigateUrl("/tasks");
  };

  // THE GRIPS AND THEIR DRAG (RailPeek's; window A's key, `canvasRailPeek`).
  const [resizing, setResizing] = useState<ResizeEdge | null>(null);
  const resizingRef = useRef<ResizeEdge | null>(null);
  resizingRef.current = resizing;
  const dragStart = useRef<{ x: number; y: number; box: Box; pointerId: number; corner: ResizeEdge; el: HTMLElement } | null>(null);
  const dragLast = useRef<Box | null>(null);
  const onGripDown = (corner: ResizeEdge) => (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || !box) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragStart.current = { x: e.clientX, y: e.clientY, box, pointerId: e.pointerId, corner, el: e.currentTarget };
    dragLast.current = null;
    setResizing(corner);
  };
  const onGripMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId || !row) return;
    // Never over window B's card under it (RailPeek's own floor,
    // rail-peek-lib `bottomClear`; owner, 2026-11-08).
    const next = dragResize(
      start.corner,
      start.box,
      e.clientX - start.x,
      e.clientY - start.y,
      row.getBoundingClientRect(),
      bottomClear(start.box, favBox),
    );
    dragLast.current = next;
    setBox((prev) => (prev && sameBox(prev, next) ? prev : next));
  };
  const endDrag = (save: boolean) => {
    const start = dragStart.current;
    dragStart.current = null;
    setResizing(null);
    const last = dragLast.current;
    dragLast.current = null;
    if (start && start.el.hasPointerCapture?.(start.pointerId)) start.el.releasePointerCapture(start.pointerId);
    if (!save || !start || !last || (last.width === start.box.width && last.height === start.box.height)) return;
    // Only a dimension the drag changed, past the grips' minimums (RailPeek
    // `endDrag`); the other keeps what was remembered.
    const prev = cardSize();
    const width = last.width !== start.box.width && last.width >= PEEK_MIN_WIDTH ? last.width : prev.width;
    const height = last.height !== start.box.height && last.height >= PEEK_MIN_HEIGHT ? last.height : prev.height;
    patchUiLayout((d) =>
      d.canvasRailPeek?.width === width && d.canvasRailPeek?.height === height ? d : { ...d, canvasRailPeek: { width, height } },
    );
  };
  const onGripUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId) return;
    endDrag(true);
  };
  const visible = shownOn && box !== null && want;
  useEffect(() => {
    if (dragStart.current && !visible) endDrag(false);
  }, [visible]);
  // A ui-layout hydrate (or another card's drag) lands a new size: re-render.
  const [, setLayoutTick] = useState(0);
  useEffect(() => subscribeUiLayout(() => setLayoutTick((n) => n + 1)), []);

  // PLACED, THEN SHOWN (RailPeek header): the box on this frame, the class on
  // the next; a close keeps the box, so the fade has somewhere to fade.
  useLayoutEffect(() => {
    if (!want) {
      setShownOn(false);
      return;
    }
    const next = place();
    if (!next) {
      lostRoom(true);
      return;
    }
    setBox(next);
    const raf = requestAnimationFrame(() => setShownOn(true));
    return () => cancelAnimationFrame(raf);
  }, [want, rail]);
  // FOLLOW THE DOOR while up: a resize, the row changing shape; plus every
  // render, cheaply.
  useEffect(() => {
    if (!want || !rail) return;
    const re = () => {
      if (resizingRef.current) return;
      const next = place();
      if (!next) {
        lostRoom(false);
        return;
      }
      setBox((prev) => (prev && sameBox(prev, next) ? prev : next));
    };
    window.addEventListener("resize", re);
    const ro = row && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (row && ro) ro.observe(row);
    return () => {
      window.removeEventListener("resize", re);
      ro?.disconnect();
    };
  }, [want, rail, row, favBox]);
  useEffect(() => {
    if (!want || resizing || !box) return;
    const next = place();
    if (next && !sameBox(next, box)) setBox(next);
  });

  // THE SAFE CONE from the door to the card's near edge, and the BRIDGE over
  // the gap to the rail (RailPeek; rail-peek-lib `inSafeCone`).
  const coneOn = useRef(false);
  const setCone = (inside: boolean) => {
    if (coneOn.current === inside) return;
    coneOn.current = inside;
    if (inside) intent.enterCone();
    else intent.leaveCone();
  };
  useEffect(() => {
    if (!shownOn || !box || !door) return;
    const r = door.getBoundingClientRect();
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
  }, [shownOn, box, door]);

  // A PRESS INSIDE pins a preview (PeekIntent `pressStart`), and makes the
  // press the card's for `openPeek` (task-peek-store `notePressInCard`) —
  // React's capture phase, so a menu or modal the views portal out counts.
  useEffect(() => {
    const upNow = () => intent.pressEnd();
    window.addEventListener("pointerup", upNow, true);
    window.addEventListener("pointercancel", upNow, true);
    return () => {
      window.removeEventListener("pointerup", upNow, true);
      window.removeEventListener("pointercancel", upNow, true);
    };
  }, [intent]);
  const claim = () => notePressInCard(token.current);

  // INERT WHILE HIDDEN (RailPeek), and the list inert under an open chat.
  useLayoutEffect(() => {
    cardRef.current?.toggleAttribute("inert", !visible);
  }, [visible]);
  useLayoutEffect(() => {
    listRef.current?.toggleAttribute("inert", !!chat);
  }, [chat, mounted]);

  // THE KEYBOARD IS NEVER LEFT ON THE BODY: a card that shuts with the focus
  // inside hands it to the door; a chat opening over the list (whose pressed
  // row just went inert) puts it on Back; Back puts it on the ×.
  const handBack = useCallback(() => {
    const a = document.activeElement;
    if (a && cardRef.current?.contains(a)) railTasksDoor(rail)?.focus({ preventScroll: true });
  }, [rail]);
  const close = useCallback(() => {
    handBack();
    closeTasksCard(true);
  }, [handBack]);
  const chatKey = chat?.key ?? null;
  const chatWas = useRef<string | null>(null);
  useEffect(() => {
    const was = chatWas.current;
    chatWas.current = chatKey;
    if (!visible) return;
    const a = document.activeElement;
    const lost = !a || a === document.body || !!cardRef.current?.contains(a);
    if (chatKey && !was && lost) backRef.current?.focus({ preventScroll: true });
    else if (!chatKey && was && lost) closeRef.current?.focus({ preventScroll: true });
  }, [chatKey, visible]);

  // THE DOOR'S ENTER LANDS THE KEYBOARD HERE (tasks-peek-store
  // `focusTasksCard`): on Back over a chat, else on the ×.
  useEffect(
    () =>
      registerTasksCardFocus(() => {
        (backRef.current ?? closeRef.current)?.focus({ preventScroll: true });
        return !!cardRef.current?.contains(document.activeElement);
      }),
    [],
  );

  // WHICH FOCUS HOLDS THE CARD (RailPeek): an editable, a popup of its own,
  // or focus the keyboard brought.
  const lastKeyAt = useRef(0);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" || e.key.startsWith("Arrow")) lastKeyAt.current = Date.now();
    };
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
    const editable = t.matches("textarea, input, select, [contenteditable=''], [contenteditable='true']");
    const pop = t.closest("[role='menu'], [role='listbox'], [role='dialog'], [role='alertdialog']");
    const popup = !!pop && pop !== cardRef.current;
    const keyboard = Date.now() - lastKeyAt.current < 400;
    intent.hold(editable || popup || keyboard);
  };
  const onBlurCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget as Node | null;
    if (next && e.currentTarget.contains(next)) return;
    intent.hold(false);
  };
  // ESCAPE closes a PREVIEW (a pinned card closes by its ×, the door or Open
  // only); never from a popup inside the card, whose Escape is its own.
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape" || e.defaultPrevented || card.pinned) return;
    const t = e.target as HTMLElement;
    const pop = t.closest("[role='menu'], [role='listbox'], [role='dialog'], [role='alertdialog']");
    if (pop && pop !== cardRef.current) return;
    e.preventDefault();
    close();
  };
  // …and with the focus nowhere (a hover's preview: the pointer brought it,
  // the keyboard is on the body), the document's Escape does the same.
  const previewing = visible && !card.pinned;
  useEffect(() => {
    if (!previewing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (e.target !== document.body && e.target !== document.documentElement) return;
      e.preventDefault();
      closeTasksCard();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [previewing]);
  const onChatEscape = () => {
    if (!tasksCardState().pinned) close();
  };
  // OPEN: the /tasks page itself; the card goes once the hop went (a leave
  // guard may refuse it — FavouritePeek's rule).
  const openPage = (href = "/tasks") => {
    void navigateUrl(href).then((went) => {
      if (went !== false) close();
    });
  };

  // MARK READ LIKE THE PAGE DOES (task-open-read): the open chat, while the
  // card shows it and the window is in view.
  const pulseRows = useTasksPulseRows(mounted);
  const docVisible = useDocumentVisible();
  const readKeys = useMemo(() => (visible && chatKey && docVisible ? [chatKey] : []), [visible, chatKey, docVisible]);
  useAutoReadOpenTasks(pulseRows, readKeys);

  if (!on) return null;
  const title = chat ? (task ? cardTitleLine(task).text || task.title || "Chat" : "Chat") : "Tasks";
  const bridge =
    box && railLeft.current > box.left + box.width + 1
      ? { left: box.left + box.width, top: box.top, width: Math.max(0, railLeft.current - 3 - (box.left + box.width)), height: box.height }
      : null;

  return createPortal(
    <>
      {bridge && (
        <div
          className={"claude-rail-peek-bridge" + (visible ? " is-on" : "")}
          aria-hidden="true"
          style={bridge}
          onPointerEnter={() => setCone(true)}
          onPointerLeave={() => setCone(false)}
        />
      )}
      <div
        ref={cardRef}
        id={TASKS_PEEK_ID}
        className={"claude-rail-peek claude-rail-tasks-peek favs-on" + (visible ? " is-on" : "") + (resizing ? " is-resizing" : "")}
        role="dialog"
        aria-label={chat ? `${title} — chat` : "Tasks"}
        style={box ? { left: box.left, top: box.top, width: box.width, height: box.height } : undefined}
        onPointerEnter={() => intent.enterCard()}
        onPointerLeave={() => intent.leaveCard()}
        onPointerDownCapture={() => {
          claim();
          intent.pressStart();
        }}
        onClickCapture={claim}
        onKeyDownCapture={claim}
        onFocusCapture={onFocusCapture}
        onBlurCapture={onBlurCapture}
        onKeyDown={onKeyDown}
      >
        {visible &&
          ALL_GRIPS.map((corner) => (
            <div
              key={corner}
              className={"claude-rail-peek-grip is-" + corner + (resizing === corner ? " is-active" : "")}
              aria-hidden="true"
              onPointerDown={onGripDown(corner)}
              onPointerMove={onGripMove}
              onPointerUp={onGripUp}
              onPointerCancel={onGripUp}
              onLostPointerCapture={() => dragStart.current && endDrag(true)}
            />
          ))}
        <div className="claude-rail-peek-card">
          {chat ? (
            // THE SIDE PEEK'S HEAD, Back in the ×'s corner (`chatOrder` above).
            <header className="task-side-peek-head claude-rail-tasks-chat-head">
              <div className="task-side-peek-acts">
                <button
                  ref={backRef}
                  type="button"
                  className="task-side-peek-btn claude-rail-tasks-back"
                  aria-label="Back to tasks"
                  data-hint="Back to tasks"
                  onClick={() => setChat(null)}
                >
                  <ArrowLeft size={16} strokeWidth={1.6} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="task-side-peek-btn"
                  aria-label="Previous task"
                  data-hint="Previous task"
                  disabled={!chatEnds.prev}
                  onClick={() => stepChat(-1)}
                >
                  <ChevronUp size={16} strokeWidth={1.6} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="task-side-peek-btn"
                  aria-label="Next task"
                  data-hint="Next task"
                  disabled={!chatEnds.next}
                  onClick={() => stepChat(1)}
                >
                  <ChevronDown size={16} strokeWidth={1.6} aria-hidden="true" />
                </button>
              </div>
              {task ? <TaskPeekWho task={task} star /> : <span className="task-side-peek-idle" aria-hidden />}
              {task && (
                <div className="task-side-peek-marks">
                  {taskHref(task) && (
                    <a
                      className="task-side-peek-open"
                      href={taskHref(task) ?? undefined}
                      aria-label="Open this task"
                      data-hint="Open this task"
                      onClick={(e) => {
                        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                        e.preventDefault();
                        const href = taskHref(task);
                        if (href) openPage(href);
                      }}
                    >
                      <ArrowUpRight size={13} strokeWidth={1.6} aria-hidden="true" />
                      Open
                    </a>
                  )}
                  {task.session_id && (
                    <span className="claude-rail-peek-kebab">
                      <AgentProvider backend={task.backend ?? ""}>
                        <Kebab
                          key={task.key}
                          file={taskChatTarget(task)}
                          sessionId={task.session_id}
                          running={taskColumn(task) === "in_progress" || taskColumn(task) === "queued"}
                          asking={taskColumn(task) === "needs_attention"}
                          queued={taskColumn(task) === "queued"}
                          onErased={() => setChat(null)}
                          onArchived={() => setChat(null)}
                        />
                      </AgentProvider>
                    </span>
                  )}
                </div>
              )}
            </header>
          ) : (
            <div className="claude-rail-peek-head">
              <div className="claude-rail-peek-row">
                <button
                  ref={closeRef}
                  type="button"
                  className="claude-rail-peek-btn claude-rail-peek-close"
                  aria-label="Close Tasks"
                  title="Close"
                  onClick={close}
                >
                  <X size={14} aria-hidden="true" />
                </button>
                <span className="claude-rail-tasks-title" title={title}>
                  {title}
                </span>
                <span className="claude-rail-peek-gap" aria-hidden="true" />
                <button type="button" className="btn claude-rail-peek-open" title="Open the Tasks page" onClick={() => openPage()}>
                  Open
                </button>
              </div>
            </div>
          )}
          <div className="claude-rail-peek-body">
            {mounted && (
              <div ref={listRef} className={"claude-rail-peek-chat claude-rail-tasks-list" + (chat ? "" : " is-shown")}>
                <CardPeekContext.Provider value={peekCtx}>
                  <ListBoundary>
                    <Suspense fallback={<p className="claude-rail-peek-note">Loading…</p>}>
                      <Scheduled inCard />
                    </Suspense>
                  </ListBoundary>
                </CardPeekContext.Provider>
              </div>
            )}
            {chat && (
              <div key={chat.key} className="claude-rail-peek-chat is-shown">
                {task?.session_id ? (
                  <ChatMount
                    onBreakout={breakoutOpener()}
                    file={taskChatTarget(task)}
                    sessionId={task.session_id}
                    // The task's own settings, a seed (TaskPeek passes the same).
                    {...(task.model ? { model: task.model } : {})}
                    {...(task.effort ? { effort: task.effort } : {})}
                    {...(task.backend ? { backend: task.backend } : {})}
                    // A message row's press names one turn of it.
                    {...(chat.anchor ? { msgAnchor: chat.anchor } : {})}
                    chatOnly
                    peek
                    noFocus
                    noAnnTools
                    paramsSource="memory"
                    onEscape={onChatEscape}
                    onNavigate={(href) => void navigateUrl(href)}
                    smallCard
                    smallCardShown={visible}
                  />
                ) : (
                  <p className="claude-rail-peek-note">{listing ? "This task has no conversation yet." : "Loading…"}</p>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </>,
    document.body,
  );
}

function sameBox(a: Box, b: Box): boolean {
  return a.left === b.left && a.top === b.top && a.width === b.width && a.height === b.height;
}
