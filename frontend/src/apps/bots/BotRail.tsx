// The Bots page's left column: every bot, then the selected bot's chats.
import { MessageSquarePlus, Plus } from "lucide-react";
import type { Task } from "@platform/lib/api";
import { timeAgo } from "@platform/lib/format";
import { Button } from "@platform/shadcn/ui/button";
import type { Bot } from "./api";
import { BotAvatar } from "./BotAvatar";
import { personaLine } from "./lib";

function sessionExcerpt(t: Task): string {
  return (t.last_message?.text || t.description || "").replace(/\s+/g, " ").trim();
}

export function BotRail({
  bots,
  selected,
  sessions,
  activeSession,
  onSelectBot,
  onNewBot,
  onNewChat,
  onOpenSession,
}: {
  bots: Bot[];
  selected: Bot | null;
  /** null = not read yet (skeleton), [] = no chats. */
  sessions: Task[] | null;
  activeSession: string;
  onSelectBot: (bot: Bot) => void;
  onNewBot: () => void;
  onNewChat: () => void;
  onOpenSession: (sessionId: string) => void;
}) {
  return (
    <aside className="bots-rail" aria-label="Bots">
      <div className="bots-rail-head">
        <h1 className="bots-rail-title">Bots</h1>
        <Button size="sm" variant="outline" onClick={onNewBot}>
          <Plus /> New bot
        </Button>
      </div>
      <ul className="bots-list">
        {bots.map((b) => (
          <li key={b.slug}>
            <button
              type="button"
              className={`bots-row${selected?.slug === b.slug ? " is-active" : ""}`}
              onClick={() => onSelectBot(b)}
              aria-current={selected?.slug === b.slug ? "true" : undefined}
            >
              <BotAvatar emoji={b.emoji} color={b.color} />
              <span className="bots-row-text">
                <span className="bots-row-name">{b.name}</span>
                <span className="bots-row-sub">{personaLine(b.persona) || "No persona yet"}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
      {selected && (
        <div className="bots-chats">
          <div className="bots-chats-head">
            <span className="bots-section-label">Chats</span>
          </div>
          <button type="button" className="bots-newchat" onClick={onNewChat}>
            <MessageSquarePlus size={14} aria-hidden="true" /> New chat
          </button>
          {sessions === null ? (
            <div className="bots-chats-empty">Loading…</div>
          ) : sessions.length === 0 ? (
            <div className="bots-chats-empty">No chats yet</div>
          ) : (
            <ul className="bots-list">
              {sessions.map((t) => (
                <li key={t.session_id}>
                  <button
                    type="button"
                    className={`bots-chat-row${activeSession === t.session_id ? " is-active" : ""}`}
                    onClick={() => onOpenSession(t.session_id)}
                    title={t.title}
                  >
                    <span className="bots-chat-top">
                      <span className="bots-chat-title">{t.title || "Untitled chat"}</span>
                      <span className="bots-chat-time">{timeAgo(t.last_active) ?? ""}</span>
                    </span>
                    {sessionExcerpt(t) && sessionExcerpt(t) !== t.title && (
                      <span className="bots-chat-excerpt">{sessionExcerpt(t)}</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </aside>
  );
}
