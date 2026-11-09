// THE BOTS CARD OFF THE RAIL'S BOTS DOOR (owner, 2026-11-09: "the bot in the
// top right corner of the rail — we don't have a popover window A for it;
// let's have one which first shows bots, and on click shows its chats; make
// sure window A behaviour is like Tasks' behaviour, and it looks good big or
// small"): resting on the rail's Bots icon previews this card beside the
// rail; a press pins it; a press on the icon while pinned closes it, and so
// does the card's ×. Behind the rail flag, like the nav doors themselves
// (ClaudeRail `navDoors`), and only where the server offers Bots
// (`useBotsAvailable` — the door stands only there too).
//
// WHAT IT IS: the Bots page's list (apps/bots BotPickList — the page's left
// column: search, rows in the page's order, the hidden group) and, on a
// row's press, THAT BOT'S CHAT IN THE CARD (apps/bots BotChat — the thread
// and composer window B's bot card shows, BotPeek). Back returns to the list
// exactly where it was — the list stays mounted under the chat, hidden, its
// search and scroll kept. The store's selection is never touched: off the
// Bots page nothing is selected, and `select` would write the page's URL.
//
// MOUNTED ONCE, IN THE SHELL (App.tsx, beside TasksPeek), for the Tasks
// card's reason: the rail is a page's and comes and goes with it. The chassis
// is window A's, shared with the Tasks card (DoorCard): its size
// (`canvasRailPeek`), grips, timings, show and fade, bridge and cone, the
// keyboard's seats, clear of window B. ClaudeRail holds the rules between the
// cards (one window A card at a time; after a click only a click changes
// window A; pane open / pane chat change / a press into the frame close it).
// THE OPEN CHAT DOES NOT SURVIVE A CLOSE (the Tasks card's rule, owner,
// 2026-11-08: "a window change resets the selection"): the next opening is
// the list.
//
// THE READ MARK IS THE PIN'S (BotPeek's rule): the chat marks the bot read
// only while the card is pinned — a hover's glance leaves the rail's unread
// dot alone. The thread keeps its scroll bookkeeping to itself (`sharedScroll`
// false): the page's thread, or window B's bot card, may show the same bot.
//
// THE HEAD: with the list, the × in the top-left corner, "Bots", and Open
// bots — the page — on the right; with a chat, Back in the ×'s corner,
// prev/next through the list behind (the Tasks card's walk), the bot's face
// and name, its status in the rail's words (claude-rail-lib
// `botStatusWords`), and Open — the bot's page.
import { Component, lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, ChevronDown, ChevronUp, X } from "lucide-react";
import { useBotsAvailable } from "@platform/lib/bots-available";
import { Face } from "@apps/bots/components/Face";
import { botHref } from "@apps/bots/routes";
import { useBotsSelector } from "@apps/bots/state/store";
import { botStatusWords } from "./claude-rail-lib";
import { DoorCard } from "./DoorCard";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { botsCardStore, useBotsCard } from "./bots-peek-store";

// The page's chunks (the Bots page lazy-loads the same modules): not the
// shell's to carry until the door is first hovered.
const BotPickList = lazy(() => import("@apps/bots/components/BotPickList"));
const BotChat = lazy(() => import("@apps/bots/components/BotChat"));

/** A chunk that fails to load (an app updated under this window) fails HERE,
 *  in the card, not at the shell's boundary — which would replace the page. */
class ListBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: unknown) {
    console.error("the Bots card failed to render", error);
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return <p className="claude-rail-peek-note">The bots didn&rsquo;t load. Reload the page to try again.</p>;
  }
}

/** The list's rows in their PAINTED order (BotList `Row` carries `data-id`),
 *  for the chat head's prev/next. */
function rowOrder(list: HTMLElement | null): string[] {
  return Array.from(list?.querySelectorAll<HTMLElement>(".bot[data-id]") ?? []).map((el) => el.dataset.id ?? "").filter(Boolean);
}

export function BotsPeek() {
  const on = useCanvasRailPeek();
  const offered = useBotsAvailable(on);
  const card = useBotsCard();
  const up = on && offered && card.up;
  const listRef = useRef<HTMLDivElement | null>(null);
  // LAZY, THEN KEPT (header).
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    if (up) setMounted(true);
  }, [up]);

  // THE CHAT IN THE CARD (header): a row's press; Back lets it go — and so
  // does the card going down: the next opening is the list.
  const [chat, setChat] = useState<string | null>(null);
  useEffect(() => {
    if (!up) setChat(null);
  }, [up]);
  const bot = useBotsSelector((s) => (chat ? s.bots.find((b) => b.id === chat) : undefined));
  // The list inert under an open chat.
  useLayoutEffect(() => {
    listRef.current?.toggleAttribute("inert", !!chat);
  }, [chat, mounted]);

  // PREV / NEXT through the list behind (the Tasks card's walk).
  const stepChat = (delta: number) => {
    if (!chat) return;
    const order = rowOrder(listRef.current);
    const at = order.indexOf(chat);
    const next = at < 0 ? null : (order[at + delta] ?? null);
    if (next) setChat(next);
  };
  const [ends, setEnds] = useState({ prev: false, next: false });
  const botCount = useBotsSelector((s) => s.bots.length);
  useEffect(() => {
    if (!chat) return;
    const order = rowOrder(listRef.current);
    const at = order.indexOf(chat);
    setEnds({ prev: at > 0, next: at >= 0 && at < order.length - 1 });
  }, [chat, botCount]);

  const name = bot ? bot.name || bot.id : "";
  const words = bot ? botStatusWords(bot.status) : "";
  const pick = useCallback((id: string) => setChat(id), []);
  const title = chat ? name || "Bot" : "Bots";

  return (
    <DoorCard store={botsCardStore} href="/bots" className="claude-rail-bots-peek" ariaLabel={chat ? `${title} — chat` : "Bots"} enabled={offered} subOpen={!!chat}>
      {(api) => (
        <>
          <div className="claude-rail-peek-head">
            {chat ? (
              <div className="claude-rail-peek-row">
                <button
                  ref={api.backRef}
                  type="button"
                  className="claude-rail-peek-btn claude-rail-peek-close claude-rail-bots-back"
                  aria-label="Back to bots"
                  title="Back to bots"
                  onClick={() => setChat(null)}
                >
                  <ArrowLeft size={14} aria-hidden="true" />
                </button>
                <button type="button" className="claude-rail-peek-btn" aria-label="Previous bot" title="Previous bot" disabled={!ends.prev} onClick={() => stepChat(-1)}>
                  <ChevronUp size={14} aria-hidden="true" />
                </button>
                <button type="button" className="claude-rail-peek-btn" aria-label="Next bot" title="Next bot" disabled={!ends.next} onClick={() => stepChat(1)}>
                  <ChevronDown size={14} aria-hidden="true" />
                </button>
                {bot && (
                  <span className="claude-rail-peek-botface" aria-hidden="true">
                    <Face b={bot} />
                  </span>
                )}
                <span className="claude-rail-peek-botname" title={title}>
                  {title}
                </span>
                <span className="claude-rail-peek-gap" aria-hidden="true" />
                {bot && <span className={"claude-rail-peek-words" + (bot.status === "waiting" ? " is-waiting" : "")}>{words}</span>}
                <button type="button" className="btn claude-rail-peek-open" title="Open this bot's page" onClick={() => api.openPage(botHref(chat))}>
                  Open
                </button>
              </div>
            ) : (
              <div className="claude-rail-peek-row">
                <button ref={api.closeRef} type="button" className="claude-rail-peek-btn claude-rail-peek-close" aria-label="Close Bots" title="Close" onClick={api.close}>
                  <X size={14} aria-hidden="true" />
                </button>
                <span className="claude-rail-tasks-title" title="Bots">
                  Bots
                </span>
                <span className="claude-rail-peek-gap" aria-hidden="true" />
                {/* "Open bots", the Tasks card's "Open tasks": this door is the page's, not a chat's. */}
                <button type="button" className="btn claude-rail-peek-open" title="Open the Bots page" onClick={() => api.openPage()}>
                  Open bots
                </button>
              </div>
            )}
          </div>
          <div className="claude-rail-peek-body">
            {mounted && (
              <div ref={listRef} className={"claude-rail-peek-chat claude-rail-bots-list" + (chat ? "" : " is-shown")}>
                <ListBoundary>
                  <Suspense fallback={<p className="claude-rail-peek-note">Loading…</p>}>
                    <BotPickList selected={chat} onPick={pick} />
                  </Suspense>
                </ListBoundary>
              </div>
            )}
            {chat && (
              <div key={chat} className="claude-rail-peek-chat is-shown">
                <ListBoundary>
                  <Suspense fallback={<p className="claude-rail-peek-note">Loading…</p>}>
                    <BotChat id={chat} markRead={api.pinned && api.visible} sharedScroll={false} />
                  </Suspense>
                </ListBoundary>
              </div>
            )}
          </div>
        </>
      )}
    </DoorCard>
  );
}
