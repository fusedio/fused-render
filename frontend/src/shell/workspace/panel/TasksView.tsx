// THE PANEL'S TASKS (SPEC §56 WS-19): the Tasks page as the rail's Tasks card
// draws it (`Scheduled inCard` in a `CardPeekContext`, TasksPeek.tsx) without
// the card chassis. A task's press opens its chat as a PEEK: beside the list
// when the panel is wide enough (the list stays live), else in the list's
// place with a Back (the list kept mounted, hidden and inert, as the card
// does). The peeked key is the host URL's (`_right=tasks:<key>`), so Back
// closes it and a reload keeps it.
//
// DUPLICATED FROM TasksPeek, the minimum: the chat block and the `pending:`
// rekey. TasksPeek's own copy is wired into DoorCard's api (pin, Escape,
// focus seats) and sharing it would change that card's behaviour.
import { Component, lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import { getSchedule, type Task } from "@platform/lib/api";
import { pendingEntryId, queueCaption } from "@platform/lib/queue";
import { breakoutOpener } from "@platform/lib/breakouts";
import { ChatMount } from "@apps/claude";
import { Kebab } from "@apps/claude/ui/Kebab";
import type { BreakoutControl } from "@apps/claude/ClaudeChat";
import { AgentProvider } from "@apps/claude/ui/agent-context";
import { ForceStartButton, queuedLine } from "@shell/ForceStartButton";
import { readListing, subscribeListing, useTasksPulseRows } from "@shell/tasksPulse";
import { pageDoorLabel, taskChatTarget, taskColumn, taskHref } from "@shell/tasks-lib";
import { TaskHead } from "@shell/TaskHead";
import { TaskPrChip } from "@shell/TaskPrChip";
import { CardPeekContext, notePressInCard, registerPeekCard, type CardPeek } from "@shell/task-peek-store";
import { useAutoReadOpenTasks, useDocumentVisible } from "@shell/task-open-read";

const Scheduled = lazy(() => import("@shell/Scheduled"));

/** Side by side from here (the list's room + the chat's 400px floor). */
export const TASKS_SPLIT_W = 780;

class ListBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: unknown) {
    console.error("the Tasks panel failed to render", error);
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return <p className="claude-rail-peek-note">The tasks didn&rsquo;t load. Reload the page to try again.</p>;
  }
}

export interface TasksViewProps {
  peek: string | null;
  /** A press in the list (a push), Back / close (null), a rekey (replace). */
  onPeek: (key: string | null, how?: "push" | "replace") => void;
  onHide: () => void;
  onNavigate: (url: string) => void;
}

export function TasksView({ peek, onPeek, onHide, onNavigate }: TasksViewProps) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const [wide, setWide] = useState(false);
  useLayoutEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const measure = () => setWide(el.getBoundingClientRect().width >= TASKS_SPLIT_W);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // A press in the list reaches THIS view (task-peek-store's card doors).
  const token = useRef({});
  const onPeekRef = useRef(onPeek);
  onPeekRef.current = onPeek;
  useEffect(
    () =>
      registerPeekCard(token.current, {
        open: (key) => onPeekRef.current(key, "push"),
        close: () => onPeekRef.current(null, "push"),
      }),
    [],
  );
  const claim = () => notePressInCard(token.current);
  const peekCtx = useMemo<CardPeek>(() => ({ key: peek }), [peek]);

  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  useEffect(
    () =>
      subscribeListing((ev) => {
        if (!ev.failed) setListing(ev.rows);
      }),
    [],
  );
  const task = peek ? (listing?.find((t) => t.key === peek) ?? null) : null;

  // A QUEUED CHAT IS REKEYED WHEN IT STARTS (TasksPeek's rule): once its
  // `pending:` row has left the listing, the entry names its session.
  const startedEntry = peek && listing && !task ? pendingEntryId(peek) : "";
  const [lostEntry, setLostEntry] = useState("");
  useEffect(() => {
    if (!startedEntry) return;
    let alive = true;
    let tries = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const again = (ms: number) => {
      if (!alive) return;
      if (++tries < 30) timer = setTimeout(look, ms);
      else setLostEntry(startedEntry);
    };
    const look = () => {
      void getSchedule().then(
        (res) => {
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
          const row = readListing()?.find((t) => t.session_id === session);
          if (!row && tries < 29) return again(500);
          onPeekRef.current(row?.key ?? session, "replace");
        },
        () => again(2000),
      );
    };
    look();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [startedEntry]);

  const split = wide && !!peek;
  // Narrow, the list is hidden under the chat and inert.
  useLayoutEffect(() => {
    listRef.current?.toggleAttribute("inert", !!peek && !split);
  }, [peek, split]);

  // Read like the page does: the open chat while it is on screen.
  const pulseRows = useTasksPulseRows(true);
  const docVisible = useDocumentVisible();
  const readKeys = useMemo(() => (peek && docVisible ? [peek] : []), [peek, docVisible]);
  useAutoReadOpenTasks(pulseRows, readKeys);

  const [breakoutCtl, setBreakoutCtl] = useState<BreakoutControl | null>(null);
  const [chipsHost, setChipsHost] = useState<HTMLElement | null>(null);
  const [statusHost, setStatusHost] = useState<HTMLElement | null>(null);
  const hideLead = { kind: "close" as const, label: "Hide the panel", onPress: onHide };

  const chatHead = (
    <TaskHead
      key="chat"
      className="ws-panel-head"
      // Narrow: the chat holds the panel, ← is the way back to the list and
      // the panel's hide stands before it. Wide: the list beside it has the
      // hide; the chat's × closes the peek only.
      {...(split
        ? { lead: { kind: "close" as const, label: "Close the chat", onPress: () => onPeek(null, "push") } }
        : {
            preLead: (
              <button type="button" className="task-head-btn task-head-lead is-close" aria-label="Hide the panel" data-hint="Hide the panel" onClick={onHide}>
                <X size={14} strokeWidth={2} aria-hidden="true" />
              </button>
            ),
            lead: { kind: "back" as const, label: "Back to tasks", onPress: () => onPeek(null, "push") },
          })}
      task={task}
      chips={task ? <TaskPrChip task={task} /> : null}
      chipsRef={setChipsHost}
      statusRef={setStatusHost}
      onFiled={(kind) => {
        if (kind === "archive") onPeek(null, "push");
      }}
      open={task && taskHref(task) ? { href: taskHref(task)!, label: pageDoorLabel(task), quiet: true, onOpen: (h) => onNavigate(h) } : null}
      kebab={
        task?.session_id ? (
          <AgentProvider backend={task.backend ?? ""}>
            <Kebab
              key={task.key}
              file={taskChatTarget(task)}
              sessionId={task.session_id}
              running={taskColumn(task) === "in_progress" || taskColumn(task) === "queued"}
              asking={taskColumn(task) === "needs_attention"}
              queued={taskColumn(task) === "queued"}
              onErased={() => onPeek(null, "push")}
              onArchived={() => onPeek(null, "push")}
              {...(breakoutCtl?.available ? { breakout: { picking: breakoutCtl.picking, onToggle: breakoutCtl.toggle } } : {})}
            />
          </AgentProvider>
        ) : null
      }
    />
  );

  return (
    <div ref={boxRef} className={"ws-panel-tasks" + (split ? " is-split" : "")} onPointerDownCapture={claim} onClickCapture={claim} onKeyDownCapture={claim}>
      <div ref={listRef} className={"ws-panel-tasks-list" + (peek && !split ? " is-hidden" : "")}>
        <TaskHead key="list" className="ws-panel-head" lead={hideLead} task={null} title="Tasks" fitEnabled={false} open={null} />
        <div className="ws-panel-body">
          <CardPeekContext.Provider value={peekCtx}>
            <ListBoundary>
              <Suspense fallback={<p className="claude-rail-peek-note">Loading…</p>}>
                <Scheduled inCard />
              </Suspense>
            </ListBoundary>
          </CardPeekContext.Provider>
        </div>
      </div>
      {peek && (
        <div key={peek} className="ws-panel-tasks-chat">
          {chatHead}
          <div className="ws-panel-body">
            {task?.session_id ? (
              <ChatMount
                onBreakout={breakoutOpener()}
                onBreakoutControl={setBreakoutCtl}
                file={taskChatTarget(task)}
                sessionId={task.session_id}
                {...(task.model ? { model: task.model } : {})}
                {...(task.effort ? { effort: task.effort } : {})}
                {...(task.backend ? { backend: task.backend } : {})}
                chatOnly
                peek
                noAnnTools
                paramsSource="memory"
                onNavigate={onNavigate}
                headChipsHost={chipsHost}
                headStatusHost={statusHost}
              />
            ) : task && queueCaption(task) ? (
              <div className="claude-rail-peek-note is-stack">
                <span>{queuedLine(task)}</span>
                <ForceStartButton facts={task} target={{ key: task.key }} />
              </div>
            ) : (
              <p className="claude-rail-peek-note">
                {!listing ? "Loading…" : startedEntry && lostEntry !== startedEntry ? "Starting the task…" : "This task has no conversation yet."}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
