// /bots — every bot on the computer, Super Bot pinned first. One row per bot:
// face, name, status (dot + word), the last thing it said, and a dot when it
// said something this phone has not shown yet. Polls every 3 s while visible.
import { useCallback, useEffect, useMemo, useState } from "react";
import { BotIcon, ChevronRightIcon, PlusIcon, StarIcon, TriangleAlertIcon, WifiOffIcon } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@platform/shadcn/ui/empty";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Link, SectionSwitch, navigate } from "../nav";
import { api } from "./api";
import { CreateBotSheet } from "./CreateBotSheet";
import { BotFace } from "./Face";
import { readSeen, usePoll, writeSeen } from "./hooks";
import { TONE_COLOR, listUnread, oneLine, seenKey, statusOf, timeAgo } from "./lib";
import { Toaster } from "./Toast";
import type { BotsList as BotsListData, PhoneBot } from "./types";

const LAST_PREFIX: Partial<Record<string, string>> = { user: "You: ", approval: "Asks: ", question: "Asks: ", error: "Error: " };

export function BotsList() {
  const [data, setData] = useState<BotsListData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    document.title = "Bots";
  }, []);

  const load = useCallback(async () => {
    try {
      const d = await api.list();
      setData(d);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  usePoll(load, 3000);

  const host = location.host;
  // A bot this phone has never listed starts "seen" at its current seq: its
  // history is not news, what it says from now on is.
  const seen = useMemo(() => {
    const out = new Map<string, number | null>();
    for (const b of data?.bots || []) {
      const k = seenKey(host, b.id);
      let v = readSeen(k);
      if (v == null) {
        writeSeen(k, b.seq);
        v = b.seq;
      }
      out.set(b.id, v);
    }
    return out;
  }, [data, host]);

  const now = data?.ts ?? Date.now() / 1000;
  const bots = data?.bots || [];
  const claudeMissing = data?.setup?.claude?.found === false;

  return (
    <main className="lan-bots mx-auto max-w-3xl px-4 pb-[max(24px,env(safe-area-inset-bottom))]">
      <header className="sticky top-0 z-10 -mx-4 bg-[var(--bg)]/92 px-4 pt-[max(12px,env(safe-area-inset-top))] pb-3 backdrop-blur">
        <div className="flex items-center justify-between gap-3">
          <SectionSwitch active="bots" />
          <Button
            variant="outline"
            className="h-10 rounded-xl px-3 text-[15px]"
            onClick={() => setCreating(true)}
            disabled={!data}
          >
            <PlusIcon className="size-4" />
            New bot
          </Button>
        </div>
      </header>

      {claudeMissing && (
        <div className="mt-2 flex items-start gap-3 rounded-2xl border border-[var(--sh-border)] bg-[var(--card)] p-3 text-[14px] leading-snug">
          <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-[var(--warning)]" />
          <div>
            <div className="font-medium">Claude Code is not installed on the computer</div>
            <div className="text-[var(--fg-muted)]">Bots need it to think. Install it from fused-render on the computer.</div>
          </div>
        </div>
      )}

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
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="flex items-center gap-3 py-3">
              <Skeleton className="size-11 rounded-full" />
              <div className="flex flex-1 flex-col gap-2">
                <Skeleton className="h-4 w-1/3" />
                <Skeleton className="h-3.5 w-2/3" />
              </div>
            </div>
          ))}
        </div>
      )}

      {data && bots.length === 0 && (
        <Empty className="mt-16">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <BotIcon />
            </EmptyMedia>
            <EmptyTitle>No bots yet</EmptyTitle>
            <EmptyDescription>A bot browses the web and does tasks for you on the computer.</EmptyDescription>
          </EmptyHeader>
          <Button variant="accent" className="h-11 rounded-xl px-4 text-[15px]" onClick={() => setCreating(true)}>
            <PlusIcon />
            New bot
          </Button>
        </Empty>
      )}

      {bots.length > 0 && (
        <ul className="mt-1 flex list-none flex-col p-0">
          {bots.map((b) => (
            <BotRow key={b.id} b={b} now={now} unread={listUnread(b, seen.get(b.id) ?? null)} />
          ))}
        </ul>
      )}

      {error && data && (
        <p className="pt-4 text-center text-[13px] text-[var(--fg-muted)]">Reconnecting… {error}</p>
      )}

      {data && (
        <CreateBotSheet
          open={creating}
          onOpenChange={setCreating}
          setup={data.setup}
          onCreated={(id) => {
            setCreating(false);
            navigate(`/bots/${encodeURIComponent(id)}`);
          }}
        />
      )}
      <Toaster />
    </main>
  );
}

function BotRow({ b, now, unread }: { b: PhoneBot; now: number; unread: boolean }) {
  const st = statusOf(b);
  const isSuper = b.kind === "super";
  const last = b.last ? (LAST_PREFIX[b.last.role] || "") + oneLine(b.last.text, 160) : b.task ? oneLine(b.task, 160) : "";
  const working = b.status === "running";
  return (
    <li className="border-b border-[var(--sh-border)] last:border-b-0">
      <Link
        href={`/bots/${encodeURIComponent(b.id)}`}
        className="lan-press -mx-2 flex min-h-[72px] items-center gap-3 rounded-xl px-2 py-3"
      >
        <div className="relative">
          <BotFace id={b.id} name={b.name} face={b.face} size={46} />
          {isSuper && (
            <span className="absolute -right-0.5 -bottom-0.5 flex size-[18px] items-center justify-center rounded-full bg-[var(--bg)]">
              <StarIcon className="size-3 fill-[var(--accent)] text-[var(--accent)]" aria-label="Pinned" />
            </span>
          )}
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex items-baseline gap-2">
            <span className="truncate text-[16px] font-semibold">{b.name}</span>
            {isSuper && b.name !== "Super Bot" && (
              <span className="shrink-0 text-[12px] text-[var(--fg-muted)]">Super Bot</span>
            )}
            <span className="lan-mono ml-auto shrink-0 text-[12px] text-[var(--fg-muted)]">
              {timeAgo(b.last?.ts || b.updated, now)}
            </span>
          </div>
          <div className="flex items-center gap-1.5 text-[13px]">
            <span
              className={working ? "size-2 shrink-0 rounded-full lan-pulse" : "size-2 shrink-0 rounded-full"}
              style={{ background: TONE_COLOR[st.tone] }}
              aria-hidden
            />
            <span className={st.tone === "needs" ? "font-medium text-[var(--warning)]" : st.tone === "error" ? "text-[var(--error)]" : "text-[var(--fg-muted)]"}>
              {st.word}
            </span>
          </div>
          {last && (
            <div className={unread ? "line-clamp-1 text-[14px] text-[var(--fg)]" : "line-clamp-1 text-[14px] text-[var(--fg-muted)]"}>
              {last}
            </div>
          )}
        </div>
        {unread ? (
          <span className="size-2.5 shrink-0 rounded-full bg-[var(--accent)]" aria-label="New messages" />
        ) : (
          <ChevronRightIcon className="size-4 shrink-0 text-[var(--fg-muted)] opacity-50" aria-hidden />
        )}
      </Link>
    </li>
  );
}
