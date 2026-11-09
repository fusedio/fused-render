// THE BOTS, AS A LIST TO PICK FROM (shell/claude-rail BotsPeek; owner,
// 2026-11-09: "a popover window A which first shows bots, and on click shows
// its chats"): the Bots page's left column — its search, its rows (BotList
// `Row`: face and status dot, name, SUPER tag, time, last line, the unread
// badge) in the page's order (`listRows`), the hidden group under its toggle
// — for a card off the page. Without the page's header (usage, browsers, +,
// collapse) and footer (builds, apps, preferences): the card's head has one
// door, Open bots, and the page keeps the rest.
//
// A PRESS IS THE CALLER'S (`onPick`): the page's row selects, writes the URL
// and tells the server (store `openBot`); here the store's selection is left
// alone — off the Bots page nothing is selected, and `select` would write the
// page's URL — and the card opens that bot's chat in its body. The row drawn
// selected is the card's open chat (`selected`), not the page's.
//
// FED WITHOUT THE PAGE (BotChat's rule): the store's quiet feed
// (`feedBotQuietly`) — the same poll, the merge alone — which stands aside
// while the page's own loop runs.
//
// THE PAGE'S STYLESHEET APPLIES: the root wears `.bots-page` (every rule in
// styles/bots.css is scoped to it) and `.bots-peek`, with `section.bots` for
// the column's own rules — the list's container query hides the time where
// the card is narrow, as the page's column does.
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import "../styles/bots.css";
import { feedBotQuietly, setShowHidden, useBots } from "../state/store";
import { Row, listRows } from "./BotList";

export interface BotPickListProps {
  /** The bot whose chat the card shows, drawn as the selected row. */
  selected: string | null;
  /** A row's press. */
  onPick: (id: string) => void;
}

export default function BotPickList({ selected, onPick }: BotPickListProps) {
  useEffect(() => feedBotQuietly(), []);
  const S = useBots();
  const [q, setQ] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const { shown, hidden, query } = listRows(S, q);
  const polled = S.bots.length > 0;
  const onSearchKey = (e: KeyboardEvent<HTMLInputElement>) => {
    // Escape clears a query first; empty, it is the card's (DoorCard: a
    // preview closes, a pinned card stays).
    if (e.key === "Escape" && q) {
      e.preventDefault();
      setQ("");
    }
  };
  return (
    <div className="bots-page bots-peek bots-pick">
      <section className="bots">
        <label className="search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.5-3.5" />
          </svg>
          <input type="search" placeholder="Search bots" aria-label="Search bots" autoComplete="off" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onSearchKey} />
        </label>
        <div className="listwrap">
          <div className="botlist" ref={listRef} role="list" aria-label="Bots">
            {!polled ? (
              <div className="empty">{S.banner.show ? S.banner.text : "Loading…"}</div>
            ) : !S.bots.length ? (
              <div className="empty">
                No bots yet.
                <br />
                Add one on the Bots page.
              </div>
            ) : (
              <>
                {shown.map((b) => (
                  <Row key={b.id} b={b} S={S} sel={b.id === selected} onOpen={onPick} />
                ))}
                {!shown.length && query ? <div className="empty">No bot matches.</div> : null}
                {hidden.length ? (
                  <div className="group" onClick={() => setShowHidden(!S.showHidden)}>
                    {S.showHidden ? "▾" : "▸"} Hidden ({hidden.length})
                  </div>
                ) : null}
                {hidden.length && S.showHidden ? hidden.map((b) => <Row key={b.id} b={b} S={S} sel={b.id === selected} onOpen={onPick} />) : null}
              </>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
