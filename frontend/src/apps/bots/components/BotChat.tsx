// THE BOT'S CHAT, ON ITS OWN (shell/claude-rail BotPeek; owner, 2026-11-05:
// "a small overlay that just displays the chat exchange of the bot, the same
// as the task floating box — no browser, just the chat UI"): the page's
// middle column with its header, search and preview toggle left off — the
// thread, its to-bottom pill, the composer and the reaction picker — for ONE
// bot named by id, mounted anywhere in the shell. The page's own ChatPane
// shows the SELECTED bot (state/store `cur()`); this one takes the bot it is
// given, and the store's selection is left alone — off the Bots page nothing
// is selected, and `select` would write the page's URL.
//
// FED WITHOUT THE PAGE: the store's rows and events come from the page's poll
// loop (startStore), which runs only while the page is mounted. This mount
// holds the store's quiet feed instead (`feedBotQuietly`): the same poll, the
// page's cadence, the merge alone — no landing, no toast, no Stage, no
// notification (store pollOnce's `page` guard) — and it stands aside while
// the page's own loop runs. A send from the composer polls through `act` the
// same way.
//
// THE PAGE'S STYLESHEET APPLIES: every rule in styles/bots.css is scoped to
// `.bots-page`, so the root here wears that class (and `.bots-peek`, for the
// few frame rules at the sheet's end); the reaction picker portals to the
// page's `.bots-page` host under <body> (lib/root `portalHost`), as on the page.
import { useCallback, useEffect, useRef, useState } from "react";
import "../styles/bots.css";
import { feedBotQuietly, useBotsSelector } from "../state/store";
import { Composer, type ReplyTo } from "./Composer";
import { ReactionPicker, type ReactionReq } from "./ReactionPicker";
import { Thread } from "./Thread";
import { ToBottom } from "./ToBottom";

export interface BotChatProps {
  /** The bot whose chat this is (`Bot.id`). */
  id: string;
  /** Sitting at the end marks the bot read (Thread `markRead`): the card passes its pin — a hover never marks. */
  markRead?: boolean;
}

const noop = () => {};

export default function BotChat({ id, markRead = true }: BotChatProps) {
  // The page-wide switches (store `hydrateBotsPrefs`: sounds, the clear-
  // context button, the one-shot seen import) are the PAGE's to hydrate,
  // not this card's: none of them is read here, and the hydrate joins the
  // shell's shared prefs read, which a mount in a test with a stubbed fetch
  // would pin for the whole process (platform/lib/api `dropSharedPrefsRead`).
  useEffect(() => feedBotQuietly(), []);
  const b = useBotsSelector((s) => s.bots.find((x) => x.id === id));
  const polled = useBotsSelector((s) => s.bots.length > 0);
  const threadRef = useRef<HTMLDivElement>(null);
  const [reply, setReply] = useState<ReplyTo | null>(null);
  const [pick, setPick] = useState<ReactionReq | null>(null);
  const closePick = useCallback(() => setPick(null), []);
  // A reply quote and an open picker belong to one bot: a swap drops them.
  useEffect(() => {
    setReply(null);
    setPick(null);
  }, [id]);
  if (!b) {
    return (
      <div className="bots-page bots-peek">
        <div className="empty">{polled ? "This bot is gone." : "Loading…"}</div>
      </div>
    );
  }
  return (
    <div className="bots-page bots-peek">
      <section className="chat">
        <div className="tw">
          <Thread b={b} threadRef={threadRef} searchQ="" onSearchCount={noop} onReact={(anchor, seq) => setPick({ anchor, seq })} onReply={setReply} markRead={markRead} />
          <ToBottom threadRef={threadRef} botId={b.id} />
        </div>
        <Composer b={b} reply={reply} setReply={setReply} threadRef={threadRef} />
        <ReactionPicker req={pick} onClose={closePick} botId={b.id} />
      </section>
    </div>
  );
}
