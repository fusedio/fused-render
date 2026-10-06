// The thread (OpenBot chat.js render()'s thread half): one display:contents `.ev` wrapper per event (so a session
// divider or the "New" rule can precede a message while the thread stays one flex column), each role's body exactly as
// OpenBot's body(e) rendered it, the live/settled approval + question cards, reactions, the reply quote, app cards, and
// the scroll rules (follow along at the end, hold your place while reading, restore anchors after the 600-cap trim).
//
// Rows are keyed by bot + seq and memoized on primitive props, so a poll only mounts what arrived; DOM nodes (and any
// text selection) survive. Markdown goes through md() into ONE dangerouslySetInnerHTML site (HtmlMsg); its buttons
// (react / reply / offer options) are handled by the thread's delegated click handler, exactly like OpenBot.
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type RefObject } from "react";
import { AppCard } from "../apps/AppCard";
import { appFromText, showAppBeside, useAppsRoot } from "../apps/apps";
import { api, stepThumbUrl, type AppRef, type Bot, type BotEvent } from "../lib/api";
import { esc, fmtDay, fmtTime, fmtWhen } from "../lib/format";
import { md } from "../lib/md";
import { chosenOption, firstNewIndex, isNoise, liveCards, optionKey, rowKeys, searchCountText, searchHit, sessionBreak } from "../lib/thread";
import {
  act, cur, eventsOf, getState, markSeen, openDialog, setNewCount, setScrollToEnd, unviewed, useBots, viewedSet,
} from "../state/store";
import { END_GAP, gapOf, evBox, pinToEnd, restoreAnchor, topVisible, updateToBottom, type Anchor } from "./threadDom";
import { SetupLines } from "./SetupLines";

const EMPTY: BotEvent[] = [];
const NO_SEQS: ReadonlySet<number> = new Set();

// ---- the bubble's markup (built as escaped strings: it shares one innerHTML with the markdown) ----
const REACT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8.5 14.5a4.5 4.5 0 0 0 7 0"/><path d="M9 9.5h.01M15 9.5h.01"/></svg>';
const REPLY_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 17H5a4 4 0 0 1 0-8h14"/><path d="m15 5 4 4-4 4"/></svg>';
const actsHtml = (seq: number) =>
  `<span class="acts"><button data-react="${seq}" aria-label="React">${REACT_SVG}</button><button data-reply="${seq}" aria-label="Reply to this message">${REPLY_SVG}</button></span>`;
const ACTABLE = new Set(["user", "thought", "done"]);

/** The one dangerouslySetInnerHTML site: a message element whose inside is markup built from escaped text and md(). */
function HtmlMsg({ className, title, seq, html }: { className: string; title: string; seq?: number; html: string }) {
  return <div className={className} title={title} data-seq={seq} dangerouslySetInnerHTML={{ __html: html }} />;
}

const QUOTE_SVG = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M9 17H5a4 4 0 0 1 0-8h14" /><path d="m15 5 4 4-4 4" /></svg>
);

interface RowProps {
  e: BotEvent;
  botId: string;
  /** A session divider goes above this message. */
  day: boolean;
  /** The "New" rule goes above this message. */
  isNew: boolean;
  /** This message's reaction emoji ("" for none). */
  reaction: string;
  /** Delivery rows (D12) that record this message being sent on another channel; the "Texted: …" line under the bubble. */
  texted?: BotEvent[];
  /** Approval / question card: the bot waits on this one. */
  live: boolean;
  /** Answered question: your answer, normalized (optionKey); null otherwise. */
  chosen: string | null;
  /** The apps root (appFromText needs it; "" until loaded). */
  appsRoot: string;
  onBeside: (a: AppRef) => void;
}

/** An action event's raw tool result as display text ("" when there is none). The wire sends a string; anything else is shown as JSON. */
const detailText = (d: unknown): string => {
  if (d == null || d === "") return "";
  if (typeof d === "string") return d;
  try { return JSON.stringify(d, null, 2); } catch { return String(d); }
};

/**
 * One action step: the one-line chip, then (when present) the raw tool result and the page thumbnail (cache only; may
 * be gone) folded under it. Clicking the chip text opens / closes the result; clicking the thumbnail toggles its size.
 * The two are independent local state: nothing else needs to know.
 */
function Action({ text, result, detail, thumbSrc, title }: { text: string; result: string; detail: string; thumbSrc: string; title: string }) {
  const [thumbOpen, setThumbOpen] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const err = /^error/.test(result);
  const res = <span className={`res ${err ? "err" : ""}`}>{result}</span>;
  const toggle = () => setDetailOpen((o) => !o);
  const chip = detail ? (
    <div className={`msg action has-detail${detailOpen ? " open" : ""}`} title={`${title} · click for the full result`}
      role="button" tabIndex={0} aria-expanded={detailOpen} onClick={toggle}
      onKeyDown={(ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); } }}>
      <span className="line">{text}{res}</span><span className="more" aria-hidden="true">›</span>
    </div>
  ) : <div className="msg action" title={title}>{text}{res}</div>;
  if (!thumbSrc && !detail) return chip;
  return (
    <div className={`step${thumbOpen ? " open" : ""}`}>
      {chip}
      {detail && detailOpen ? <pre className="detail">{detail}</pre> : null}
      {thumbSrc ? <img className="thumb" loading="lazy" src={thumbSrc} alt="" title="Page after this step · click to enlarge" onClick={() => setThumbOpen((o) => !o)} /> : null}
    </div>
  );
}

function body({ e, botId, reaction, live, chosen, appsRoot, onBeside }: RowProps): JSX.Element {
  const title = fmtWhen(e.ts);
  if (e.role === "action") {
    return <Action text={e.text} result={e.result || ""} detail={detailText(e.detail)} thumbSrc={e.thumb ? stepThumbUrl(botId, e.thumb) : ""} title={title} />;
  }
  if (e.role === "note") {
    // A harness line ("Already ran …", "Still waiting for Approve / Deny on …"): quiet plain text, no bubble, no
    // react / reply / time. An app card follows it like any other message's. `detail` is not shown (yet).
    const app = e.app?.dir ? e.app : appFromText(e.text, appsRoot);
    return <><div className="msg note" title={title}>{e.text}</div>{app ? <AppCard app={app} onBeside={onBeside} /> : null}</>;
  }
  if (e.role === "approval") {
    // Live card while the bot is still waiting on this one; a plain note once answered.
    return (
      <div className={`msg approval${live ? "" : " settled"}`} data-seq={e.seq} title={title}>
        {e.text}
        <div className="btns"><button className="primary" data-approve="1" disabled={!live}>Approve</button><button data-deny="1" disabled={!live}>Deny</button></div>
      </div>
    );
  }
  if (e.role === "question") {
    // Optional one-click answers: lettered rows, like a quick poll. Typing in the composer still works.
    const options = e.options || [];
    const cls = `msg question${live ? "" : " settled"}${options.length ? " card" : ""}${e.offer ? " offer md" : ""}`;
    // An app offer: the bot's pitch (markdown, it may carry the findings), a proposed-app row for a new app or the regular
    // app card for an existing one, then Use it / Build it / Not now. The rows stay live while the offer is pending.
    const o = e.offer || null;
    const offerCard = o && e.app?.dir ? <AppCard app={e.app} onBeside={onBeside} /> : null;
    if (o) {
      const proposed = !e.app?.dir
        ? `<div class="proposed"><span class="ico">⧉</span><div class="txt"><b>${esc(o.name)}</b><small>${esc(o.kind === "use" ? "Existing app" : "New app · Claude builds it in a few minutes")}${o.spec ? " · " + esc(o.spec.replace(/\s+/g, " ").slice(0, 140)) : ""}</small></div></div>`
        : "";
      const opts = options.map((x, i) => `<button class="opt${chosen != null && optionKey(x) === chosen ? " chosen" : ""}" data-opt="${esc(x)}"${live ? "" : " disabled"}><kbd>${String.fromCharCode(65 + i)}</kbd><span>${esc(x)}</span></button>`).join("");
      return <><HtmlMsg className={cls} title={title} seq={e.seq} html={md(e.text) + proposed + (opts ? `<div class="opts">${opts}</div>` : "")} />{offerCard}</>;
    }
    return (
      <div className={cls} data-seq={e.seq} title={title}>
        {e.text}
        {options.length ? (
          <div className="opts">
            {options.map((x, i) => (
              <button key={i} className={`opt${chosen != null && optionKey(x) === chosen ? " chosen" : ""}`} data-opt={x} disabled={!live}>
                <kbd>{String.fromCharCode(65 + i)}</kbd><span>{x}</span>
              </button>
            ))}
          </div>
        ) : null}
      </div>
    );
  }
  const actable = ACTABLE.has(e.role);
  const viaLabel = (k: string) => (k === "imessage" ? "iMessage" : k === "botsend" ? "botsend" : k);
  // Bot text is markdown (bold, lists, code…); what the user typed stays verbatim.
  // A channel chip (docs §10) on a user line that arrived by text. Bot lines get a <Texted> line from their delivery rows instead.
  const viaChip = e.role === "user" && e.via?.kind && e.via.kind !== "web" && e.via.kind !== "routine"
    ? `<span class="via" title="${esc(`Received over ${viaLabel(e.via.kind)} from ${e.via.addr || "?"}`)}">via ${esc(viaLabel(e.via.kind))}</span>`
    : "";
  const html = (actable ? actsHtml(e.seq) + `<time class="when">${esc(fmtTime(e.ts))}</time>` : "")
    + (e.role === "user" ? esc(e.text) : md(e.text)) + viaChip
    + (actable ? `<span class="rx" data-react="${e.seq}" title="Change reaction">${esc(reaction)}</span>` : "");
  const cls = `msg ${e.role}${e.role === "user" ? "" : " md"}${actable && reaction ? " has-rx" : ""}`;
  const bubble = <HtmlMsg className={cls} title={title} seq={actable ? e.seq : undefined} html={html} />;
  // An app card follows the bubble when the event carries `app` (finished build, `show` action) or when the text links a
  // built app — a bot's older message, or a "Copy state" link the user pasted, which reopens the app at that state.
  const app = e.app?.dir ? e.app : appFromText(e.text, appsRoot);
  const card = app ? <AppCard app={app} onBeside={onBeside} /> : null;
  if (!e.reply?.text) return <>{bubble}{card}</>;
  return (
    <>
      <div className={`qwrap ${e.role}`}>
        <div className="quoted" title={e.reply.text}>{QUOTE_SVG}<span>{e.reply.text}</span></div>
        {bubble}
      </div>
      {card}
    </>
  );
}

const chanLabel = (k: string) => (k === "imessage" ? "iMessage" : k === "botsend" ? "botsend" : k);

/** D12: what else this message was sent as. One quiet line per channel under the bubble: the exact text that went out when it
 *  differs from the bubble (a summary, a numbered question, a cut), a bare "→ iMessage" when it is the same, "Not texted" on error. */
function Texted({ e, rows }: { e: BotEvent; rows: BotEvent[] }) {
  const norm = (s: string) => (s || "").replace(/^@\S+(?:\s\S+)?\s+/, "").replace(/\s+/g, " ").trim();
  return (
    <>
      {rows.map((d) => {
        const label = chanLabel(d.channel || "");
        if (d.error) return <div key={d.seq} className="texted err" title={d.error}>Not sent to {label}: {d.error}</div>;
        const same = norm(d.text) === norm(e.text);
        return same
          ? <div key={d.seq} className="texted" title={d.text}>→ {label}</div>
          : <div key={d.seq} className="texted" title={d.text}><b>Texted</b> ({label}): {d.text.replace(/\s+/g, " ")}</div>;
      })}
    </>
  );
}

const Row = memo(function Row(p: RowProps) {
  const { e, day, isNew } = p;
  if (isNoise(p.e)) return <div className="ev" />;
  return (
    <div className="ev" data-seq={e.seq}>
      {day ? <div className="day">{fmtDay(e.ts)}</div> : null}
      {isNew ? <div className="new">New</div> : null}
      {body(p)}
      {p.texted?.length ? <Texted e={e} rows={p.texted} /> : null}
    </div>
  );
});

export interface ThreadProps {
  /** The selected bot (undefined: the hero). */
  b: Bot | undefined;
  /** The #thread element; ChatPane shares it with ToBottom and the composer. */
  threadRef: RefObject<HTMLDivElement>;
  /** The normalized search query while the search bar is open with text, else "". */
  searchQ: string;
  /** The search count text ("3 matches", "No matches", "" when not filtering), after every render. */
  onSearchCount: (text: string) => void;
  /** A react button or a reaction badge was clicked: open the picker for that message. */
  onReact: (anchor: Element, seq: number) => void;
  /** A reply button was clicked: quote this message in the composer. */
  onReply: (r: { seq: number; text: string }) => void;
}

export function Thread({ b, threadRef, searchQ, onSearchCount, onReact, onReply }: ThreadProps) {
  const S = useBots();
  const appsRoot = useAppsRoot();
  const botId = b?.id ?? "";
  const evs = b ? S.events[b.id] || EMPTY : EMPTY;
  const firstSeq = evs.length ? evs[0].seq : 0;

  // Measured before the DOM changes: sitting at the end means you are following along, so whatever this render does to
  // the thread (append, or the rebuild once history is trimmed at the 600-event cap) must leave you at the end afterwards.
  // Scrolled up and reading: remember which messages sit at the top of the view and exactly where, so a rebuild can put
  // them back. Holding your distance from the bottom instead slid the page up by the height of every message that arrived.
  const measure = useRef<{ wasAtEnd: boolean; anchors: Anchor[] | null }>({ wasAtEnd: true, anchors: null });
  {
    const th = threadRef.current;
    const wasAtEnd = th ? gapOf(th) <= END_GAP : true;
    measure.current = { wasAtEnd, anchors: wasAtEnd || !th ? null : topVisible(th) };
  }
  const prev = useRef({ botId: "\u0000", firstSeq: -1, scrollThread: S.scrollThread });

  // ---- cards, reactions, the New rule ----
  const live = b ? liveCards(evs, b) : new Set<number>();
  // Cards settled on the first click, before the round trip: the poll that confirms the reply can lag, and every extra
  // click in that window would otherwise send another approve. Released when the send settles, so the next render
  // recomputes `settled` from the events and a failed send reopens the card.
  const [held, setHeld] = useState<ReadonlySet<number>>(NO_SEQS);
  const settleNow = (btn: Element, send: () => Promise<unknown>) => {
    const seq = Number(btn.closest(".msg")?.getAttribute("data-seq"));
    if (!Number.isFinite(seq)) { void send(); return; }
    setHeld((h) => new Set(h).add(seq));
    void send().finally(() => setHeld((h) => { if (!h.has(seq)) return h; const n = new Set(h); n.delete(seq); return n.size ? n : NO_SEQS; }));
  };
  const mark = S.newMark?.id === botId ? S.newMark.seq : null;
  const firstNew = firstNewIndex(evs, mark);  // first shown message you have not seen
  const rxs = b?.reactions || {};
  const onBeside = useCallback((a: AppRef) => showAppBeside(a), []);

  // ---- after every commit: scroll rules, search, viewed tracking, the pill, seen ----
  useLayoutEffect(() => {
    const th = threadRef.current; if (!th) return;
    const p = prev.current, s = getState();
    const botChanged = p.botId !== botId, rebuilt = botChanged || p.firstSeq !== firstSeq;
    // Opening a bot (select bumps scrollThread); a same-bot re-select, or leaving the live view, does not scroll (OpenBot's
    // `scrollThread && !sameRun`). The thread keeps rendering under the live view, so following along needs no catch-up.
    const bumped = p.scrollThread !== s.scrollThread && botChanged;
    prev.current = { botId, firstSeq, scrollThread: s.scrollThread };
    // Your own send (scrollToEnd), opening a bot, or anything arriving while you sit at the end keeps you at the bottom;
    // scrolled up, incoming messages leave you where you are and light the pill.
    const { wasAtEnd, anchors } = measure.current;
    if (bumped || s.scrollToEnd || wasAtEnd) pinToEnd(th, botId || undefined);
    // Anchors only ever fall off the top, so losing all of them means you were reading history the cap has now dropped:
    // the oldest message left is the closest thing to what you were looking at, so sit at the top of it.
    else if (rebuilt && !restoreAnchor(th, anchors)) th.scrollTop = 0;
    if (s.scrollToEnd) setScrollToEnd(false);

    // Search: filters the rendered thread in place and re-applies after each append.
    th.classList.toggle("filtering", !!searchQ);
    if (searchQ) {
      let n = 0;
      for (const el of th.children) { const hit = searchHit(el.textContent, searchQ); el.classList.toggle("hit", hit); if (hit) n++; }
      onSearchCount(searchCountText(n));
    } else onSearchCount("");

    // "Viewed" = a message has scrolled into the thread at least once. The .ev wrapper has no box: watch the message inside.
    const io = viewer.current;
    if (io) th.querySelectorAll<HTMLElement>(".ev[data-seq]:not([data-obs])").forEach((el) => { el.dataset.obs = "1"; const m = evBox(el); if (m) io.observe(m); });
    const c = cur();
    if (c) setNewCount(unviewed(c, eventsOf(c.id)).length);
    updateToBottom(th);
    // Sitting at the end with the tab visible means you are following along: keep "seen" current so a reload or re-open
    // does not draw a New line above messages you already watched arrive. Not while the live view covers the thread.
    const s2 = getState();
    if (c && !document.hidden && s2.pinned && !s2.fast) markSeen(c.id, c.seq);
  });

  // The observer (threshold .4, rooted at the thread) and the ResizeObserver that keeps a pinned thread pinned when its
  // box changes (the composer grows with a multi-line draft, the stop row, the reply quote).
  const viewer = useRef<IntersectionObserver | null>(null);
  useEffect(() => {
    const th = threadRef.current; if (!th) return;
    const io = new IntersectionObserver((entries) => {
      const c = cur(); if (!c) return;
      const set = viewedSet(c.id);
      let hit = false;
      for (const en of entries) {
        const seq = (en.target.closest(".ev") as HTMLElement | null)?.dataset.seq;
        if (en.isIntersecting && seq) { set.add(Number(seq)); hit = true; }
      }
      if (hit) { setNewCount(unviewed(c, eventsOf(c.id)).length); updateToBottom(th); }
    }, { root: th, threshold: 0.4 });
    viewer.current = io;
    th.querySelectorAll<HTMLElement>(".ev[data-seq]").forEach((el) => { el.dataset.obs = "1"; const m = evBox(el); if (m) io.observe(m); });
    const ro = new ResizeObserver(() => { if (getState().pinned) th.scrollTop = th.scrollHeight; });
    ro.observe(th);
    return () => { io.disconnect(); ro.disconnect(); viewer.current = null; };
  }, [threadRef]);

  // ---- clicks: react → picker, reply → quote, option → answer, approve / deny ----
  const onClick = (ev: ReactMouseEvent<HTMLDivElement>) => {
    const t = ev.target as Element, sel = getState().sel;
    const rc = t.closest("[data-react]");
    if (rc) { onReact(rc, Number(rc.getAttribute("data-react"))); return; }
    const rp = t.closest("[data-reply]");
    if (rp) {
      const seq = Number(rp.getAttribute("data-reply"));
      const e = eventsOf(sel).find((x) => x.seq === seq);
      if (e) onReply({ seq, text: (e.text || "").replace(/\s+/g, " ").trim() });
      return;
    }
    const opt = t.closest(".msg.question:not(.settled) .opt");
    if (opt && sel) { const text = opt.getAttribute("data-opt") || ""; settleNow(opt, () => act(() => api.send(sel, text))); return; }
    const ok = t.closest(".msg.approval:not(.settled) [data-approve]"), no = t.closest(".msg.approval:not(.settled) [data-deny]");
    if ((!ok && !no) || !sel) return;
    settleNow((ok || no)!, () => act(() => api.send(sel, ok ? "approve" : "deny")));
  };

  let content: JSX.Element | JSX.Element[];
  if (!b) {
    const first = !S.bots.length;
    content = (
      <div className="empty hero">
        <div className="herotitle">{first ? "No bots yet" : "No bot selected"}</div>
        <div className="herotext">{first ? "Create a bot and give it a task — it browses the web for you." : "Pick one on the left, or create a new one."}</div>
        <button id="emptyaddbot" className="primary" onClick={() => openDialog({ kind: "newBot" })}>+ New bot</button>
        {/* What this Mac still lacks (Chrome, Claude Code) and a link into
            fused-render's setup wizard, a route (components/SetupLines). */}
        <SetupLines first={first} />
      </div>
    );
  } else if (!evs.length) {
    content = <div className="empty">Say hello: give this bot a task.</div>;
  } else {
    const keys = rowKeys(evs, botId);
    // D12: delivery rows join the bubble they refer to (`ref`); they render nothing of their own (isNoise).
    const texted = new Map<number, BotEvent[]>();
    for (const e of evs) if (e.role === "delivery" && e.ref != null) texted.set(e.ref, [...(texted.get(e.ref) || []), e]);
    content = evs.map((e, i) => {
      const card = e.role === "approval" || e.role === "question";
      return (
        <Row key={keys[i]} e={e} botId={botId} day={sessionBreak(evs[i - 1], e)} isNew={i === firstNew} texted={texted.get(e.seq)}
          reaction={ACTABLE.has(e.role) ? rxs[e.seq] || "" : ""} live={card && live.has(e.seq) && !held.has(e.seq)}
          chosen={e.role === "question" ? chosenOption(evs, e.seq) : null} appsRoot={appsRoot} onBeside={onBeside} />
      );
    });
  }

  return (
    <div className="thread" id="thread" ref={threadRef} data-bot={botId || undefined}
      onScroll={() => updateToBottom(threadRef.current)} onClick={onClick}>
      {content}
    </div>
  );
}
