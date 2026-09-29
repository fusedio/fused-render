// The top bar's two side panels — Memory and Tasks. There is no shadcn Sheet
// in @platform/shadcn/ui, so this is the shadcn Dialog re-pinned to the right
// edge (tailwind-merge lets these utilities replace the centred defaults).
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ExternalLink, RefreshCw } from "lucide-react";
import { appLandingUrl } from "@platform/lib/appLanding";
import { navigateUrl, encodeFsPathSegments } from "@platform/lib/router";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { getMemory, listBotTasks, putMemory, type Bot, type BotTask } from "./api";
import { taskStatus } from "./lib";

const TASK_POLL_MS = 5000;

function Sheet({
  open,
  onClose,
  title,
  description,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="bots-sheet top-0 right-0 left-auto h-dvh w-[460px] max-w-[94vw] sm:max-w-[460px] translate-x-0 translate-y-0 rounded-none grid-rows-[auto_minmax(0,1fr)] data-open:zoom-in-100 data-closed:zoom-out-100 data-open:slide-in-from-right-10 data-closed:slide-out-to-right-10">
        <DialogHeader>
          <DialogTitle className="font-semibold">{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {children}
      </DialogContent>
    </Dialog>
  );
}

export function MemorySheet({ bot, open, onClose }: { bot: Bot; open: boolean; onClose: () => void }) {
  const [text, setText] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setText(null);
    setError(null);
    getMemory(bot.slug)
      .then(({ content }) => {
        if (cancelled) return;
        setText(content);
        setSaved(content);
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [open, bot.slug]);

  const save = async () => {
    if (text === null) return;
    setBusy(true);
    setError(null);
    try {
      await putMemory(bot.slug, text);
      setSaved(text);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={`${bot.name}'s memory`}
      description="What this bot has saved about you. It reads this at the start of every chat."
    >
      <div className="bots-sheet-body">
        <Textarea
          className="bots-memory-input"
          value={text ?? ""}
          disabled={text === null}
          placeholder={text === null ? "Loading…" : "# Memory"}
          onChange={(e) => setText(e.target.value)}
        />
        {error && <p className="bots-error">{error}</p>}
        <div className="bots-sheet-actions">
          <Button onClick={() => void save()} disabled={busy || text === null || text === saved}>
            {busy ? "Saving…" : text !== null && text === saved ? "Saved" : "Save"}
          </Button>
        </div>
      </div>
    </Sheet>
  );
}

const STATUS_LABEL = {
  starting: "Starting",
  running: "Running",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  unknown: "Unknown",
} as const;

function taskAge(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(t).toLocaleDateString();
}

/** Where "Open app" goes: the app's entry page with the chat pane attached to
 *  the task's live run (platform appLanding), or — once there is no run to
 *  attach to — the app page's Tasks tab, which lists this task's session. */
function openAppUrl(t: BotTask): string {
  if (t.run_id && t.entry_html && taskStatus(t) !== "done") return appLandingUrl(t.entry_html, t.run_id);
  return "/apps/" + encodeFsPathSegments(t.app_path) + "?_tab=tasks";
}

export function TasksSheet({ bot, open, onClose }: { bot: Bot; open: boolean; onClose: () => void }) {
  const [tasks, setTasks] = useState<BotTask[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const { tasks } = await listBotTasks(bot.slug);
      // The server answers oldest first; newest goes on top here.
      setTasks([...tasks].sort((a, b) => (b.created || "").localeCompare(a.created || "")));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [bot.slug]);

  // Poll only while the sheet is open: status is the schedule's live answer,
  // joined server-side, and nobody is looking at it otherwise.
  useEffect(() => {
    if (!open) return;
    setTasks(null);
    void load();
    const id = window.setInterval(() => void load(), TASK_POLL_MS);
    return () => window.clearInterval(id);
  }, [open, load]);

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={`${bot.name}'s tasks`}
      description="Apps this bot asked a builder agent to create or change."
    >
      <div className="bots-sheet-body">
        <div className="bots-sheet-actions bots-sheet-actions--top">
          <Button size="sm" variant="outline" onClick={() => void load()}>
            <RefreshCw /> Refresh
          </Button>
        </div>
        {error && <p className="bots-error">{error}</p>}
        {tasks === null ? (
          <p className="bots-muted">Loading…</p>
        ) : tasks.length === 0 ? (
          <p className="bots-muted">
            No tasks yet. Ask {bot.name} to build or change an app and it will show up here.
          </p>
        ) : (
          <ul className="bots-task-list">
            {tasks.map((t) => {
              const status = taskStatus(t);
              const href = openAppUrl(t);
              return (
                <li key={t.id} className="bots-task">
                  <div className="bots-task-top">
                    <Badge variant="outline">{t.kind === "new" ? "New app" : "Edit"}</Badge>
                    <span className="bots-task-name">{t.app_name}</span>
                    <span className={`bots-status bots-status--${status}`}>{STATUS_LABEL[status]}</span>
                  </div>
                  {t.spec && <p className="bots-task-spec">{t.spec}</p>}
                  {status === "failed" && t.error && <p className="bots-error">{t.error}</p>}
                  <div className="bots-task-foot">
                    <span className="bots-muted">{taskAge(t.created)}</span>
                    <a
                      className="bots-task-open"
                      href={href}
                      onClick={(e) => {
                        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                        e.preventDefault();
                        onClose();
                        navigateUrl(href);
                      }}
                    >
                      Open app <ExternalLink size={12} aria-hidden="true" />
                    </a>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Sheet>
  );
}
