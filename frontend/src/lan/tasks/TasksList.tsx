// /tasks — the computer's agent tasks, phone-first. A search field, a chip
// strip (Needs you · Running · Upcoming · Done · Archived · Folder), then the
// rows in fixed sections (Needs you, Running, Queued, Upcoming, Done,
// Archived). Polls every 5 s while visible with the last answer's token; an
// `unchanged` answer leaves the DOM alone. A long press on a row opens its
// actions (Rename, Star, Archive, Delete); "+" opens the New sheet.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  CheckIcon,
  ChevronRightIcon,
  FolderIcon,
  GitPullRequestIcon,
  ListChecksIcon,
  PlusIcon,
  SearchIcon,
  StarIcon,
  WifiOffIcon,
  XIcon,
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
import { TONE_COLOR } from "../bots/lib";
import { usePoll } from "../bots/hooks";
import { Sheet } from "../bots/Sheet";
import { Toaster, toast } from "../bots/Toast";
import { Link, SectionSwitch, navigate } from "../nav";
import { api } from "./api";
import {
  CHIPS,
  LANE_LABEL,
  caption,
  filterKey,
  group,
  laneCaption,
  parseFilter,
  persistedFilter,
  projectOptions,
  statusOf,
  titleLine,
  type Chip,
  type Filter,
} from "./lib";
import { NewSheet, type NewTab } from "./NewSheet";
import type { PhoneTask, TasksListData } from "./types";

const LONG_PRESS_MS = 450;

function readFilter(key: string): Filter {
  try {
    return parseFilter(localStorage.getItem(key));
  } catch {
    return { chip: null, folders: [] };
  }
}
function writeFilter(key: string, f: Filter) {
  try {
    localStorage.setItem(key, JSON.stringify(persistedFilter(f)));
  } catch {
    /* private mode */
  }
}

export function TasksList() {
  const host = location.host;
  const fkey = filterKey(host);
  const [data, setData] = useState<TasksListData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>(() => readFilter(fkey));
  const [folderSheet, setFolderSheet] = useState(false);
  const [creating, setCreating] = useState<NewTab | null>(null);
  const [acting, setActing] = useState<PhoneTask | null>(null);
  const [renaming, setRenaming] = useState<PhoneTask | null>(null);
  const [deleting, setDeleting] = useState<PhoneTask | null>(null);
  const genRef = useRef<string>("");
  const archived = filter.chip === "archived";

  useEffect(() => {
    document.title = "Tasks";
  }, []);

  const load = useCallback(async () => {
    try {
      const d = await api.list({ gen: genRef.current, archived });
      setError(null);
      if ("unchanged" in d) return;
      genRef.current = d.generation;
      setData(d);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [archived]);
  const kick = usePoll(load, 5000);
  // A view switch (archived on or off) is a different answer: ask at once, with no token.
  useEffect(() => {
    genRef.current = "";
    kick();
  }, [archived, kick]);

  const update = (f: Filter) => {
    setFilter(f);
    writeFilter(fkey, f);
  };
  const pickChip = (c: Chip) => update({ ...filter, chip: filter.chip === c ? null : c });
  const toggleFolder = (path: string) =>
    update({ ...filter, folders: filter.folders.includes(path) ? filter.folders.filter((p) => p !== path) : [...filter.folders, path] });

  const now = data?.ts ?? Date.now() / 1000;
  const sections = useMemo(() => (data ? group(data.tasks, filter, query) : []), [data, filter, query]);
  const shown = sections.reduce((n, s) => n + s.rows.length, 0);
  const filtered = !!(query || filter.chip || filter.folders.length);
  const projects = data ? projectOptions(data.projects) : [];

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    try {
      await fn();
      if (done) toast(done);
      kick();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  return (
    <main className="lan-bots mx-auto max-w-3xl px-4 pb-[max(24px,env(safe-area-inset-bottom))]">
      <header className="sticky top-0 z-10 -mx-4 bg-[var(--bg)]/92 px-4 pt-[max(12px,env(safe-area-inset-top))] pb-2 backdrop-blur">
        <div className="flex items-center justify-between gap-3 pb-3">
          <SectionSwitch active="tasks" badge={data?.attention} />
          <span className="lan-mono shrink-0 text-xs text-[var(--fg-muted)]">{data ? `${shown}/${data.total}` : ""}</span>
          <Button variant="outline" size="icon" className="size-10 shrink-0 rounded-xl" aria-label="New task" onClick={() => setCreating("task")} disabled={!data}>
            <PlusIcon className="size-5" />
          </Button>
        </div>
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-[var(--fg-muted)]" />
          <Input
            type="search"
            inputMode="search"
            enterKeyHint="search"
            autoCapitalize="off"
            autoCorrect="off"
            placeholder="Search tasks"
            aria-label="Search tasks"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="h-11 rounded-xl pl-9 pr-9 text-base"
          />
          {query && (
            <Button variant="ghost" size="icon-sm" aria-label="Clear search" onClick={() => setQuery("")} className="absolute top-1/2 right-1.5 -translate-y-1/2">
              <XIcon />
            </Button>
          )}
        </div>
        <div className="lan-strip -mx-4 mt-2 flex gap-2 overflow-x-auto px-4 py-1">
          {CHIPS.map((c) => (
            <ChipButton key={c.key} on={filter.chip === c.key} onClick={() => pickChip(c.key)}>
              {c.label}
              {c.key === "needs" && data?.attention ? <span className="ml-1 text-[var(--warning)]">{data.attention}</span> : null}
            </ChipButton>
          ))}
          <ChipButton on={filter.folders.length > 0} onClick={() => setFolderSheet(true)} disabled={!projects.length}>
            <FolderIcon className="size-3.5" />
            {filter.folders.length ? `${filter.folders.length} folder${filter.folders.length > 1 ? "s" : ""}` : "Folder"}
          </ChipButton>
        </div>
      </header>

      {error && !data && (
        <Empty className="mt-16">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <WifiOffIcon />
            </EmptyMedia>
            <EmptyTitle>Can't reach the computer</EmptyTitle>
            <EmptyDescription>{error}. Make sure the phone is on the same Wi-Fi and the computer is awake.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}

      {!error && !data && (
        <div className="flex flex-col gap-1 pt-3">
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="flex items-center gap-3 py-3">
              <Skeleton className="size-2.5 rounded-full" />
              <div className="flex flex-1 flex-col gap-2">
                <Skeleton className="h-4 w-2/3" />
                <Skeleton className="h-3.5 w-1/3" />
              </div>
            </div>
          ))}
        </div>
      )}

      {data && data.tasks.length === 0 && !filtered && (
        <Empty className="mt-16">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <ListChecksIcon />
            </EmptyMedia>
            <EmptyTitle>No tasks yet</EmptyTitle>
            <EmptyDescription>A task is a conversation with an agent about a folder on the computer. Start one, or build a new app.</EmptyDescription>
          </EmptyHeader>
          <Button variant="accent" className="h-11 rounded-xl px-4 text-[15px]" onClick={() => setCreating("task")}>
            <PlusIcon />
            New task
          </Button>
        </Empty>
      )}

      {data && shown === 0 && (data.tasks.length > 0 || filtered) && (
        <Empty className="mt-16">
          <EmptyHeader>
            <EmptyTitle>Nothing matches</EmptyTitle>
            <EmptyDescription>{archived ? "Nothing has been archived." : "No task matches the search and filters."}</EmptyDescription>
          </EmptyHeader>
          {filtered && (
            <Button
              variant="outline"
              className="h-11 rounded-xl px-4"
              onClick={() => {
                setQuery("");
                update({ chip: null, folders: [] });
              }}
            >
              Show everything
            </Button>
          )}
        </Empty>
      )}

      {sections.map((s) => (
        <section key={s.lane} className="pt-3">
          <h2 className="lan-mono flex items-baseline justify-between px-1 pb-1 text-[11px] font-medium tracking-[0.08em] text-[var(--fg-muted)] uppercase">
            <span>{LANE_LABEL[s.lane]}</span>
            <span className="normal-case tracking-normal">{laneCaption(s.lane, s.rows)}</span>
          </h2>
          <ul className="flex list-none flex-col p-0">
            {s.rows.map((t) => (
              <TaskRow key={t.key} t={t} now={now} onLongPress={() => setActing(t)} />
            ))}
          </ul>
        </section>
      ))}

      {error && data && <p className="pt-4 text-center text-[13px] text-[var(--fg-muted)]">Reconnecting… {error}</p>}

      <Sheet open={folderSheet} onOpenChange={setFolderSheet} title="Folders">
        <ul className="flex list-none flex-col p-0">
          {projects.map((p) => {
            const on = filter.folders.includes(p.path);
            return (
              <li key={p.path}>
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={on}
                  onClick={() => toggleFolder(p.path)}
                  className="lan-press flex min-h-12 w-full items-center gap-3 rounded-xl px-2 text-left"
                >
                  <span className={cn("flex size-5 items-center justify-center rounded-md border", on ? "border-[var(--accent)] bg-[var(--accent)] text-[var(--on-accent)]" : "border-[var(--sh-border)]")}>
                    {on && <CheckIcon className="size-3.5" />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[16px]">{p.label}</span>
                    <span className="block truncate text-[12px] text-[var(--fg-muted)]">{p.path}</span>
                  </span>
                  <span className="lan-mono text-[12px] text-[var(--fg-muted)]">{p.count}</span>
                </button>
              </li>
            );
          })}
        </ul>
        {filter.folders.length > 0 && (
          <Button variant="ghost" className="mt-2 h-11 w-full rounded-xl" onClick={() => update({ ...filter, folders: [] })}>
            Every folder
          </Button>
        )}
      </Sheet>

      <Sheet open={!!acting} onOpenChange={(o) => !o && setActing(null)} title={acting ? titleLine(acting) : ""}>
        {acting && (
          <div className="flex flex-col gap-1 pb-2">
            <ActionRow
              text="Rename"
              onClick={() => {
                setRenaming(acting);
                setActing(null);
              }}
            />
            <ActionRow
              text={acting.starred ? "Unstar" : "Star"}
              onClick={() => {
                const t = acting;
                setActing(null);
                void act(() => api.favourite(t.key, !t.starred));
              }}
            />
            <ActionRow
              text={acting.status === "archived" ? "Unarchive" : "Archive"}
              onClick={() => {
                const t = acting;
                setActing(null);
                void act(() => api.op(t.key, t.status === "archived" ? "unarchive" : "archive"), t.status === "archived" ? "Unarchived" : "Archived");
              }}
            />
            {acting.entry_url && <ActionRow text="Open the app" onClick={() => (location.href = acting.entry_url)} />}
            <ActionRow
              text="Delete"
              danger
              onClick={() => {
                setDeleting(acting);
                setActing(null);
              }}
            />
          </div>
        )}
      </Sheet>

      <RenameSheet
        task={renaming}
        onClose={() => setRenaming(null)}
        onRename={(t, title) => {
          setRenaming(null);
          void act(() => api.rename(t.key, title));
        }}
      />

      <AlertDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this task?</AlertDialogTitle>
            <AlertDialogDescription>
              It leaves the list. The conversation stays on the computer, and a reply in it brings the task back.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const t = deleting;
                setDeleting(null);
                if (t) void act(() => api.op(t.key, "delete"), "Deleted");
              }}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <NewSheet
        open={creating != null}
        onOpenChange={(o) => !o && setCreating(null)}
        tab={creating || "task"}
        projects={data?.projects ?? null}
        onCreated={(key, entryId) => {
          setCreating(null);
          navigate(`/tasks/${encodeURIComponent(key)}${entryId ? `?entry=${encodeURIComponent(entryId)}` : ""}`);
        }}
      />
      <Toaster />
    </main>
  );
}

function ChipButton({ on, onClick, disabled, children }: { on: boolean; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex h-9 shrink-0 items-center gap-1 rounded-full border px-3.5 text-[14px] font-medium whitespace-nowrap transition-colors disabled:opacity-40",
        on ? "border-[var(--fg)] bg-[var(--fg)] text-[var(--bg)]" : "border-[var(--sh-border)] bg-[var(--card)] text-[var(--fg)]",
      )}
    >
      {children}
    </button>
  );
}

function ActionRow({ text, onClick, danger }: { text: string; onClick: () => void; danger?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn("lan-press flex min-h-12 w-full items-center rounded-xl px-3 text-left text-[16px]", danger && "text-[var(--error)]")}
    >
      {text}
    </button>
  );
}

function RenameSheet({ task, onClose, onRename }: { task: PhoneTask | null; onClose: () => void; onRename: (t: PhoneTask, title: string) => void }) {
  const [title, setTitle] = useState("");
  useEffect(() => {
    if (task) setTitle(task.title);
  }, [task]);
  return (
    <Sheet
      open={!!task}
      onOpenChange={(o) => !o && onClose()}
      title="Rename"
      footer={
        <Button variant="accent" className="h-12 w-full rounded-xl text-[16px]" onClick={() => task && onRename(task, title.trim())}>
          Save
        </Button>
      }
    >
      <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title (empty: back to the automatic one)" className="h-11 rounded-xl text-[16px]" autoFocus />
    </Sheet>
  );
}

/** One row. A tap opens the task; a press held LONG_PRESS_MS opens its actions. */
function TaskRow({ t, now, onLongPress }: { t: PhoneTask; now: number; onLongPress: () => void }) {
  const st = statusOf(t);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fired = useRef(false);
  const start = () => {
    fired.current = false;
    timer.current = setTimeout(() => {
      fired.current = true;
      onLongPress();
    }, LONG_PRESS_MS);
  };
  const stop = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  const working = t.status === "in_progress";
  const needs = st.tone === "needs";
  return (
    <li className="border-b border-[var(--sh-border)] last:border-b-0">
      <Link
        href={`/tasks/${encodeURIComponent(t.key)}`}
        onPointerDown={start}
        onPointerUp={stop}
        onPointerCancel={stop}
        onPointerLeave={stop}
        onContextMenu={(e) => {
          e.preventDefault();
          onLongPress();
        }}
        onClick={(e) => {
          if (fired.current) e.preventDefault();
        }}
        className="lan-press -mx-2 flex min-h-[64px] items-center gap-3 rounded-xl px-2 py-2.5 select-none [-webkit-touch-callout:none]"
      >
        <span className={cn("mt-0.5 size-2.5 shrink-0 self-start rounded-full", working && "lan-pulse")} style={{ background: TONE_COLOR[st.tone] }} aria-label={st.word} />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex items-baseline gap-2">
            <span className={cn("line-clamp-2 text-[16px] leading-snug", t.unread ? "font-semibold" : "font-medium")}>{titleLine(t)}</span>
          </div>
          <div className="flex items-center gap-1.5 text-[13px] text-[var(--fg-muted)]">
            {t.starred && <StarIcon className="size-3.5 shrink-0 fill-[var(--accent)] text-[var(--accent)]" aria-label="Starred" />}
            <span className="truncate">{caption(t, now)}</span>
            {t.prs.length > 0 && (
              <span className="flex shrink-0 items-center gap-0.5 rounded-full border border-[var(--sh-border)] px-1.5 text-[11px]">
                <GitPullRequestIcon className="size-3" />#{t.prs[0].number}
              </span>
            )}
          </div>
          {needs && t.attention?.summary && <div className="line-clamp-1 text-[13px] font-medium text-[var(--warning)]">{t.attention.summary}</div>}
          {needs && !t.attention?.summary && <div className="text-[13px] font-medium text-[var(--warning)]">{st.word}</div>}
        </div>
        {t.unread ? (
          <span className="size-2.5 shrink-0 rounded-full bg-[var(--accent)]" aria-label={`${t.unread} unread`} />
        ) : (
          <ChevronRightIcon className="size-4 shrink-0 text-[var(--fg-muted)] opacity-50" aria-hidden />
        )}
      </Link>
    </li>
  );
}
