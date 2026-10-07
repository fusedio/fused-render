// Left column (OpenBot index.html section.bots + chat.js render()'s list half): header (usage meter, +, collapse),
// search, the list (pinned → waiting-unread → your last message), the hidden group, and the Builds / Apps footer.
// Rows that change slot glide (FLIP, lib/layout glideRows); new rows fade in. Right-click hands off to the menu.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Bot, BotEvent } from "../lib/api";
import { activeHandoff, lastBotMsg, lastTs, lastUserTs, routineGlyph, routineNote, statusLabel } from "../lib/derive";
import { fmtAgo, fmtWhen } from "../lib/format";
import { glideRows, rowOffsets, toggleLeft } from "../lib/layout";
import { waitingUnread } from "../lib/unread";
import { openBot, openDialog, setShowHidden, unreadCount, useBots, type BotsState } from "../state/store";
import { Face } from "./Face";

export interface BotListProps {
  /** #add and the empty thread's "+ New bot". */
  onAddBot: () => void;
  /** #usage: the Model usage dialog. */
  onOpenUsage: () => void;
  /** #browsers: the Browsers dialog (the logins bots share). */
  onOpenBrowsers: () => void;
  /** #builds: the Builds panel (builds/ also clears the chip's `fresh`). */
  onOpenBuilds: () => void;
  /** #apps: the Apps gallery. */
  onOpenApps: () => void;
  /** Right-click on a row, at the pointer (OpenBot openMenu(id, x, y)). */
  onContextMenu: (id: string, x: number, y: number) => void;
}

const EMPTY: BotEvent[] = [];

function Row({ b, S, onContextMenu }: { b: Bot; S: BotsState; onContextMenu: BotListProps["onContextMenu"] }) {
  const evs = S.events[b.id] || EMPTY, n = unreadCount(b), sel = b.id === S.sel;
  const msg = lastBotMsg(evs), ts = lastTs(evs), glyph = routineGlyph(b), note = b.status === "idle" ? routineNote(b) : null;
  // Super Bot with a hand-off in flight (docs §11): that outranks its last message, which is "I asked <Bot>…" anyway.
  const ho = b.kind === "super" ? activeHandoff(b) : null;
  return (
    <div className={`bot${sel ? " sel" : ""}`} data-id={b.id} title="Right-click for options"
      onClick={() => openBot(b.id)}
      onContextMenu={(e) => { e.preventDefault(); onContextMenu(b.id, e.clientX, e.clientY); }}>
      {/* The selected row's avatar opens Settings, like the header avatar; on any other row it just selects. */}
      <span className={`av${sel ? " settings" : ""}`} title={sel ? "Settings" : undefined}
        onClick={sel ? (e) => { e.stopPropagation(); openDialog({ kind: "settings", id: b.id }); } : undefined}>
        <Face b={b} svgId={sel ? "lface" : undefined} /><span className={`dot ${b.status}${n ? " unread" : ""}`} /></span>
      <div className="name">
        {b.pinned ? <span className="pin">📌</span> : null}{b.name}
        {b.kind === "super" ? <span className="super" title="Super Bot: Claude Code's own tools on this Mac">SUPER</span> : null}
        {glyph ? <span className={`rt ${glyph.warn ? "warn" : ""}`} title={glyph.title}>⟳</span> : null}
      </div>
      <div className="meta">
        {n ? <span className={`badge ${b.status === "waiting" ? "q" : ""}`} title={`${n} new message${n > 1 ? "s" : ""}`} /> : null}
        <span className="time" title={fmtWhen(ts)}>{fmtAgo(ts)}</span>
      </div>
      <div className="sub">
        {ho ? `waiting on ${ho.target_name || "a bot"}` : msg || (<>
          {b.status === "idle" && b.task ? b.task : statusLabel(b)}
          {b.status !== "idle" && b.title ? " · " + b.title : ""}
          {note ? <> · {note.warn ? <span className="warn">{note.text}</span> : note.text}</> : null}
        </>)}
      </div>
    </div>
  );
}

export function BotList({ onAddBot, onOpenUsage, onOpenBrowsers, onOpenBuilds, onOpenApps, onContextMenu }: BotListProps) {
  const S = useBots();
  const [q, setQ] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  // The FLIP "before": each row's slot as the DOM stands right now, read before React commits this render (OpenBot read it before rebuilding).
  const before = useRef<Record<string, number>>({});
  before.current = rowOffsets(listRef.current);
  useLayoutEffect(() => { glideRows(listRef.current, before.current); });
  // The list's native scrollbar is hidden (it would reserve a gutter and clip the selection disc); this thumb overlays the
  // right edge instead and follows scrollTop. Styled directly so a scroll never re-renders the rows.
  const barRef = useRef<HTMLDivElement>(null);
  const syncBar = () => {
    const el = listRef.current, bar = barRef.current;
    if (!el || !bar) return;
    const { scrollTop, scrollHeight, clientHeight } = el;
    if (scrollHeight <= clientHeight + 1) { bar.style.display = "none"; return; }
    const h = Math.max(24, (clientHeight / scrollHeight) * clientHeight);
    bar.style.display = "";
    bar.style.height = `${h}px`;
    bar.style.transform = `translateY(${(scrollTop / (scrollHeight - clientHeight)) * (clientHeight - h)}px)`;
  };
  useLayoutEffect(syncBar);
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const ro = new ResizeObserver(syncBar);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Pinned first, then bots waiting on you that you haven't looked at yet, then by your own last message (newest first). Hidden bots collapse under a toggle.
  // Opening a waiting bot marks its events seen, so it drops back into the normal order instead of sitting at the top until answered.
  const recency = (b: Bot) => lastUserTs(S.events[b.id] || EMPTY) || b.created || 0;
  const waiting = (b: Bot) => waitingUnread(b, S.seen[b.id], unreadCount(b));
  const order = (a: Bot, b: Bot) => (Number(!!b.pinned) - Number(!!a.pinned)) || (Number(waiting(b)) - Number(waiting(a))) || (recency(b) - recency(a));
  const query = q.trim().toLowerCase();
  const match = (b: Bot) => !query || `${b.name} ${b.task || ""} ${b.title || ""}`.toLowerCase().includes(query);
  const shown = S.bots.filter((b) => !b.hidden && match(b)).sort(order);
  const hidden = S.bots.filter((b) => b.hidden && match(b)).sort(order);

  const u = S.usage, chip = S.buildsChip;
  return (
    <section className="bots">
      <header>
        <h1>Bots</h1>
        <button id="usage" onClick={onOpenUsage}
          title={u ? `${u.hour} in the last hour · ${u.errors} failed · click for details` : "Model calls made today · click for details"}>
          {u ? `${u.today} call${u.today === 1 ? "" : "s"} today` : "…"}
        </button>
        <button id="browsers" className="ptog" title="Browsers · the logins your bots share" onClick={onOpenBrowsers}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3Z" /></svg>
        </button>
        <button id="add" title="New bot" onClick={onAddBot}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        </button>
        <button id="lcol" className="ptog" title="Collapse or expand the bot list" onClick={toggleLeft}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" aria-hidden="true"><rect className="pane" x="3" y="4" width="6" height="16" rx="2" stroke="none" /><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M9 4v16" /></svg>
        </button>
      </header>
      <label className="search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
        <input id="botq" type="search" placeholder="Search" autoComplete="off" value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") { setQ(""); e.currentTarget.blur(); } }} />
      </label>
      <div className="listwrap">
      <div className="botlist" id="botlist" ref={listRef} onScroll={syncBar}>
        {!S.bots.length ? <div className="empty">No bots yet.<br />Add one and give it a task.</div> : (<>
          {shown.map((b) => <Row key={b.id} b={b} S={S} onContextMenu={onContextMenu} />)}
          {!shown.length && query ? <div className="empty">No bot matches.</div> : null}
          {hidden.length ? <div className="group" id="hiddentoggle" onClick={() => setShowHidden(!S.showHidden)}>{S.showHidden ? "▾" : "▸"} Hidden ({hidden.length})</div> : null}
          {hidden.length && S.showHidden ? hidden.map((b) => <Row key={b.id} b={b} S={S} onContextMenu={onContextMenu} />) : null}
        </>)}
      </div>
      <div className="railbar" ref={barRef} aria-hidden="true" />
      </div>
      <footer className="botfoot">
        <button id="builds" className={`ptog${chip.live ? " live" : ""}${chip.warn ? " warn" : ""}${chip.fresh ? " fresh" : ""}`} title={chip.title}
          style={chip.hidden ? { display: "none" } : undefined} onClick={onOpenBuilds}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m3 17 2 2 4-4" /><path d="m3 7 2 2 4-4" /><path d="M13 6h8" /><path d="M13 12h8" /><path d="M13 18h8" /></svg>
          <span className="n" id="buildsn">{chip.n}</span>
        </button>
        <button id="apps" className="ptog" title="Apps · everything the builds have made" onClick={onOpenApps}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></svg>
        </button>
      </footer>
    </section>
  );
}
