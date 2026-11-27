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
import { Component, lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { getSchedule, type Task } from "@platform/lib/api";
import { pendingEntryId, queueCaption } from "@platform/lib/queue";
import { ForceStartButton, queuedLine } from "@shell/ForceStartButton";
import { breakoutOpener } from "@platform/lib/breakouts";
import { navigateUrl } from "@platform/lib/router";
import { ChatMount } from "@apps/claude";
import { Kebab } from "@apps/claude/ui/Kebab";
import type { BreakoutControl } from "@apps/claude/ClaudeChat";
import { opensInSidePane, requestSideMaximized } from "@platform/lib/side-maximized-store";
import { AgentProvider } from "@apps/claude/ui/agent-context";
import { readListing, subscribeListing, useTasksPulseRows } from "@shell/tasksPulse";
import { cardTitleLine, pageDoorLabel, taskChatTarget, taskColumn, taskHref } from "@shell/tasks-lib";
// THE SHARED TASK HEAD (design.md, "One task head, everywhere"): both of
// this card's heads — the list's and the chat's — on one chassis at one
// height, so the list↔chat swap does not jump.
import { TaskHead } from "@shell/TaskHead";
import { TaskPrChip } from "@shell/TaskPrChip";
import { useUrlVersion } from "@platform/lib/hooks";
import { openInTasksItem, openInTasksProps } from "@platform/ui/openInTasks";
import {
  CardPeekContext,
  notePressInCard,
  registerPeekCard,
  type CardPeek,
} from "@shell/task-peek-store";
import { useAutoReadOpenTasks, useDocumentVisible } from "@shell/task-open-read";
import { DoorCard } from "./DoorCard";
import { RailPinButton } from "./RailPin";
import { isWorkspaceHost } from "@platform/lib/workspace-bridge";
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
  const card = useTasksCard();
  const up = card.up;
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
  // "Open in Tasks" reads the page's path: a card kept across a hop re-reads it.
  useUrlVersion();
  /** The shared head's chip seat (ChatMount `headChipsHost`): the card's chat
   *  shows its worktree pill there — it showed none before. */
  const [chipsHost, setChipsHost] = useState<HTMLElement | null>(null);
  /** …and its status seat, for the chat's pause word (ChatMount
   *  `headStatusHost`) — on no rung of the head's ladder. */
  const [statusHost, setStatusHost] = useState<HTMLElement | null>(null);
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
  // A CHAT THAT WAS QUEUED WITH NO SESSION IS REKEYED WHEN IT STARTS (Bugbot,
  // PR #2348): its row is `pending:<entry>` until its run mints a session, and
  // then it is that session's row — so the key this card was opened on names
  // nothing, and the body fell back to "no conversation yet" over a task that
  // had just been force-started from this very card. The entry id is the name
  // that does not move (`api_queue_force`'s own note): once the pending row has
  // left the listing, the entry is asked which session it became and the card
  // follows it there.
  const startedEntry = chat && listing && !task ? pendingEntryId(chat.key) : "";
  // …AND AN ENTRY THAT IS NOT BECOMING A SESSION SAYS SO: cancelled or failed
  // before it ran, or silent for the whole look. The body goes back to "no
  // conversation yet" rather than "Starting…" for ever.
  const [lostEntry, setLostEntry] = useState("");
  useEffect(() => {
    if (!startedEntry) return;
    let alive = true;
    let tries = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const again = (ms: number) => {
      // Half a minute of looking: a run that has not said its session by then
      // is not about to, and Back is the way out.
      if (!alive) return;
      if (++tries < 30) timer = setTimeout(look, ms);
      else setLostEntry(startedEntry);
    };
    const look = () => {
      void getSchedule().then((res) => {
        if (!alive) return;
        const entry = res.entries.find((e) => e.id === startedEntry);
        const session = entry?.claude_session_id;
        if (!session) {
          if (entry && (entry.state === "error" || entry.state === "cancelled" || entry.state === "missed")) {
            setLostEntry(startedEntry);
            return;
          }
          return again(1000);
        }
        // ONTO THE ROW, once the listing has it: the schedule can name the
        // session a beat before the listing rekeys, and following it there
        // early would show an empty card for that beat. If the row never
        // arrives in the time, the session itself is the key (a row is keyed
        // by its session). The anchor a press named is kept.
        const row = readListing()?.find((t) => t.session_id === session);
        if (!row && tries < 29) return again(500);
        const key = row?.key ?? session;
        setChat((was) => (was && pendingEntryId(was.key) === startedEntry ? { key, anchor: was.anchor } : was));
      }, () => again(2000));
    };
    look();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [startedEntry]);
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
          {chat ? (
            <TaskHead
              key="chat"
              reveal="window"
              className="claude-rail-tasks-chat-head"
              // × THEN ← (Akshil, 2026-11-16): the card's own close stands in
              // the corner on the chat too, the way back to the list after
              // it. No ↑↓ here — the list is one ← away. …but for the pin:
              // no card has a × with it (RailPeek's note).
              lead={{ kind: "back", label: "Back to tasks", onPress: () => setChat(null), ref: api.backRef }}
              pinNode={<RailPinButton pinned={api.pin.pinned} onToggle={api.pin.onToggle} />}
              task={task}
              // The task's pull request, after the chat's branch tag
              // (Akshil, 2026-11-19: "a PR thing on this tab").
              chips={task ? <TaskPrChip task={task} /> : null}
              chipsRef={setChipsHost}
              statusRef={setStatusHost}
              // The ring's Archive leaves the chat for the list, as the ⋮'s.
              onFiled={(kind) => {
                if (kind === "archive") setChat(null);
              }}
              open={
                task && taskHref(task)
                  ? { href: taskHref(task)!, label: pageDoorLabel(task), quiet: true, onOpen: (h) => api.openPage(h) }
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
                    {...openInTasksProps(
                      openInTasksItem({ on: true, taskKey: task.key, onOpen: (href) => void api.openPage(href) }),
                    )}
                    {...(taskHref(task) && opensInSidePane(taskHref(task)!) && !isWorkspaceHost() ? {
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
              // With the pin: no ×, and the pin is where the keyboard lands.
              lead={null}
              pinNode={<RailPinButton btnRef={api.closeRef} pinned={api.pin.pinned} onToggle={api.pin.onToggle} />}
              task={null}
              title="Tasks"
              // No ⋮ to carry a folded door, and "Open tasks" is the only
              // one: this head never folds.
              fitEnabled={false}
              // "Open tasks", not the other cards' "Open" (owner,
              // 2026-11-08): this door is the page's, not a chat's.
              open={{ href: "/tasks", label: "Open the Tasks page", word: "Open tasks", quiet: true, onOpen: () => api.openPage() }}
            />
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
                    headChipsHost={chipsHost}
                    headStatusHost={statusHost}
                  />
                ) : task && queueCaption(task) ? (
                  // QUEUED, AND NO CHAT YET: the line it stands in, and the way
                  // to stop standing in it (Akshil, 2026-11-18).
                  <div className="claude-rail-peek-note is-stack">
                    <span>{queuedLine(task)}</span>
                    <ForceStartButton facts={task} target={{ key: task.key }} />
                  </div>
                ) : (
                  <p className="claude-rail-peek-note">
                    {!listing
                      ? "Loading…"
                      : startedEntry && lostEntry !== startedEntry
                        ? "Starting the task…"
                        : "This task has no conversation yet."}
                  </p>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </DoorCard>
  );
}
