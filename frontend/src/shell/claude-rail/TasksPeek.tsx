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
// WINDOW A'S CHASSIS IS DoorCard's (2026-11-09; the Bots card shares it): the
// rail card's classes, its two-frame show and fade, bridge and safe cone,
// inert while hidden, WINDOW A'S SIZE (`canvasRailPeek`) and grips, window
// A's timings (tasks-peek-store's PeekIntent), the keyboard's seats.
//
// IT IS WINDOW A (ClaudeRail; owner, 2026-11-08: "the Tasks window is like
// window A, it shouldn't affect anything else"): it opens wherever window A
// opens, pane open or shut; its opening puts down the list's card, the +
// popover and the Bots card, and any of those opening closes it — a ring's
// press beside the OPEN pane opens that chat in the pane and closes this
// card, and the pane's chat changing under it closes it too. Window B (the
// favourite's or the bot's card) is the other section's: never closed by this
// card, never closes it — this card keeps clear of it (DoorCard `favBox`).
//
// THE HEAD is one row, the bot card's: the × in the top-left corner, Back
// beside it while a chat is open, the small title ("Tasks", or the chat's),
// and Open — the /tasks page itself — on the right.
import { Component, lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, ArrowUpRight, ChevronDown, ChevronUp, X } from "lucide-react";
import type { Task } from "@platform/lib/api";
import { breakoutOpener } from "@platform/lib/breakouts";
import { navigateUrl } from "@platform/lib/router";
import { ChatMount } from "@apps/claude";
import { Kebab } from "@apps/claude/ui/Kebab";
import type { BreakoutControl } from "@apps/claude/ClaudeChat";
import { opensInSidePane, requestSideMaximized } from "@platform/lib/side-maximized-store";
import { AgentProvider } from "@apps/claude/ui/agent-context";
import { readListing, subscribeListing, useTasksPulseRows } from "@shell/tasksPulse";
import { cardTitleLine, pageDoorLabel, taskChatTarget, taskColumn, taskHref } from "@shell/tasks-lib";
import { TaskPeekWho } from "@shell/TaskPeekWho";
// THE SHARED TASK HEAD (design.md, "One task head, everywhere"), behind its
// flag: both of this card's heads — the list's and the chat's — on one chassis
// at one height, so the list↔chat swap no longer jumps 26→44px.
import { TaskHead } from "@shell/TaskHead";
import { useTaskHeadUnified } from "@shell/task-head-flag";
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
import { DoorCard } from "./DoorCard";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { tasksCardStore, useTasksCard } from "./tasks-peek-store";

// The page's own chunk (App.tsx lazy-loads the same module): not the shell's
// to carry until the door is first hovered.
const Scheduled = lazy(() => import("@shell/Scheduled"));

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
  const up = on && card.up;
  const [visible, setVisible] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);
  // LAZY, THEN KEPT (header).
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    if (up) setMounted(true);
  }, [up]);

  // THE CHAT IN THE CARD (header): what `openPeek` hands over for a press in
  // the card; Back (or the views' own close) lets it go — and so does the
  // card going down (header): the next opening is the list.
  const [chat, setChat] = useState<{ key: string; anchor: string | null } | null>(null);
  // The open chat's Breakout, as it reports it (`onBreakoutControl`, TaskPeek's
  // route) — so this card's ⋮ carries the pane chat's items too (Akshil,
  // 2026-11-09). `setState` is stable, so the chat's report effect is not
  // re-fired by a fresh callback.
  const [breakoutCtl, setBreakoutCtl] = useState<BreakoutControl | null>(null);
  const unified = useTaskHeadUnified();
  /** The shared head's chip seat (ChatMount `headChipsHost`): the card's chat
   *  shows its worktree pill there — it showed none before. */
  const [chipsHost, setChipsHost] = useState<HTMLElement | null>(null);
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

  // The list inert under an open chat.
  useLayoutEffect(() => {
    listRef.current?.toggleAttribute("inert", !!chat);
  }, [chat, mounted]);

  // MARK READ LIKE THE PAGE DOES (task-open-read): the open chat, while the
  // card shows it and the window is in view.
  const chatKey = chat?.key ?? null;
  const pulseRows = useTasksPulseRows(mounted);
  const docVisible = useDocumentVisible();
  const readKeys = useMemo(() => (visible && chatKey && docVisible ? [chatKey] : []), [visible, chatKey, docVisible]);
  useAutoReadOpenTasks(pulseRows, readKeys);

  const claim = useCallback(() => notePressInCard(token.current), []);
  const title = chat ? (task ? cardTitleLine(task).text || task.title || "Chat" : "Chat") : "Tasks";

  return (
    <DoorCard
      store={tasksCardStore}
      href="/tasks"
      className="claude-rail-tasks-peek"
      ariaLabel={chat ? `${title} — chat` : "Tasks"}
      subOpen={!!chat}
      onClaim={claim}
      onVisible={setVisible}
    >
      {(api) => (
        <>
          {unified ? (
            chat ? (
              <TaskHead
                key="chat"
                className="claude-rail-tasks-chat-head"
                lead={{ kind: "back", label: "Back to tasks", onPress: () => setChat(null), ref: api.backRef }}
                nav={{ prev: chatEnds.prev, next: chatEnds.next, step: stepChat }}
                task={task}
                chipsRef={setChipsHost}
                open={
                  task && taskHref(task)
                    ? { href: taskHref(task)!, label: pageDoorLabel(task), onOpen: (h) => api.openPage(h) }
                    : null
                }
                kebab={task?.session_id ? (
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
                      {...(taskHref(task) && opensInSidePane(taskHref(task)!) ? {
                        fullScreen: {
                          on: false,
                          onToggle: () => {
                            const href = taskHref(task);
                            if (!href) return;
                            const sid = task.session_id;
                            void api.openPage(href).then((went) => {
                              if (went !== false) requestSideMaximized(sid);
                            });
                          },
                        },
                      } : {})}
                      {...(breakoutCtl?.available ? {
                        breakout: { picking: breakoutCtl.picking, onToggle: breakoutCtl.toggle },
                      } : {})}
                    />
                  </AgentProvider>
                ) : null}
              />
            ) : (
              <TaskHead
                key="list"
                className="claude-rail-tasks-list-head"
                lead={{ kind: "close", label: "Close Tasks", hint: "Close", onPress: api.close, ref: api.closeRef }}
                task={null}
                title="Tasks"
                // No ⋮ to carry a folded door, and "Open tasks" is the only
                // one: this head never folds.
                fitEnabled={false}
                // "Open tasks", not the other cards' "Open" (owner,
                // 2026-11-08): this door is the page's, not a chat's.
                open={{ href: "/tasks", label: "Open the Tasks page", word: "Open tasks", onOpen: () => api.openPage() }}
              />
            )
          ) : chat ? (
            // THE SIDE PEEK'S HEAD, Back in the ×'s corner (`chatOrder` above).
            <header className="task-side-peek-head claude-rail-tasks-chat-head">
              <div className="task-side-peek-acts">
                <button
                  ref={api.backRef}
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
                        if (href) api.openPage(href);
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
                          // FULL SCREEN: the Open door, then the Claude panel
                          // filling the window (side-maximized-store).
                          {...(taskHref(task) && opensInSidePane(taskHref(task)!) ? {
                            fullScreen: {
                              on: false,
                              onToggle: () => {
                                const href = taskHref(task);
                                if (!href) return;
                                // A request the side-pane chat takes once
                                // it is mounted there (side-maximized-store) —
                                // made only once the hop went.
                                const sid = task.session_id;
                                void api.openPage(href).then((went) => {
                                  if (went !== false) requestSideMaximized(sid);
                                });
                              },
                            },
                          } : {})}
                          {...(breakoutCtl?.available ? {
                            breakout: { picking: breakoutCtl.picking, onToggle: breakoutCtl.toggle },
                          } : {})}
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
                  ref={api.closeRef}
                  type="button"
                  className="claude-rail-peek-btn claude-rail-peek-close"
                  aria-label="Close Tasks"
                  title="Close"
                  onClick={api.close}
                >
                  <X size={14} aria-hidden="true" />
                </button>
                <span className="claude-rail-tasks-title" title={title}>
                  {title}
                </span>
                <span className="claude-rail-peek-gap" aria-hidden="true" />
                {/* "Open tasks", not the other cards' "Open" (owner, 2026-11-08):
                    this door is the page's, not a chat's. */}
                <button type="button" className="btn claude-rail-peek-open" title="Open the Tasks page" onClick={() => api.openPage()}>
                  Open tasks
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
                    onBreakoutControl={setBreakoutCtl}
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
                    onEscape={api.onSubEscape}
                    onNavigate={(href) => void navigateUrl(href)}
                    smallCard
                    smallCardShown={api.visible}
                    {...(unified ? { headChipsHost: chipsHost } : {})}
                  />
                ) : (
                  <p className="claude-rail-peek-note">{listing ? "This task has no conversation yet." : "Loading…"}</p>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </DoorCard>
  );
}
