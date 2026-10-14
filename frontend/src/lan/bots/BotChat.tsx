// /bots/<id> — one bot's chat. A status strip on top (live browser shot, status,
// task, Pause/Resume · Stop · Clear context · Settings), the thread in the
// middle, the composer at the bottom riding on the keyboard.
//
// Polling: `since=0&limit=80` on open, then `since=<seq>` every 1.5 s while
// the bot runs or waits on you, 4 s otherwise; "Load earlier" pages back with
// `before`. Events only ever append (or prepend on Load earlier), keyed by seq,
// and the scroll position is held: at the bottom you follow along, scrolled up
// you stay put and a "New" pill offers the way down.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownIcon,
  ChevronLeftIcon,
  EraserIcon,
  PauseIcon,
  PlayIcon,
  Settings2Icon,
  SquareIcon,
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
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { cn } from "@platform/lib/utils";
import { IN_APP } from "../env";
import { Link, navigate } from "../nav";
import { ApiError, api } from "./api";
import { Composer, toSendFile } from "./Composer";
import { BotFace } from "./Face";
import { readSeen, usePoll, useVisualViewportVar, writeSeen } from "./hooks";
import { ImageViewer } from "./ImageViewer";
import { TONE_COLOR, isActive, mergeEvents, oneLine, seenKey, statusOf } from "./lib";
import { SettingsSheet } from "./SettingsSheet";
import { Thread } from "./Thread";
import { Toaster, toast } from "./Toast";
import type { BotDetail, BotEvent, Setup } from "./types";

const PAGE = 80;
const BACKFILL_PAGES = 5; // on open, at most this many earlier pages to reach the last line seen
/** Whether "Load earlier" has anything left: the window does not start at the log's first line (seq 1). */
const hasMore = (evs: BotEvent[]) => evs.length > 0 && evs[0].seq > 1;
const HOLD_MAX_MS = 20_000; // a card's local "Sending…" hold at most
const END_GAP = 96; // px from the bottom that still counts as "following along"
const READABLE = new Set(["done", "question", "approval", "error"]);

export function BotChat({ id }: { id: string }) {
  useVisualViewportVar();
  const [bot, setBot] = useState<BotDetail | null>(null);
  const [events, setEvents] = useState<BotEvent[]>([]);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const [setup, setSetup] = useState<Setup | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [replyTo, setReplyTo] = useState<{ seq: number; text: string } | null>(null);
  const [held, setHeld] = useState<ReadonlySet<number>>(() => new Set());
  const [viewer, setViewer] = useState<{ src: string; alt: string } | null>(null);
  const [settings, setSettings] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [pill, setPill] = useState(false);
  const seqRef = useRef<number | null>(null);
  const seen = useRef<{ key: string; at: number | null } | null>(null);
  // The seen cursor as it was when the chat opened (null: nothing unread then); the "New" divider sits
  // above the first readable line after it among the events held.
  const unreadAfter = useRef<number | null>(null);
  const newSeq = useMemo(() => {
    const was = unreadAfter.current;
    return was == null ? null : (events.find((e) => e.seq > was && READABLE.has(e.role))?.seq ?? null);
  }, [events]);

  const thread = useRef<HTMLDivElement>(null);
  const atEnd = useRef(true);
  const prepend = useRef<{ height: number; top: number } | null>(null);

  const host = location.host;
  const skey = seenKey(host, id);
  if (!seen.current || seen.current.key !== skey) seen.current = { key: skey, at: readSeen(skey) };

  useEffect(() => {
    document.title = bot?.name || "Bot";
  }, [bot?.name]);

  // The models / efforts the settings sheet offers come from the list route's setup.
  useEffect(() => {
    api
      .list()
      .then((d) => setSetup(d.setup))
      .catch(() => {});
  }, []);

  const load = useCallback(async () => {
    try {
      const first = seqRef.current == null;
      const d = await api.page(id, first ? { since: 0, limit: PAGE } : { since: seqRef.current! });
      setBot(d.bot);
      setNow(d.ts || Date.now() / 1000);
      setError(null);
      if (first) {
        // The seen cursor is written to the end below, so page back (capped) until the window reaches the
        // last line seen: the unread lines are then loaded and under the "New" divider. A failed earlier
        // page only stops the walk; the tail already here is shown.
        let evs = d.events;
        let more = evs.length >= PAGE && hasMore(evs);
        const was = seen.current?.at ?? null;
        try {
          for (let i = 0; was != null && more && evs[0].seq > was + 1 && i < BACKFILL_PAGES; i++) {
            const e = await api.page(id, { since: 0, before: evs[0].seq, limit: PAGE });
            more = e.events.length >= PAGE;
            evs = mergeEvents(evs, e.events);
            more = more && hasMore(evs);
          }
        } catch {
          // keep `more`: "Load earlier" picks up where the walk stopped
        }
        // The divider sits above the first unread line among the events held, so a backlog past the cap
        // that "Load earlier" brings in later lands under it too.
        const unread = was != null && (evs.some((e) => e.seq > was && READABLE.has(e.role)) || (more && evs[0].seq > was + 1));
        unreadAfter.current = unread ? was : null;
        setEvents(evs);
        setHasEarlier(more);
      } else if (d.events.length) {
        setEvents((old) => mergeEvents(old, d.events));
      }
      seqRef.current = Math.max(seqRef.current ?? 0, d.seq);
      if (document.visibilityState === "visible") writeSeen(skey, d.seq);
      // The poll that brings a card's answer back releases its local hold.
      for (const s of heldRef.current) if (d.bot.waiting_seq !== s) heldRef.current.delete(s);
      setHeld((h) => (h.size ? new Set([...h].filter((s) => d.bot.waiting_seq === s)) : h));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setGone(true);
      setError((e as Error).message);
    }
  }, [id, skey]);

  const active = bot ? isActive(bot.status) : false;
  const kick = usePoll(load, active ? 1500 : 4000, !gone);

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
  const lastSeq = events.length ? events[events.length - 1].seq : 0;
  const firstSeq = events.length ? events[0].seq : 0;
  useLayoutEffect(() => {
    const el = thread.current;
    if (!el) return;
    const p = prepend.current;
    if (p) {
      // Load earlier: the rows above grew; keep the ones you were reading in place.
      el.scrollTop = p.top + (el.scrollHeight - p.height);
      prepend.current = null;
      return;
    }
    if (atEnd.current) el.scrollTop = el.scrollHeight;
    else setPill(true);
  }, [lastSeq, firstSeq]);
  // The thread's box changes (composer grows, keyboard, an image loads): stay at the end if you were there.
  useEffect(() => {
    const el = thread.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (atEnd.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, [bot != null]);

  const loadEarlier = async () => {
    if (!events.length || loadingEarlier) return;
    setLoadingEarlier(true);
    try {
      const d = await api.page(id, { since: 0, before: events[0].seq, limit: PAGE });
      const el = thread.current;
      if (el && d.events.length) prepend.current = { height: el.scrollHeight, top: el.scrollTop };
      setEvents((old) => mergeEvents(old, d.events));
      setHasEarlier(d.events.length >= PAGE && hasMore(d.events));
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setLoadingEarlier(false);
    }
  };

  // ---- sending ----
  const send = useCallback(
    async (text: string, opts: { reply_to?: number; answer?: number; files?: File[] } = {}): Promise<boolean> => {
      try {
        const files = opts.files?.length ? await Promise.all(opts.files.map(toSendFile)) : undefined;
        const r = await api.send(id, {
          text,
          ...(opts.reply_to != null ? { reply_to: opts.reply_to } : {}),
          ...(opts.answer != null ? { answer: opts.answer } : {}),
          ...(files ? { files } : {}),
        });
        if (r.held) toast("Queued: it sends when the browser is handed back on the computer.");
        atEnd.current = true;
        kick();
        return true;
      } catch (e) {
        toast((e as Error).message, "error");
        return false;
      }
    },
    [id, kick],
  );

  // `held` drives the render; heldRef is the same set read synchronously, so a second tap that lands
  // before the re-render is still refused (it would pass the server's waiting check as a second answer).
  const heldRef = useRef<Set<number>>(new Set());
  const release = (seq: number) => {
    heldRef.current.delete(seq);
    setHeld((h) => {
      if (!h.has(seq)) return h;
      const n = new Set(h);
      n.delete(seq);
      return n;
    });
  };
  // The hold stops a double tap while the answer is in flight; the poll that shows the bot moved on clears
  // it. A hold the poll never clears (a question the bot did not take as its answer, a message parked while
  // the Mac holds the browser) would leave the card dead, so every hold also expires on its own.
  /** Take the hold on a card; false when it is already held (a double tap). */
  const hold = (seq: number): boolean => {
    if (heldRef.current.has(seq)) return false;
    heldRef.current.add(seq);
    setHeld((h) => new Set(h).add(seq));
    window.setTimeout(() => release(seq), HOLD_MAX_MS);
    return true;
  };

  const onVerdict = useCallback(
    (seq: number, yes: boolean) => {
      if (!hold(seq)) return;
      void send(yes ? "approve" : "deny", { answer: seq }).then((ok) => !ok && release(seq));
    },
    [send],
  );
  const onAnswer = useCallback(
    (seq: number, text: string) => {
      if (!hold(seq)) return;
      void send(text).then((ok) => !ok && release(seq));
    },
    [send],
  );
  const onReply = useCallback((e: BotEvent) => setReplyTo({ seq: e.seq, text: e.text }), []);
  const onImage = useCallback((src: string, alt: string) => setViewer({ src, alt }), []);

  const control = async (op: "pause" | "resume" | "stop" | "clear") => {
    try {
      await api.control(id, op);
      if (op === "clear") toast("Context cleared");
      kick();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  // ---- render ----
  if (gone) {
    return (
      <main className="lan-bots mx-auto max-w-3xl px-4 pt-[max(12px,env(safe-area-inset-top))]">
        <Empty className="mt-16">
          <EmptyHeader>
            <EmptyTitle>This bot is gone</EmptyTitle>
            <EmptyDescription>It was deleted on the computer, or the link is wrong.</EmptyDescription>
          </EmptyHeader>
          <Button variant="outline" className="h-11 rounded-xl px-4" onClick={() => navigate("/bots", true)}>
            All bots
          </Button>
        </Empty>
      </main>
    );
  }

  const st = bot ? statusOf(bot) : null;
  const placeholder = !bot
    ? "Message"
    : bot.status === "waiting"
      ? "Reply, or answer above"
      : bot.status === "running"
        ? "Add to the task…"
        : `Message ${bot.name}`;

  return (
    <div className="lan-bots lan-chat mx-auto max-w-3xl bg-[var(--bg)]">
      {!IN_APP && (
        <header className="flex shrink-0 items-center gap-2 px-2 pt-[max(8px,env(safe-area-inset-top))] pb-1">
          <Link
            href="/bots"
            aria-label="All bots"
            className="flex size-11 items-center justify-center rounded-full text-[var(--fg)] active:bg-[var(--row-bg-hover)]"
          >
            <ChevronLeftIcon className="size-6" />
          </Link>
          {bot ? (
            <>
              <BotFace id={bot.id} name={bot.name} face={bot.face} size={32} />
              <h1 className="min-w-0 flex-1 truncate text-[17px] font-semibold">{bot.name}</h1>
            </>
          ) : (
            <Skeleton className="h-5 w-32" />
          )}
        </header>
      )}

      {bot && st && (
        <StatusStrip
          bot={bot}
          word={st.word}
          color={TONE_COLOR[st.tone]}
          onShot={(src) => setViewer({ src, alt: "Live view of the bot's browser" })}
          onPause={() => control("pause")}
          onResume={() => control("resume")}
          onStop={() => control("stop")}
          onClear={() => setConfirmClear(true)}
          onSettings={() => setSettings(true)}
        />
      )}

      <div className="relative min-h-0 flex-1">
        <div ref={thread} onScroll={onScroll} className="lan-thread absolute inset-0">
          <div>
            {!bot && !error && (
              <div className="flex flex-col gap-3 p-4">
                <Skeleton className="h-12 w-2/3 rounded-2xl" />
                <Skeleton className="ml-auto h-10 w-1/2 rounded-2xl" />
                <Skeleton className="h-20 w-3/4 rounded-2xl" />
              </div>
            )}
            {!bot && error && (
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
            {bot && events.length === 0 && (
              <div className="flex flex-col items-center gap-3 px-8 pt-20 text-center">
                <BotFace id={bot.id} name={bot.name} face={bot.face} size={64} />
                <p className="text-[15px] text-[var(--fg-muted)]">Say hello — give {bot.name} a task.</p>
              </div>
            )}
            {bot && events.length > 0 && (
              <Thread
                events={events}
                bot={bot}
                held={held}
                newSeq={newSeq}
                now={now}
                onReply={onReply}
                onVerdict={onVerdict}
                onAnswer={onAnswer}
                onImage={onImage}
              />
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
        {error && bot && (
          <div className="pointer-events-none absolute inset-x-0 top-2 flex justify-center">
            <span className="rounded-full bg-[var(--bg-alt)] px-3 py-1 text-[12px] text-[var(--fg-muted)] shadow">Reconnecting…</span>
          </div>
        )}
      </div>

      <Composer
        replyTo={replyTo}
        onClearReply={() => setReplyTo(null)}
        placeholder={placeholder}
        disabled={!bot}
        onSend={async (text, files) => {
          // Images alone are fine: the server adds its "Attached (…)" line.
          const ok = await send(text, {
            reply_to: replyTo?.seq,
            files,
          });
          if (ok) setReplyTo(null);
          return ok;
        }}
      />

      {bot && (
        <SettingsSheet
          open={settings}
          onOpenChange={setSettings}
          bot={bot}
          setup={setup}
          onChanged={kick}
          onDeleted={() => navigate("/bots", true)}
        />
      )}

      <AlertDialog open={confirmClear} onOpenChange={setConfirmClear}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>Clear context?</AlertDialogTitle>
            <AlertDialogDescription>
              {bot?.name || "The bot"} starts its next task fresh. The chat stays; its memory and settings are kept.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setConfirmClear(false);
                void control("clear");
              }}
            >
              Clear
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <ImageViewer src={viewer?.src ?? null} alt={viewer?.alt} onClose={() => setViewer(null)} />
      <Toaster bottom={84} />
    </div>
  );
}

// ---------------------------------------------------------------- status strip ----
function StatusStrip({
  bot,
  word,
  color,
  onShot,
  onPause,
  onResume,
  onStop,
  onClear,
  onSettings,
}: {
  bot: BotDetail;
  word: string;
  color: string;
  onShot: (src: string) => void;
  onPause: () => void;
  onResume: () => void;
  onStop: () => void;
  onClear: () => void;
  onSettings: () => void;
}) {
  const running = bot.status === "running";
  const shot = useShot(bot);
  const canPause = bot.status === "running" || bot.status === "waiting";
  const canStop = bot.status === "running" || bot.status === "waiting" || bot.status === "paused";
  const canClear = bot.status === "idle" || bot.status === "error";
  const sub = bot.control
    ? "You're using its browser on the computer"
    : bot.task
      ? oneLine(bot.task, 120)
      : bot.browser?.title
        ? oneLine(bot.browser.title, 120)
        : "";
  const icon = "flex size-11 shrink-0 items-center justify-center rounded-full active:bg-[var(--row-bg-hover)]";
  return (
    <div className="flex shrink-0 items-center gap-3 border-b border-[var(--sh-border)] px-3 py-2">
      {shot ? (
        <button
          type="button"
          onClick={() => onShot(shot)}
          aria-label="Open the live view"
          className="relative h-12 w-[72px] shrink-0 overflow-hidden rounded-lg border border-[var(--sh-border)] bg-[var(--bg-alt)]"
        >
          <img src={shot} alt="" className="size-full object-cover object-top" />
          {running && (
            <span className="absolute top-1 left-1 flex items-center gap-1 rounded-full bg-black/60 px-1.5 text-[9px] font-semibold tracking-wide text-white uppercase">
              <span className="lan-pulse size-1.5 rounded-full bg-[#ff4d4d]" />
              Live
            </span>
          )}
        </button>
      ) : IN_APP ? (
        <BotFace id={bot.id} name={bot.name} face={bot.face} size={36} />
      ) : null}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5 text-[14px] font-medium">
          <span className={cn("size-2 shrink-0 rounded-full", running && "lan-pulse")} style={{ background: color }} aria-hidden />
          <span className={bot.status === "waiting" ? "text-[var(--warning)]" : bot.status === "error" ? "text-[var(--error)]" : undefined}>
            {word}
          </span>
        </div>
        {sub && <div className="truncate text-[13px] text-[var(--fg-muted)]">{sub}</div>}
      </div>
      <div className="-mr-1 flex shrink-0 items-center">
        {bot.status === "paused" ? (
          <button type="button" className={icon} onClick={onResume} aria-label="Resume">
            <PlayIcon className="size-5" />
          </button>
        ) : canPause ? (
          <button type="button" className={icon} onClick={onPause} aria-label="Pause">
            <PauseIcon className="size-5" />
          </button>
        ) : null}
        {canStop && (
          <button type="button" className={icon} style={{ color: "var(--error)" }} onClick={onStop} aria-label="Stop">
            <SquareIcon className="size-[18px] fill-current" />
          </button>
        )}
        {canClear && (
          <button type="button" className={icon} onClick={onClear} aria-label="Clear context">
            <EraserIcon className="size-5" />
          </button>
        )}
        <button type="button" className={icon} onClick={onSettings} aria-label="Settings">
          <Settings2Icon className="size-5" />
        </button>
      </div>
    </div>
  );
}

/** The live shot's URL, cache-busted by `shot_ts`. While the bot runs, a new
 *  frame is taken at most every 4 s and swapped in only once it has loaded,
 *  so the tile never flashes empty. */
function useShot(bot: BotDetail): string | null {
  const latest = bot.shot ? `${bot.shot}?t=${bot.shot_ts ?? 0}` : null;
  const [shown, setShown] = useState<string | null>(latest);
  const latestRef = useRef(latest);
  latestRef.current = latest;
  const running = bot.status === "running";

  // Each swap takes a generation; a frame is shown only if it is newer than the
  // one on screen. A slow image never overwrites a fresher one, yet a frame
  // that loads after the next swap began still lands (a shot slower than 4 s
  // must not freeze the tile on the first frame).
  const gen = useRef(0);
  const shownGen = useRef(0);
  const swap = useMemo(
    () => (url: string | null) => {
      const g = ++gen.current;
      if (!url) {
        shownGen.current = g;
        return setShown(null);
      }
      const img = new Image();
      img.onload = () => {
        if (g <= shownGen.current) return;
        shownGen.current = g;
        setShown(url);
      };
      img.src = url;
    },
    [],
  );

  // Idle / waiting: follow shot_ts as it changes. Running: every 4 s.
  useEffect(() => {
    if (!running) swap(latest);
  }, [running, latest, swap]);
  useEffect(() => {
    if (!running) return;
    swap(latestRef.current);
    const t = setInterval(() => {
      const base = latestRef.current;
      // A bot whose shot_ts does not move still gets a fresh frame.
      if (base) swap(`${base.split("&r=")[0]}&r=${Math.floor(Date.now() / 4000)}`);
    }, 4000);
    return () => clearInterval(t);
  }, [running, swap]);
  return shown;
}
