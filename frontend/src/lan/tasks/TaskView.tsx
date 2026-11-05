// /tasks/<key> — one task. A strip on top (status, folder, agent, PR chips,
// queue place, next run), the conversation in the middle (prompts on the
// right; replies, tool lines and cards on the left), the composer at the
// bottom riding on the keyboard, Stop beside Send while a turn runs.
//
// Polling: the last 80 turns on open, then `since=<cursor>&last=<id>` every
// 3 s while live and 10 s otherwise; the server re-sends the last turn so a
// reply still streaming grows in place, and says `reset` when it cannot
// continue from the cursor (the phone then takes the fresh tail). "Load
// earlier" pages back with `before`. Cards ride every poll; the one the
// server names live has buttons, and a tap holds them until the poll shows
// the verdict (the bots' local-hold rule).
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  EllipsisIcon,
  GitPullRequestIcon,
  LoaderIcon,
  ShieldAlertIcon,
  SquareIcon,
  SquareCheckIcon,
  StarIcon,
  WifiOffIcon,
} from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@platform/shadcn/ui/alert-dialog";
import { Button } from "@platform/shadcn/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@platform/shadcn/ui/empty";
import { Input } from "@platform/shadcn/ui/input";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { cn } from "@platform/lib/utils";
import { GateCard, type GateStatus } from "@platform/ui/GateCard";
import { IN_APP } from "../env";
import { TONE_COLOR } from "../bots/lib";
import { usePoll, useVisualViewportVar } from "../bots/hooks";
import { Sheet } from "../bots/Sheet";
import { Toaster, toast } from "../bots/Toast";
import { Link, navigate } from "../nav";
import { ApiError, api } from "./api";
import {
  backendLabel,
  cardStatus,
  clock,
  dayBreak,
  dayLabel,
  dueEpoch,
  isActive,
  markdownLite,
  mergeTurns,
  oneLine,
  queueLine,
  statusOf,
  timeAgo,
  titleLine,
  toggleIn,
} from "./lib";
import type { PhoneCard, PhoneTask, PhoneTurn, Scheduled, TaskDetail } from "./types";

const PAGE = 80;
const HOLD_MAX_MS = 20_000;
const END_GAP = 96;
const FINE_POINTER = typeof matchMedia !== "undefined" && matchMedia("(pointer: fine)").matches;

export function TaskView({ taskKey }: { taskKey: string }) {
  useVisualViewportVar();
  const [key, setKey] = useState(taskKey);
  const [task, setTask] = useState<PhoneTask | null>(null);
  const [turns, setTurns] = useState<PhoneTurn[]>([]);
  const [cards, setCards] = useState<PhoneCard[]>([]);
  const [liveCard, setLiveCard] = useState<string | null>(null);
  const [scheduled, setScheduled] = useState<Scheduled[]>([]);
  const [live, setLive] = useState(false);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [held, setHeld] = useState<ReadonlySet<string>>(() => new Set());
  const [menu, setMenu] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [pill, setPill] = useState(false);
  const cursor = useRef<{ since: number; last: string } | null>(null);
  const readPosted = useRef(false);
  const mounted = useRef(true);
  // The entry a freshly created task is followed by, across its rekey (`?entry=`).
  const entryId = useMemo(() => new URLSearchParams(location.search).get("entry") || "", []);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const thread = useRef<HTMLDivElement>(null);
  const atEnd = useRef(true);
  const prepend = useRef<{ height: number; top: number } | null>(null);

  useEffect(() => {
    document.title = task ? titleLine(task) : "Task";
  }, [task]);

  /** A `pending:` key whose run has minted a session: find the row by entry and move there. */
  const follow = useCallback(async () => {
    const wanted = entryId || (key.startsWith("pending:") ? key.slice("pending:".length) : "");
    if (!wanted) return false;
    try {
      const d = await api.list();
      if ("unchanged" in d) return false;
      const row = d.tasks.find((t) => t.entry_id === wanted && t.key !== key);
      if (!row) return false;
      cursor.current = null;
      readPosted.current = false;
      setKey(row.key);
      navigate(`/tasks/${encodeURIComponent(row.key)}`, true);
      return true;
    } catch {
      return false;
    }
  }, [entryId, key]);

  const load = useCallback(async () => {
    try {
      const c = cursor.current;
      const d = await api.detail(key, c ? { since: c.since, last: c.last, limit: PAGE } : { limit: PAGE });
      if (!mounted.current) return;
      apply(d, !c);
      // A pending key answers with its scheduled message until the run mints a session.
      if (key.startsWith("pending:") && d.task.status !== "upcoming" && d.task.status !== "queued") void follow();
      if (!readPosted.current && d.task.unread) {
        readPosted.current = true;
        api.op(key, "read").catch(() => {});
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        if (await follow()) return;
        setGone(true);
      }
      setError((e as Error).message);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, follow]);

  function apply(d: TaskDetail, first: boolean) {
    setTask(d.task);
    setNow(d.ts || Date.now() / 1000);
    setError(null);
    setLive(d.live);
    setCards(d.cards);
    setLiveCard(d.card?.request_id ?? null);
    setScheduled(d.scheduled);
    setTurns((old) => mergeTurns(old, d.turns, first || d.reset));
    if (first || d.reset) setHasEarlier(d.first > 0);
    const lastTurn = d.turns.length ? d.turns[d.turns.length - 1] : null;
    if (lastTurn) cursor.current = { since: d.cursor, last: lastTurn.id };
    else if (!cursor.current || d.reset) cursor.current = { since: d.cursor, last: "" };
    // The poll that brings a card's verdict releases its local hold.
    setHeld((h) => {
      if (!h.size) return h;
      const n = new Set([...h].filter((id) => d.card?.request_id === id));
      return n.size === h.size ? h : n;
    });
  }

  const active = task ? isActive(task) || live : false;
  const kick = usePoll(load, active ? 3000 : 10000, !gone);

  // ---- scroll: follow at the end, hold position otherwise ----
  const onScroll = () => {
    const el = thread.current;
    if (!el) return;
    atEnd.current = el.scrollHeight - el.scrollTop - el.clientHeight <= END_GAP;
    if (atEnd.current) setPill(false);
  };
  const toEnd = (smooth = false) => {
    const el = thread.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    atEnd.current = true;
    setPill(false);
  };
  const lastI = turns.length ? turns[turns.length - 1].i : -1;
  const firstI = turns.length ? turns[0].i : -1;
  const lastText = turns.length ? (turns[turns.length - 1] as { text?: string }).text?.length ?? 0 : 0;
  useLayoutEffect(() => {
    const el = thread.current;
    if (!el) return;
    const p = prepend.current;
    if (p) {
      el.scrollTop = p.top + (el.scrollHeight - p.height);
      prepend.current = null;
      return;
    }
    if (atEnd.current) el.scrollTop = el.scrollHeight;
    else setPill(true);
  }, [lastI, firstI, lastText, cards.length]);
  useEffect(() => {
    const el = thread.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (atEnd.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, [task != null]);

  const loadEarlier = async () => {
    if (!turns.length || loadingEarlier) return;
    setLoadingEarlier(true);
    try {
      const d = await api.detail(key, { before: turns[0].i, limit: PAGE });
      const el = thread.current;
      if (el && d.turns.length) prepend.current = { height: el.scrollHeight, top: el.scrollTop };
      setTurns((old) => mergeTurns(old, d.turns));
      setHasEarlier(d.first > 0);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setLoadingEarlier(false);
    }
  };

  // ---- actions ----
  const act = async (fn: () => Promise<unknown>, done?: string): Promise<boolean> => {
    try {
      await fn();
      if (done) toast(done);
      kick();
      return true;
    } catch (e) {
      toast((e as Error).message, "error");
      return false;
    }
  };
  const send = async (text: string): Promise<boolean> => {
    try {
      const r = await api.send(key, text);
      if (r.queued) toast("Queued. The folder is busy.");
      atEnd.current = true;
      kick();
      return true;
    } catch (e) {
      toast((e as Error).message, "error");
      return false;
    }
  };
  const heldRef = useRef<Set<string>>(new Set());
  const release = (id: string) => {
    heldRef.current.delete(id);
    setHeld((h) => {
      if (!h.has(id)) return h;
      const n = new Set(h);
      n.delete(id);
      return n;
    });
  };
  const hold = (id: string): boolean => {
    if (heldRef.current.has(id)) return false;
    heldRef.current.add(id);
    setHeld((h) => new Set(h).add(id));
    window.setTimeout(() => release(id), HOLD_MAX_MS);
    return true;
  };
  const onDecide = (id: string, decision: "allow" | "deny") => {
    if (!hold(id)) return;
    void act(() => api.decide(key, id, decision)).then((ok) => !ok && release(id));
  };
  const onAnswer = (id: string, answers: Record<string, string[]>, custom?: Record<string, string>) => {
    if (!hold(id)) return;
    void act(() => api.answer(key, id, answers, custom)).then((ok) => !ok && release(id));
  };

  // ---- render ----
  if (gone) {
    return (
      <main className="lan-bots mx-auto max-w-3xl px-4 pt-[max(12px,env(safe-area-inset-top))]">
        <Empty className="mt-16">
          <EmptyHeader>
            <EmptyTitle>This task is gone</EmptyTitle>
            <EmptyDescription>It was deleted on the computer, or the link is wrong.</EmptyDescription>
          </EmptyHeader>
          <Button variant="outline" className="h-11 rounded-xl px-4" onClick={() => navigate("/tasks", true)}>
            All tasks
          </Button>
        </Empty>
      </main>
    );
  }

  const st = task ? statusOf(task) : null;
  const pending = key.startsWith("pending:");
  const placeholder = !task ? "Message" : liveCard ? "Reply, or answer above" : live ? "Add to the task…" : "Follow up";

  return (
    <div className="lan-bots lan-chat mx-auto max-w-3xl bg-[var(--bg)]">
      <header className={cn("flex shrink-0 items-center gap-1 px-2 pb-1", IN_APP ? "pt-1" : "pt-[max(8px,env(safe-area-inset-top))]")}>
        {!IN_APP && (
          <Link href="/tasks" aria-label="All tasks" className="flex size-11 items-center justify-center rounded-full text-[var(--fg)] active:bg-[var(--row-bg-hover)]">
            <ChevronLeftIcon className="size-6" />
          </Link>
        )}
        {task ? (
          <h1 className={cn("min-w-0 flex-1 truncate text-[17px] font-semibold", IN_APP && "sr-only")}>{titleLine(task)}</h1>
        ) : (
          <Skeleton className="h-5 w-32 flex-1" />
        )}
        {IN_APP && <span className="flex-1" />}
        <button type="button" onClick={() => setMenu(true)} aria-label="More" disabled={!task} className="flex size-11 items-center justify-center rounded-full active:bg-[var(--row-bg-hover)] disabled:opacity-40">
          <EllipsisIcon className="size-5" />
        </button>
      </header>

      {task && st && (
        <Strip
          task={task}
          word={st.word}
          color={TONE_COLOR[st.tone]}
          live={live}
          scheduled={scheduled}
          now={now}
          onSkip={() => act(() => api.op(key, "skip"), "Moved to the front of the line")}
          onCancel={() => act(() => api.cancel(key), "Cancelled")}
        />
      )}

      <div className="relative min-h-0 flex-1">
        <div ref={thread} onScroll={onScroll} className="lan-thread absolute inset-0">
          <div>
            {!task && !error && (
              <div className="flex flex-col gap-3 p-4">
                <Skeleton className="ml-auto h-10 w-1/2 rounded-2xl" />
                <Skeleton className="h-12 w-2/3 rounded-2xl" />
                <Skeleton className="h-20 w-3/4 rounded-2xl" />
              </div>
            )}
            {!task && error && (
              <Empty className="mt-16">
                <EmptyHeader>
                  <EmptyMedia variant="icon">
                    <WifiOffIcon />
                  </EmptyMedia>
                  <EmptyTitle>Can't reach the computer</EmptyTitle>
                  <EmptyDescription>{error}</EmptyDescription>
                </EmptyHeader>
              </Empty>
            )}
            {hasEarlier && (
              <div className="flex justify-center pt-3">
                <Button variant="ghost" className="h-10 rounded-full px-4 text-[14px] text-[var(--fg-muted)]" onClick={loadEarlier} disabled={loadingEarlier}>
                  {loadingEarlier ? "Loading…" : "Load earlier"}
                </Button>
              </div>
            )}
            {task && pending && scheduled.length > 0 && (
              <div className="flex flex-col items-center gap-3 px-6 pt-16 text-center">
                <div className="text-[15px] text-[var(--fg-muted)]">
                  {task.status === "queued" ? "Waiting for the folder to free up." : "Scheduled. The conversation starts when it runs."}
                </div>
                <div className="max-w-[92%] rounded-[20px] rounded-br-md bg-[color-mix(in_srgb,var(--accent)_20%,var(--card))] px-3.5 py-2.5 text-left text-[16px] leading-[1.4] whitespace-pre-wrap">
                  {scheduled[0].text}
                </div>
              </div>
            )}
            {task && !pending && turns.length === 0 && cards.length === 0 && (
              <div className="flex flex-col items-center gap-3 px-8 pt-20 text-center">
                <p className="text-[15px] text-[var(--fg-muted)]">{live ? "Starting…" : "Nothing here yet."}</p>
              </div>
            )}
            {task && (turns.length > 0 || cards.length > 0) && (
              <Thread turns={turns} cards={cards} liveCard={liveCard} held={held} now={now} live={live} onDecide={onDecide} onAnswer={onAnswer} />
            )}
          </div>
        </div>
        {pill && (
          <button
            type="button"
            onClick={() => toEnd(true)}
            className="absolute bottom-3 left-1/2 flex h-9 -translate-x-1/2 items-center gap-1.5 rounded-full bg-[var(--fg)] px-3.5 text-[14px] font-medium text-[var(--bg)] shadow-lg duration-150 animate-in fade-in-0 slide-in-from-bottom-1"
          >
            <ArrowDownIcon className="size-4" />
            New
          </button>
        )}
        {error && task && (
          <div className="pointer-events-none absolute inset-x-0 top-2 flex justify-center">
            <span className="rounded-full bg-[var(--bg-alt)] px-3 py-1 text-[12px] text-[var(--fg-muted)] shadow">Reconnecting…</span>
          </div>
        )}
      </div>

      {!pending && <Composer placeholder={placeholder} disabled={!task} live={live} onSend={send} onStop={() => act(() => api.cancel(key), "Stopping")} />}
      {pending && <div className="shrink-0 pb-[env(safe-area-inset-bottom)]" />}

      <Sheet open={menu} onOpenChange={setMenu} title={task ? titleLine(task) : ""}>
        {task && (
          <div className="flex flex-col gap-1 pb-2">
            <MenuRow
              text="Rename"
              onClick={() => {
                setMenu(false);
                setRenaming(true);
              }}
            />
            <MenuRow
              text={task.starred ? "Unstar" : "Star"}
              onClick={() => {
                setMenu(false);
                void act(() => api.favourite(key, !task.starred));
              }}
            />
            <MenuRow
              text={task.status === "archived" ? "Unarchive" : "Archive"}
              onClick={() => {
                setMenu(false);
                void act(() => api.op(key, task.status === "archived" ? "unarchive" : "archive"), task.status === "archived" ? "Unarchived" : "Archived");
              }}
            />
            {task.entry_url && <MenuRow text="Open the app" onClick={() => (location.href = task.entry_url)} />}
            <MenuRow
              text="Delete"
              danger
              onClick={() => {
                setMenu(false);
                setConfirmDelete(true);
              }}
            />
          </div>
        )}
      </Sheet>

      <Sheet
        open={renaming}
        onOpenChange={setRenaming}
        title="Rename"
        footer={
          <Button
            variant="accent"
            className="h-12 w-full rounded-xl text-[16px]"
            onClick={() => {
              const el = document.getElementById("task-rename") as HTMLInputElement | null;
              setRenaming(false);
              void act(() => api.rename(key, (el?.value || "").trim()));
            }}
          >
            Save
          </Button>
        }
      >
        <Input id="task-rename" key={task?.title} defaultValue={task?.title || ""} placeholder="Title (empty: back to the automatic one)" className="h-11 rounded-xl text-[16px]" autoFocus />
      </Sheet>

      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this task?</AlertDialogTitle>
            <AlertDialogDescription>It leaves the list. The conversation stays on the computer, and a reply in it brings the task back.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setConfirmDelete(false);
                void act(() => api.op(key, "delete")).then((ok) => ok && navigate("/tasks", true));
              }}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Toaster bottom={84} />
    </div>
  );
}

function MenuRow({ text, onClick, danger }: { text: string; onClick: () => void; danger?: boolean }) {
  return (
    <button type="button" onClick={onClick} className={cn("lan-press flex min-h-12 w-full items-center rounded-xl px-3 text-left text-[16px]", danger && "text-[var(--error)]")}>
      {text}
    </button>
  );
}

// ---------------------------------------------------------------- strip ----
function Strip({
  task,
  word,
  color,
  live,
  scheduled,
  now,
  onSkip,
  onCancel,
}: {
  task: PhoneTask;
  word: string;
  color: string;
  live: boolean;
  scheduled: Scheduled[];
  now: number;
  onSkip: () => void;
  onCancel: () => void;
}) {
  const agent = [backendLabel(task.backend), task.model].filter(Boolean).join(" · ");
  const queue = queueLine(task);
  const next = scheduled.find((s) => s.state === "pending");
  const due = next ? dueEpoch(next.due) : NaN;
  const tone = task.status === "needs_attention" || task.status === "blocked" ? "text-[var(--warning)]" : task.failed ? "text-[var(--error)]" : "";
  return (
    <div className="flex shrink-0 flex-col gap-1 border-b border-[var(--sh-border)] px-3 py-2">
      <div className="flex items-center gap-2 text-[14px]">
        <span className={cn("size-2 shrink-0 rounded-full", live && "lan-pulse")} style={{ background: color }} aria-hidden />
        <span className={cn("font-medium", tone)}>{word}</span>
        <span className="min-w-0 flex-1 truncate text-[var(--fg-muted)]">
          {task.project_label}
          {agent ? ` · ${agent}` : ""}
          {task.task_id ? ` · ${task.task_id}` : ""}
        </span>
        {task.starred && <StarIcon className="size-3.5 shrink-0 fill-[var(--accent)] text-[var(--accent)]" aria-label="Starred" />}
      </div>
      {(task.prs.length > 0 || queue || next) && (
        <div className="flex flex-wrap items-center gap-2 text-[13px]">
          {task.prs.map((p) => (
            <a
              key={p.url}
              href={p.url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex h-7 items-center gap-1 rounded-full border border-[var(--sh-border)] px-2.5 text-[var(--fg)]"
            >
              <GitPullRequestIcon className="size-3.5" />
              {p.repo.split("/")[1] || p.repo} #{p.number}
            </a>
          ))}
          {queue && (
            <span className="flex items-center gap-2 text-[var(--fg-muted)]">
              {queue}
              <button type="button" onClick={onSkip} className="h-7 rounded-full border border-[var(--sh-border)] px-2.5 text-[var(--fg)]">
                Skip the line
              </button>
            </span>
          )}
          {next && !queue && (
            <span className="flex items-center gap-2 text-[var(--fg-muted)]">
              {Number.isFinite(due) ? `Runs ${due > now ? "in " + timeAgo(now - (due - now), now).replace(" ago", "") : timeAgo(due, now)}` : "Scheduled"}
              {next.repeats ? " · repeats" : ""}
              <button type="button" onClick={onCancel} className="h-7 rounded-full border border-[var(--sh-border)] px-2.5 text-[var(--fg)]">
                Cancel
              </button>
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- thread ----
const BUBBLE = "max-w-[88%] rounded-[20px] px-3.5 py-2.5 text-[16px] leading-[1.4] break-words";
const LEFT = cn(BUBBLE, "self-start rounded-bl-md border border-[var(--sh-border)] bg-[var(--card)]");

function Thread({
  turns,
  cards,
  liveCard,
  held,
  now,
  live,
  onDecide,
  onAnswer,
}: {
  turns: PhoneTurn[];
  cards: PhoneCard[];
  liveCard: string | null;
  held: ReadonlySet<string>;
  now: number;
  live: boolean;
  onDecide: (id: string, decision: "allow" | "deny") => void;
  onAnswer: (id: string, answers: Record<string, string[]>, custom?: Record<string, string>) => void;
}) {
  const out: ReactNode[] = [];
  let prev: PhoneTurn | undefined;
  // Consecutive tool lines fold into one quiet block, the bots' steps rule.
  let i = 0;
  while (i < turns.length) {
    const t = turns[i];
    if (dayBreak(prev, t)) {
      out.push(
        <div key={`d${t.i}`} className="lan-mono py-3 text-center text-[11px] tracking-[0.08em] text-[var(--fg-muted)] uppercase">
          {dayLabel(t.at!, now)}
        </div>,
      );
    }
    if (t.kind === "tool") {
      let j = i;
      while (j < turns.length && turns[j].kind === "tool") j++;
      const run = turns.slice(i, j) as Extract<PhoneTurn, { kind: "tool" }>[];
      out.push(<Tools key={`t${t.i}`} run={run} live={live && j === turns.length} />);
      prev = turns[j - 1];
      i = j;
      continue;
    }
    out.push(<Row key={t.id} t={t} />);
    prev = t;
    i++;
  }
  for (const c of cards) {
    out.push(<Card key={c.request_id} c={c} live={c.request_id === liveCard && !held.has(c.request_id)} held={held.has(c.request_id)} onDecide={onDecide} onAnswer={onAnswer} />);
  }
  return <div className="flex flex-col gap-1.5 px-3 pt-2 pb-3">{out}</div>;
}

function Foot({ at, right }: { at: number | null; right?: boolean }) {
  if (!at) return null;
  return <div className={cn("lan-mono px-1 text-[12px] text-[var(--fg-muted)]", right && "text-right")}>{clock(at)}</div>;
}

function Row({ t }: { t: PhoneTurn }) {
  if (t.kind === "user") {
    return (
      <div className="flex flex-col items-end gap-1 pt-1">
        <div className={cn(BUBBLE, "rounded-br-md bg-[color-mix(in_srgb,var(--accent)_20%,var(--card))] whitespace-pre-wrap text-[var(--fg)]")}>{t.text}</div>
        <Foot at={t.at} right />
      </div>
    );
  }
  if (t.kind === "system") {
    const error = t.tone === "error";
    return (
      <div className={cn("px-1 py-1 text-center text-[13px] leading-snug", error ? "text-[var(--error)]" : "text-[var(--fg-muted)]")}>{oneLine(t.text, 240)}</div>
    );
  }
  if (t.kind === "assistant") {
    return (
      <div className="flex flex-col gap-1 pt-1">
        <div className={LEFT}>
          <div className="lan-md" dangerouslySetInnerHTML={{ __html: markdownLite(t.text) }} />
        </div>
        <Foot at={t.at} />
      </div>
    );
  }
  return null;
}

/** A run of tool calls: one line each when two or fewer, else a folded "N steps" row. */
function Tools({ run, live }: { run: Extract<PhoneTurn, { kind: "tool" }>[]; live: boolean }) {
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState<ReadonlySet<number>>(() => new Set());
  const short = run.length <= 2;
  const last = run[run.length - 1];
  if (!short && !open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className="lan-press -mx-1 flex min-h-11 items-center gap-2 rounded-xl px-2 py-1.5 text-left text-[13px] text-[var(--fg-muted)]">
        <ChevronRightIcon className="size-3.5 shrink-0" />
        <span className="shrink-0 font-medium">{run.length} steps</span>
        <span className="min-w-0 flex-1 truncate">· {last.summary}</span>
        {live && last.status === "running" && <span className="lan-pulse size-1.5 shrink-0 rounded-full bg-[var(--success)]" aria-label="Working" />}
      </button>
    );
  }
  return (
    <div className="flex flex-col gap-1 py-1 pl-1">
      {!short && (
        <button type="button" onClick={() => setOpen(false)} className="flex min-h-9 items-center gap-1.5 text-left text-[13px] text-[var(--fg-muted)]">
          <ChevronDownIcon className="size-3.5" />
          {run.length} steps
        </button>
      )}
      {run.map((t) => (
        <div key={t.id} className="flex flex-col pl-5 text-[13px] leading-snug">
          <button
            type="button"
            onClick={() => t.detail && setShown((s) => toggleIn(new Set([...s].map(String)), String(t.i)) as unknown as ReadonlySet<number>)}
            className="flex min-h-7 items-center gap-2 text-left"
          >
            <span
              className={cn(
                "size-1.5 shrink-0 rounded-full",
                t.status === "error" ? "bg-[var(--error)]" : t.status === "running" ? "lan-pulse bg-[var(--success)]" : "bg-[var(--fg-muted)] opacity-50",
              )}
              aria-label={t.status}
            />
            <span className={cn("min-w-0 flex-1 truncate", t.status === "error" ? "text-[var(--error)]" : "text-[var(--fg)] opacity-80")}>{t.summary}</span>
          </button>
          {t.detail && (shown as ReadonlySet<unknown>).has(String(t.i)) && (
            <pre className="mt-1 max-h-48 overflow-auto rounded-lg bg-[var(--bg-alt)] p-2 text-[12px] leading-snug whitespace-pre-wrap text-[var(--fg-muted)]">{t.detail}</pre>
          )}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- cards ----
function Card({
  c,
  live,
  held,
  onDecide,
  onAnswer,
}: {
  c: PhoneCard;
  live: boolean;
  held: boolean;
  onDecide: (id: string, decision: "allow" | "deny") => void;
  onAnswer: (id: string, answers: Record<string, string[]>, custom?: Record<string, string>) => void;
}) {
  if (c.kind === "question" && c.questions) {
    return <QuestionCard c={c} live={live} held={held} onAnswer={onAnswer} />;
  }
  const settled = cardStatus(c);
  const status: GateStatus | undefined = live ? undefined : held ? { tone: "queued", text: "Sending…" } : settled.text ? settled : { tone: "expired", text: "Answered at the computer" };
  const plan = c.kind === "plan";
  return (
    <div className="flex flex-col gap-1 pt-1">
      <GateCard
        touch
        className="max-w-[94%] self-start"
        head={
          <span className="flex items-center gap-1.5 font-semibold">
            <ShieldAlertIcon className="size-4" />
            {plan ? (live ? "A plan needs your OK" : "Asked to approve a plan") : live ? <><span className="gate-tool">{c.tool}</span> needs your OK</> : "Asked for permission"}
          </span>
        }
        severity={c.risk?.severity}
        riskSummary={c.risk?.summary}
        sub={c.summary && c.summary !== c.detail ? c.summary : undefined}
        body={c.detail || c.summary}
        bodyWhat={c.tool === "Bash" ? "command" : "request"}
        resolved={!live}
        status={status}
        actions={[
          { key: "allow", text: plan ? "Approve" : "Allow once", kind: "primary", onClick: () => onDecide(c.request_id, "allow") },
          { key: "deny", text: "Deny", kind: "default", onClick: () => onDecide(c.request_id, "deny") },
        ]}
      />
      <Foot at={c.created_at || null} />
    </div>
  );
}

/** AskUserQuestion: each question's options (one, or several when multiSelect)
 *  and a free "Other" line, then one Send for all of them. */
function QuestionCard({
  c,
  live,
  held,
  onAnswer,
}: {
  c: PhoneCard;
  live: boolean;
  held: boolean;
  onAnswer: (id: string, answers: Record<string, string[]>, custom?: Record<string, string>) => void;
}) {
  const qs = c.questions || [];
  const [picked, setPicked] = useState<Record<string, ReadonlySet<string>>>({});
  const [other, setOther] = useState<Record<string, string>>({});
  const pickedOf = (q: string) => picked[q] || new Set<string>();
  const choose = (q: string, label: string, multi: boolean) =>
    setPicked((p) => ({ ...p, [q]: multi ? toggleIn(pickedOf(q), label) : new Set([label]) }));
  const complete = qs.every((q) => pickedOf(q.question).size > 0 || (other[q.question] || "").trim());
  const submit = () => {
    const answers: Record<string, string[]> = {};
    const custom: Record<string, string> = {};
    for (const q of qs) {
      const typed = (other[q.question] || "").trim();
      const labels = [...pickedOf(q.question)];
      if (typed) {
        answers[q.question] = [...labels, typed];
        custom[q.question] = typed;
      } else if (labels.length) answers[q.question] = labels;
    }
    onAnswer(c.request_id, answers, Object.keys(custom).length ? custom : undefined);
  };
  const settled = cardStatus(c);
  return (
    <div className="flex flex-col gap-1 pt-1">
      <div className={cn(LEFT, "max-w-[94%]")}>
        {qs.map((q) => {
          const answered = c.answers[q.question];
          return (
            <div key={q.question} className="pb-2 last:pb-0">
              {q.header && <div className="lan-mono pb-0.5 text-[11px] tracking-[0.08em] text-[var(--fg-muted)] uppercase">{q.header}</div>}
              <div className="lan-md" dangerouslySetInnerHTML={{ __html: markdownLite(q.question) }} />
              <div className={cn("mt-2 flex gap-2", q.multiSelect ? "flex-col" : "flex-wrap")}>
                {q.options.map((o) => {
                  const on = live ? pickedOf(q.question).has(o.label) : !!answered && answered.includes(o.label);
                  return (
                    <button
                      key={o.label}
                      type="button"
                      role={q.multiSelect ? "checkbox" : "radio"}
                      aria-checked={on}
                      disabled={!live}
                      onClick={() => choose(q.question, o.label, q.multiSelect)}
                      className={cn(
                        "min-h-11 rounded-2xl border px-3.5 py-2 text-left text-[15px] leading-snug transition-colors",
                        live && !on && "border-[var(--sh-border)] bg-[var(--bg)] active:bg-[var(--row-bg-hover)]",
                        !live && !on && "border-[var(--sh-border)] text-[var(--fg-muted)] opacity-60",
                        on && "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_18%,transparent)] font-medium",
                      )}
                    >
                      <span className="flex items-start gap-2">
                        {q.multiSelect ? (
                          on ? <SquareCheckIcon className="mt-0.5 size-4 shrink-0 text-[var(--accent)]" /> : <SquareIcon className="mt-0.5 size-4 shrink-0 text-[var(--fg-muted)]" />
                        ) : (
                          on && <CheckIcon className="mt-0.5 size-4 shrink-0" />
                        )}
                        <span>
                          {o.label}
                          {o.description && <span className="block text-[13px] font-normal text-[var(--fg-muted)]">{o.description}</span>}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
              {live && (
                <Input
                  value={other[q.question] || ""}
                  onChange={(e) => setOther((o) => ({ ...o, [q.question]: e.target.value }))}
                  placeholder="Other…"
                  className="mt-2 h-10 rounded-xl text-[15px]"
                />
              )}
            </div>
          );
        })}
        {live && (
          <Button variant="accent" className="mt-2 h-11 w-full rounded-xl text-[15px]" onClick={submit} disabled={!complete}>
            Send
          </Button>
        )}
        {!live && (
          <div className={cn("mt-2 text-[13px]", settled.tone === "allow" ? "text-[var(--success)]" : "text-[var(--fg-muted)]")}>
            {held ? "Sending…" : settled.text || "Answered at the computer"}
          </div>
        )}
      </div>
      <Foot at={c.created_at || null} />
    </div>
  );
}

// ---------------------------------------------------------------- composer ----
function Composer({
  placeholder,
  disabled,
  live,
  onSend,
  onStop,
}: {
  placeholder: string;
  disabled?: boolean;
  live: boolean;
  onSend: (text: string) => Promise<boolean>;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const ta = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 148)}px`;
  }, [text]);
  const canSend = !sending && !disabled && text.trim().length > 0;
  const send = async () => {
    if (!canSend) return;
    setSending(true);
    try {
      const draft = text;
      if (await onSend(draft.trim())) setText((t) => (t === draft ? "" : t));
    } finally {
      setSending(false);
    }
  };
  return (
    <div className="shrink-0 border-t border-[var(--sh-border)] bg-[var(--bg)]/95 px-3 pt-2 pb-[max(8px,env(safe-area-inset-bottom))] backdrop-blur [html[data-kb]_&]:pb-2">
      <div className="flex items-end gap-2">
        {live && (
          <button type="button" onClick={onStop} aria-label="Stop" className="flex size-11 shrink-0 items-center justify-center rounded-full text-[var(--error)] active:bg-[var(--row-bg-hover)]">
            <SquareIcon className="size-[18px] fill-current" />
          </button>
        )}
        <textarea
          ref={ta}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (FINE_POINTER && e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          rows={1}
          placeholder={placeholder}
          aria-label="Message"
          disabled={disabled}
          className="min-h-11 flex-1 resize-none rounded-[22px] border border-[var(--sh-border)] bg-[var(--card)] px-4 py-[10px] text-[16px] leading-[1.4] outline-none placeholder:text-[var(--fg-muted)] focus:border-[color-mix(in_srgb,var(--accent)_60%,var(--sh-border))]"
        />
        <button
          type="button"
          onClick={send}
          disabled={!canSend}
          aria-label="Send"
          className={cn("flex size-11 shrink-0 items-center justify-center rounded-full transition-colors", canSend ? "bg-[var(--accent)] text-[var(--on-accent)]" : "bg-[var(--bg-alt)] text-[var(--fg-muted)]")}
        >
          {sending ? <LoaderIcon className="size-5 animate-spin" /> : <ArrowUpIcon className="size-5" />}
        </button>
      </div>
    </div>
  );
}
