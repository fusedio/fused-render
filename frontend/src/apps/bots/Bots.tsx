// Bots (/bots) — persona chat assistants on the Claude Code harness.
//
// A bot is a folder under ~/Fused-bot/<slug>/ (bot.json, memory/MEMORY.md,
// tasks.json); every chat with it is a Claude session whose cwd is that
// folder, so the conversation itself is the ordinary native chat (`ChatMount`,
// file=<bot folder>) — agent.py recognises the folder and runs it read-only
// with the fused_bot MCP tools (remember / create_app / edit_app …).
//
// URL: `?bot=<slug>` picks the bot; every other param is the chat's own
// (`session_id`, `run`, `model`, `effort`, …, paramsSource="url"), so a chat
// is bookmarkable exactly like one in the explorer's side pane. Picking a bot,
// a session, or New chat is a navigation that deliberately DROPS the old
// chat's params and remounts the chat (`mountSeq`), because it is a different
// conversation; the chat's own writes (its new session id) never remount it.
//
// Layout (Grok-inspired): left rail of bots + the selected bot's chats; main
// column = top bar (avatar, name, persona line, Persona / Memory / Tasks),
// then the chat. Styling lives in styles/bots.css.
import { useCallback, useEffect, useState } from "react";
import { Brain, ListChecks, Pencil } from "lucide-react";
import { ChatMount, canvasChatSrc, useNativeChatFlag } from "@apps/claude";
import { statPath, type Task } from "@platform/lib/api";
import { useNavEpoch } from "@platform/lib/hooks";
import { navigateUrl, replaceSearch } from "@platform/lib/router";
import { Button } from "@platform/shadcn/ui/button";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  createBot,
  deleteBot,
  listBots,
  listBotSessions,
  updateBot,
  type Bot,
  type BotInput,
} from "./api";
import { BotAvatar } from "./BotAvatar";
import { BotDialog } from "./BotDialog";
import { BotHero, FirstBotHero } from "./BotHero";
import { BotRail } from "./BotRail";
import { MemorySheet, TasksSheet } from "./BotSheets";
import { botUrl, personaLine, useSearchParams } from "./lib";

// The chat list follows new sessions without a push channel of its own: a
// re-read when the open session changes (a first message mints one) plus a
// slow poll for titles and times.
const SESSIONS_POLL_MS = 15000;
// A brand-new session is a row only once its transcript is written and the
// server's watcher has seen it — seconds after the id lands on the URL. Two
// extra looks cover that window (same numbers as the chat's own Recent list,
// apps/claude/protocol/sessions.ts RECENT_RETRY_MS).
const SESSION_RETRY_MS = [2500, 6000];
const URL_KEYS = ["bot", "session_id", "run", "queued"] as const;

type DialogState = { mode: "new"; seed: BotInput | null } | { mode: "edit" } | null;

export default function Bots() {
  const epoch = useNavEpoch();
  const params = useSearchParams(URL_KEYS, epoch);
  const native = useNativeChatFlag();

  const [bots, setBots] = useState<Bot[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [sheet, setSheet] = useState<"memory" | "tasks" | null>(null);
  const [sessions, setSessions] = useState<Task[] | null>(null);
  // Remount counter for the chat: bumped by every navigation that means "a
  // different conversation" (bot switch, New chat, a session row, an opener).
  const [mountSeq, setMountSeq] = useState(0);
  // An opener chip's text, handed to exactly one fresh mount as `initialAsk`.
  const [ask, setAsk] = useState<string | undefined>(undefined);
  // The claude template for the legacy (flag-off) frame, per bot folder.
  const [chatTpl, setChatTpl] = useState<{ path: string; tpl: string | null } | null>(null);

  const refreshBots = useCallback(async () => {
    try {
      const { bots } = await listBots();
      setBots(bots);
      setError(null);
      return bots;
    } catch (e) {
      setError((e as Error).message);
      setBots((prev) => prev ?? []);
      return null;
    }
  }, []);

  useEffect(() => {
    void refreshBots();
  }, [refreshBots]);

  const selected = bots?.find((b) => b.slug === params.bot) ?? bots?.[0] ?? null;

  // Missing or stale `?bot=`: settle the URL on the bot actually shown, in
  // place (no history entry) and keeping whatever else is on the query.
  useEffect(() => {
    if (!selected || params.bot === selected.slug) return;
    const q = new URLSearchParams(location.search);
    q.set("bot", selected.slug);
    // …and the bot's run settings as the fresh chat's seeds (see botUrl).
    if (!q.get("session_id")) {
      if (selected.model && !q.get("model")) q.set("model", selected.model);
      if (selected.effort && !q.get("effort")) q.set("effort", selected.effort);
    }
    replaceSearch(location.pathname + "?" + q.toString());
  }, [selected, params.bot]);

  const botPath = selected?.path ?? null;
  useEffect(() => {
    if (!botPath) {
      setSessions(null);
      return;
    }
    let cancelled = false;
    const load = () =>
      listBotSessions(botPath)
        .then((rows) => !cancelled && setSessions(rows))
        .catch(() => !cancelled && setSessions((prev) => prev ?? []));
    void load();
    const id = window.setInterval(load, SESSIONS_POLL_MS);
    const retries = SESSION_RETRY_MS.map((ms) => window.setTimeout(load, ms));
    return () => {
      cancelled = true;
      window.clearInterval(id);
      retries.forEach((t) => window.clearTimeout(t));
    };
  }, [botPath, params.session_id]);

  // A switched bot starts on an empty list, not the previous bot's chats.
  useEffect(() => setSessions(null), [botPath]);

  useEffect(() => {
    if (!botPath) return;
    let cancelled = false;
    statPath(botPath)
      .then((st) => {
        if (cancelled) return;
        setChatTpl({ path: botPath, tpl: st.templates?.find((t) => t.mode === "claude")?.path ?? null });
      })
      .catch(() => !cancelled && setChatTpl({ path: botPath, tpl: null }));
    return () => {
      cancelled = true;
    };
  }, [botPath]);

  // State first, then the push: nothing registers a router leave guard today
  // (platform/lib/router `guarded`), so navigateUrl pushes synchronously and
  // the batched re-render mounts the new chat on the NEW URL. If a guard ever
  // appears, this must move behind the epoch bump instead.
  const freshChat = (bot: Bot, extra: Record<string, string> = {}, opener?: string) => {
    setAsk(opener);
    setMountSeq((n) => n + 1);
    navigateUrl(botUrl(bot, extra));
  };

  const saveBot = async (input: BotInput) => {
    if (dialog?.mode === "edit" && selected) {
      const { bot } = await updateBot(selected.slug, input);
      setBots((prev) => (prev ?? []).map((b) => (b.slug === bot.slug ? bot : b)));
    } else {
      const { bot } = await createBot(input);
      await refreshBots();
      freshChat(bot);
    }
    setDialog(null);
  };

  const removeBot = async () => {
    if (!selected) return;
    await deleteBot(selected.slug);
    setDialog(null);
    setSheet(null);
    const left = await refreshBots();
    const next = left?.[0];
    if (next) freshChat(next);
    else navigateUrl("/bots");
  };

  const dialogEl = (
    <BotDialog
      open={dialog !== null}
      bot={dialog?.mode === "edit" ? selected : null}
      seed={dialog?.mode === "new" ? dialog.seed : null}
      onClose={() => setDialog(null)}
      onSave={saveBot}
      onDelete={removeBot}
    />
  );

  if (bots === null) {
    return <div className="bots-page bots-page--loading" aria-busy="true" />;
  }

  if (!selected) {
    return (
      <div className="bots-page bots-page--empty">
        {error && <ErrorBanner>{error}</ErrorBanner>}
        <FirstBotHero onCreate={(seed) => setDialog({ mode: "new", seed })} />
        {dialogEl}
      </div>
    );
  }

  // `initialAsk` is native-only (the legacy frame never reads it), so an
  // opener only counts as a conversation — and chips only send — natively.
  const nativeChat = native === true;
  const hasConversation = !!(
    params.session_id ||
    params.run ||
    params.queued ||
    (nativeChat && ask)
  );
  const tplReady = chatTpl?.path === selected.path;
  const legacySrc = tplReady && chatTpl?.tpl ? canvasChatSrc(chatTpl.tpl, selected.path) : "";
  // Native needs no template; the legacy frame waits for the stat.
  const canMount = native === true || (native === false && tplReady && !!legacySrc);

  return (
    <div className="bots-page">
      <BotRail
        bots={bots}
        selected={selected}
        sessions={sessions}
        activeSession={params.session_id}
        onSelectBot={(b) => freshChat(b)}
        onNewBot={() => setDialog({ mode: "new", seed: null })}
        onNewChat={() => freshChat(selected)}
        onOpenSession={(id) => freshChat(selected, { session_id: id })}
      />
      <section className="bots-main">
        <header className="bots-topbar">
          <BotAvatar emoji={selected.emoji} color={selected.color} />
          <div className="bots-topbar-text">
            <div className="bots-topbar-name">{selected.name}</div>
            {selected.persona && (
              <div className="bots-topbar-sub" title={selected.persona}>
                {personaLine(selected.persona)}
              </div>
            )}
          </div>
          <div className="bots-topbar-actions">
            <Button size="sm" variant="ghost" onClick={() => setDialog({ mode: "edit" })}>
              <Pencil /> Persona
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSheet("memory")}>
              <Brain /> Memory
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSheet("tasks")}>
              <ListChecks /> Tasks
            </Button>
          </div>
        </header>
        {error && <ErrorBanner>{error}</ErrorBanner>}
        {!hasConversation && (
          <BotHero
            bot={selected}
            {...(nativeChat ? { onOpener: (text: string) => freshChat(selected, {}, text) } : {})}
          />
        )}
        <div className="bots-chat">
          {canMount ? (
            <ChatMount
              key={`${selected.slug}:${mountSeq}`}
              file={selected.path}
              chatOnly
              paramsSource="url"
              legacySrc={legacySrc}
              title={`Chat with ${selected.name}`}
              model={selected.model}
              effort={selected.effort}
              recap
              {...(nativeChat && ask ? { initialAsk: ask } : {})}
            />
          ) : native === false && tplReady ? (
            <p className="bots-muted bots-chat-note">No Claude chat template is available for this bot.</p>
          ) : null}
        </div>
      </section>
      {dialogEl}
      <MemorySheet bot={selected} open={sheet === "memory"} onClose={() => setSheet(null)} />
      <TasksSheet bot={selected} open={sheet === "tasks"} onClose={() => setSheet(null)} />
    </div>
  );
}
