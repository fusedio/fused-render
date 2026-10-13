// The thread (OpenBot chat.js render()'s thread half): one display:contents `.ev` wrapper per event (so a session
// divider or the "New" rule can precede a message while the thread stays one flex column), each role's body exactly as
// OpenBot's body(e) rendered it, the live/settled approval + question cards, reactions, the reply quote, app cards, and
// the scroll rules (follow along at the end, hold your place while reading, restore anchors after the 600-cap trim).
//
// Rows are keyed by bot + seq and memoized on primitive props, so a poll only mounts what arrived; DOM nodes (and any
// text selection) survive. Markdown goes through md() into ONE dangerouslySetInnerHTML site (HtmlMsg); its buttons
// (react / reply / offer options) are handled by the thread's delegated click handler, exactly like OpenBot.
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type RefObject } from "react";
import { AppCard } from "../apps/AppCard";
import { appFromText, showAppBeside, useAppsRoot } from "../apps/apps";
import { openBuilds, getBuildRows } from "../builds/builds";
import { api, stepThumbUrl, type AppRef, type Bot, type BotEvent } from "../lib/api";
import { esc, fmtDay, fmtSecs, fmtTime, fmtWhen } from "../lib/format";
import { md } from "../lib/md";
import { botsRoot } from "../lib/root";
import { chosenOption, clearIndex, firstNewIndex, foldHint, foldStretches, isHandoff, isNoise, liveCards, optionKey, rowKeys, searchCountText, searchHit, sessionBreak } from "../lib/thread";
import { act, clearScrollSeq, cur, eventsOf, getState, jumpTo, markSeen, openDialog, select, setNewCount, setScrollToEnd, showBanner, unviewed, useBots, viewedSet } from "../state/store";
import { END_GAP, clearSearchHighlight, gapOf, evBox, highlightSearch, pinToEnd, restoreAnchor, topVisible, updateToBottom, type Anchor } from "./threadDom";
import { HandBackRow, HandoverNote } from "./HandoverCard";
import { SetupLines } from "./SetupLines";

const EMPTY: BotEvent[] = [];
const NO_SEQS: ReadonlySet<number> = new Set();
const NO_RX: Record<number, string> = {};

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
  /** The question a hand back settled (Bot.handback): how long you drove, and the step still of the page as you left it. */
  hbSecs?: number;
  hbShot?: string | null;
  /** The live hand-over question while you hold the browser (Bot.control): the bot, for the hand-back row it ends with. */
  hbBot?: Bot;
  /** Your message queued while you hold the browser (Bot.held): greyed, dashed, sent when you hand back. */
  queued?: boolean;
  /** Answered question: your answer, normalized (optionKey); null otherwise. */
  chosen: string | null;
  /** The apps root (appFromText needs it; "" until loaded). */
  appsRoot: string;
  onBeside: (a: AppRef) => void;
  /** Hand-off card: the hand-off's live state from Super Bot's `handoffs` (the event's own stamp is as of writing). */
  hstate?: string;
  /** Hand-off card: the bot it went to still exists ("Open <Bot>'s chat" is live). */
  targetLive?: boolean;
  /** Build notice (`e.build`): still the build's current stall (buildOpen). A prop for the same reason as linkLive. */
  bopen?: boolean;
  /** "Sent to Super Bot" line: the bot its `link` points at still exists ("Read more" is live). A prop, not a store read:
   *  Row is memoized and would keep a stale answer. */
  linkLive?: boolean;
  /** A row before the last Clear context marker while the earlier conversation is folded: hidden. */
  folded?: boolean;
  /** The last Clear context marker: how many messages sit above it, whether they are shown, and the toggle. */
  foldN?: number;
  foldOpen?: boolean;
  onFold?: () => void;
  /** This row carries a fold trigger (lib/thread `foldStretches`): the state of the stretch, the stretch's key (the
   *  seq the toggle is keyed by, `data-fold`) and the hover text. `bare`: this is the stretch's first hidden row standing
   *  in for it as one muted line, because the row that ends the stretch (a card, your message) has no line to seat two
   *  words on; otherwise the trigger goes on this bubble's last line. The click is the thread's delegated handler, so the
   *  row needs no callback prop and its memo holds. Absent on every other row. */
  fold?: "open" | "closed";
  foldKey?: number;
  foldHint?: string;
  bare?: boolean;
}

/** The two words (cloud's RunTrigger): never a count, the count is the hover. */
const foldHtml = (state: "open" | "closed", key: number, hint: string | undefined, bare?: boolean) =>
  `<button class="fold${bare ? " bare" : ""}" data-fold="${key}" aria-expanded="${state === "open"}" title="${esc(hint || "")}">${state === "open" ? "show less" : "show more"}</button>`;

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

/** Super Bot's hand-off line (docs §11): who it asked, where that stands, and (on the result) exactly the text the bot
 *  sent back. The header already names the bot and the step, so the body drops the matching lead-in of the text. */
/** Hand-off events written before 2026-10-07 carry the old state names; bot.json rows were renamed on load, events were not. */
const LEGACY_HANDOFF_STATE: Record<string, string> = { queued: "received", running: "working", waiting: "blocked", error: "failed", stopped: "cancelled" };

function HandoffCard({ e, state: rawState, targetLive }: { e: BotEvent; state: string; targetLive: boolean }) {
  const state = LEGACY_HANDOFF_STATE[rawState] || rawState;
  const h = e.handoff!, who = h.target_name || "the bot";
  const head = e.role === "system" ? `Asked ${who}`
    : e.role === "question" ? `${who} needs you at the laptop`
    : state === "failed" ? `${who} failed` : state === "cancelled" ? `${who} stopped` : `${who} finished`;
  const lead = e.role === "system" ? /^Asked .+? to:\s*/ : e.role === "question" ? /^.+? needs you at the laptop:\s*/ : null;
  const text = lead ? e.text.replace(lead, "") : e.text;
  return (
    <div className={`handoff-card ${state}`} data-seq={e.seq} title={fmtWhen(e.ts)}>
      <div className="hd"><span className="ttl">{head}</span><span className={`pill ${state}`}>{state}</span></div>
      {text ? <HtmlMsg className="msg md" title={fmtWhen(e.ts)} html={md(text)} /> : null}
      <div className="ft">
        <button disabled={!targetLive} title={targetLive ? (h.task_dir || undefined) : "That bot is gone"}
          onClick={() => { if (getState().bots.some((x) => x.id === h.target)) select(h.target); }}>Open {who}'s chat</button>
      </div>
    </div>
  );
}

/** A build that stopped moving (bot.py _surface_build_stall): what Claude waits on, and the door to that task under
 *  Builds, where Claude's own card is answered. `open`: still the build's current stall; otherwise it reads resolved. */
const BUILD_STALL: Record<string, { pill: string; cls: string }> = {
  permission: { pill: "needs your OK", cls: "blocked" }, question: { pill: "question", cls: "blocked" },
  failed: { pill: "failed", cls: "failed" }, usage_limit: { pill: "paused", cls: "cancelled" },
};
function BuildCard({ e, open }: { e: BotEvent; open: boolean }) {
  const b = e.build!, st = BUILD_STALL[b.reason] || { pill: b.reason, cls: "blocked" };
  const what = b.reason === "permission" && (b.tool || b.summary)
    ? `Claude wants to run **${b.tool || "a tool"}**${b.summary ? `\n\n\`${b.summary.replace(/`/g, "'")}\`` : ""}`
    : b.reason === "question" && b.summary ? `Claude asks: ${b.summary}` : e.text;
  return (
    <div className={`handoff-card build ${open ? st.cls : "settled"}`} data-seq={e.seq} title={fmtWhen(e.ts)}>
      <div className="hd"><span className="ttl">Build · {b.name}</span><span className={`pill ${open ? st.cls : "cancelled"}`}>{open ? st.pill : "resolved"}</span></div>
      <HtmlMsg className="msg md" title={fmtWhen(e.ts)} html={md(what)} />
      <div className="ft">
        <button className={open && st.cls === "blocked" ? "primary" : undefined} disabled={!b.key}
          title={b.dir || undefined} onClick={() => openBuilds(b.key)}>{open && st.cls === "blocked" ? "Answer in Builds" : "Open in Builds"}</button>
      </div>
    </div>
  );
}

/** Is this build notice still the build's current stall: the newest notice for that build, and (when the Builds feed has
 *  the row) the task still waiting or blocked. Without the row (feed not loaded yet) the newest notice counts as open. */
function buildOpen(evs: BotEvent[], e: BotEvent): boolean {
  const b = e.build!, same = (x: BotEvent) => x.source === "build" && (b.entry_id ? x.build?.entry_id === b.entry_id || x.app?.dir === b.dir : x.build?.key === b.key);
  if (evs.some((x) => x.seq > e.seq && same(x))) return false;
  const row = getBuildRows().find((t) => (b.entry_id && t.entry_id === b.entry_id) || t.key === b.key);
  return !row || row.status === "needs_attention" || row.status === "blocked";
}

/** The Clear context marker (Bot.clear_context): a rule across the thread, and (on the last marker, when anything sits
 *  above it) the toggle that unfolds the earlier conversation. Earlier rows stay for you to read; the bot no longer sees them. */
function ClearMark({ e, n, open, onFold }: { e: BotEvent; n: number; open: boolean; onFold?: () => void }) {
  return (
    <div className="cleared" title={`${e.text}\n${fmtWhen(e.ts)}`}>
      <span className="lbl">Context cleared · {fmtTime(e.ts)}</span>
      {n && onFold ? (
        <button aria-expanded={open} onClick={onFold}>
          {open ? "Hide" : "Show"} earlier conversation · {n}
        </button>
      ) : null}
    </div>
  );
}

/** Channel names in chips and "Texted" lines; a task Super Bot handed off reads "from Super Bot" (docs §11). */
const chanLabel = (k: string) => (k === "imessage" ? "iMessage" : k === "handoff" ? "Super Bot" : k);

function body({ e, botId, reaction, live, chosen, appsRoot, onBeside, hstate, targetLive, linkLive, hbSecs, hbShot, hbBot, queued, bopen, foldN, foldOpen, onFold, fold, foldKey, foldHint: hint, bare }: RowProps): JSX.Element {
  const title = fmtWhen(e.ts);
  if (e.source === "clear") return <ClearMark e={e} n={foldN || 0} open={!!foldOpen} onFold={onFold} />;
  if (e.handoff && isHandoff(e)) return <HandoffCard e={e} state={hstate || e.handoff.state} targetLive={!!targetLive} />;
  if (e.build && e.source === "build") return <BuildCard e={e} open={!!bopen} />;
  // The target bot's "Sent to Super Bot: …" (what went up): a quiet harness line.
  if (isHandoff(e) && e.role === "system") {
    const l = e.link;
    return (
      <div className="msg note" title={title}>
        {e.text}
        {l ? <button className="readmore" data-jump-bot={l.bot} data-jump-seq={l.seq} disabled={!linkLive} title={linkLive ? "Open Super Bot's chat at this message" : "Super Bot is gone"}>Read more</button> : null}
      </div>
    );
  }
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
        <div className="btns">
          <button className="primary" data-approve="1" disabled={!live}>Approve</button>
          <button data-deny="1" disabled={!live}>Deny</button>
          {e.forever ? (
            // The third answer: a yes that also writes the card's rule (bot.json allow_rules), so this bot never asks
            // again when it does the same thing. Only cards whose call can be remembered carry `forever`.
            <button className="forever" data-forever="1" disabled={!live} title={`Never ask again when this bot ${e.forever.label}. Undo it in Settings › Permissions.`}>
              Approve, don't ask again
            </button>
          ) : null}
        </div>
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
    if (e.multi && options.length) return <MultiQuestion e={e} botId={botId} cls={cls} title={title} live={live} chosen={chosen} />;
    // A hand-over you handed back: settled like an answered question, plus how long you drove and the page as you left it.
    if (hbSecs != null && !live) {
      return (
        <div className={`${cls} settled handedback`} data-seq={e.seq} title={title}>
          {e.text}
          <div className="hbfoot">You handed it back · {fmtSecs(hbSecs)}</div>
          {hbShot ? <img className="hbshot" loading="lazy" src={stepThumbUrl(botId, hbShot)} alt="The page as you left it" /> : null}
        </div>
      );
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
        {hbBot ? <HandBackRow b={hbBot} /> : null}
      </div>
    );
  }
  const actable = ACTABLE.has(e.role);
  // Bot text is markdown (bold, lists, code…); what the user typed stays verbatim.
  // A channel chip (docs §10) on a user line that arrived by text, or that Super Bot handed off. Bot lines get a <Texted> line from their delivery rows instead.
  const k = e.via?.kind;
  const viaChip = e.role === "user" && k && k !== "web" && k !== "routine"
    ? k === "handoff"
      ? `<span class="via" title="Handed off by Super Bot">from ${esc(chanLabel(k))}</span>`
      : k === "app"
        // An app page's send through `fused.bots` (docs §13): the addr is "<app key>:<call id>"; the key is what the user recognises.
        ? `<span class="via" title="${esc(`Sent by the app ${(e.via!.addr || "").split(":")[0] || "?"} through fused.bots`)}">from ${esc((e.via!.addr || "").split(":")[0] || "an app")}</span>`
        : `<span class="via" title="${esc(`Received over ${chanLabel(k)} from ${e.via!.addr || "?"}`)}">via ${esc(chanLabel(k))}</span>`
    : "";
  // The seeded Super Bot's greeting (registry.seed_super, source "seed") carries the door to Settings > Phone.
  const seedBtn = e.role === "done" && e.source === "seed"
    ? '<div class="opts"><button class="opt seed" data-dialog="phone">Connect your phone</button></div>' : "";
  // THE STRETCH'S TRIGGER ON THIS BUBBLE'S LAST LINE (lib/thread `foldStretches`, the cloud seat rule): the two words
  // go INSIDE the last paragraph the markdown produced, so they sit on the sentence's own line, right edge, in the
  // reading type, the way the cloud transcript's `show more` does. A bubble whose markdown does not end in a paragraph
  // (a list, a code block) gets them on a line of their own after it.
  const trigger = fold && !bare && foldKey != null ? foldHtml(fold, foldKey, hint) : "";
  let bodyHtml = e.role === "user" ? esc(e.text) : md(e.text);
  if (trigger) bodyHtml = bodyHtml.endsWith("</p>") ? bodyHtml.slice(0, -4) + trigger + "</p>" : bodyHtml + `<p class="foldline">${trigger}</p>`;
  const html = (actable ? actsHtml(e.seq) + `<time class="when">${esc(fmtTime(e.ts))}</time>` : "")
    + bodyHtml + seedBtn + viaChip
    + (actable ? `<span class="rx" data-react="${e.seq}" title="Change reaction">${esc(reaction)}</span>` : "");
  const cls = `msg ${e.role}${e.role === "user" ? "" : " md"}${actable && reaction ? " has-rx" : ""}${queued ? " queued" : ""}`;
  const bubble = <HtmlMsg className={cls} title={queued ? "Queued: sent when you hand the browser back" : title} seq={actable ? e.seq : undefined} html={html} />;
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

/** A question with `multi`: tick any number of rows, then Done sends them as "A, B" (or "None"). Once answered, the rows
 *  named in the answer stay ticked. */
function MultiQuestion({ e, botId, cls, title, live, chosen }: { e: BotEvent; botId: string; cls: string; title: string; live: boolean; chosen: string | null }) {
  const options = e.options || [];
  const answered = chosen != null ? new Set(chosen.split(/\s*(?:,|\band\b)\s*/).map(optionKey).filter(Boolean)) : null;
  const [picked, setPicked] = useState<Set<string>>(() => new Set());
  const [sent, setSent] = useState(false);
  const sending = useRef(false);  // a second click before the re-render must not send twice
  const on = (x: string) => (answered ? answered.has(optionKey(x)) : picked.has(x));
  const toggle = (x: string) => { if (!live || sent) return; setPicked((p) => { const n = new Set(p); if (n.has(x)) n.delete(x); else n.add(x); return n; }); };
  const done = () => {
    if (!live || sent || sending.current) return;
    sending.current = true;
    setSent(true);
    const text = options.filter((x) => picked.has(x)).join(", ") || "None";
    // act() swallows a failed send into the banner and returns undefined: reopen the card then, like the single-choice rows.
    void act(() => api.send(botId, text)).then((r) => { sending.current = false; if (!r) setSent(false); });
  };
  return (
    <div className={`${cls} multi${sent ? " settled" : ""}`} data-seq={e.seq} title={title}>
      {e.text}
      <div className="opts">
        {options.map((x, i) => (
          <button key={i} className={`opt${on(x) ? " chosen" : ""}`} data-mopt={x} disabled={!live || sent} onClick={() => toggle(x)}>
            <kbd>{String.fromCharCode(65 + i)}</kbd><span>{x}</span>
          </button>
        ))}
      </div>
      {live && !sent ? (
        <div className="btns">
          <button className="primary" onClick={done}>{picked.size ? `Done · ${picked.size}` : "None of these"}</button>
        </div>
      ) : null}
    </div>
  );
}

const Row = memo(function Row(p: RowProps) {
  const { e, day, isNew } = p;
  const cls = p.folded ? "ev folded" : "ev";
  if (isNoise(p.e)) return <div className={cls} />;
  // A BARE TRIGGER: this row is the first of a folded stretch whose end has no bubble to seat the words on, so it
  // stands in for the stretch as one muted line (cloud's `is-bare` seat). Open, the line stays above the steps and
  // reads `show less`; closed, the steps behind it are `.folded` and this line is all that is left of them.
  const bare = p.bare && p.fold && p.foldKey != null
    ? <div className="msg foldbare" dangerouslySetInnerHTML={{ __html: foldHtml(p.fold, p.foldKey, p.foldHint, true) }} />
    : null;
  return (
    <div className={cls} data-seq={e.seq}>
      {day ? <div className="day">{fmtDay(e.ts)}</div> : null}
      {isNew ? <div className="new">New</div> : null}
      {bare}
      {bare && p.fold === "closed" ? null : body(p)}
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
  const rxs = b?.reactions || NO_RX;
  const onBeside = useCallback((a: AppRef) => showAppBeside(a), []);

  // ---- Clear context: rows before the last marker fold behind its toggle (shown again while searching) ----
  const ci = clearIndex(evs);
  const foldKey = ci > 0 ? `${botId}:${evs[ci].seq}` : "";
  const [openFold, setOpenFold] = useState("");
  const folded = !!foldKey && openFold !== foldKey && !searchQ;
  const foldN = ci > 0 ? evs.slice(0, ci).filter((e) => !isNoise(e)).length : 0;
  // The toggle keeps the marker where it sits on screen: the earlier rows open (or close) above it, not under your pointer.
  const foldRef = useRef({ key: "", anchor: null as Anchor | null });
  foldRef.current.key = foldKey;
  const onFold = useCallback(() => {
    const th = threadRef.current, k = foldRef.current.key; if (!th || !k) return;
    const seq = k.slice(k.lastIndexOf(":") + 1), box = evBox(th.querySelector(`.ev[data-seq="${seq}"]`));
    foldRef.current.anchor = box ? { seq, y: box.getBoundingClientRect().top - th.getBoundingClientRect().top } : null;
    setOpenFold((o) => (o === k ? "" : k));
  }, [threadRef]);

  // ---- step fold: the bot's process folds behind the exchange, one fold per stretch (lib/thread `foldStretches`) ----
  //
  // SEEDED ONCE PER BOT, THEN THE READER'S. The cloud transcript learnt this the hard way (2026-09-17): a fold that
  // re-derives every render shuts the minute-old answer under a reader who is mid-conversation. So on the first paint
  // with this bot's events, every stretch lands closed except the newest task's (the one you came back for, settled or
  // still running), and from then on nothing here moves a fold except a click. A stretch that settles while you watch
  // arrives open and stays open. Switching bots is a fresh seed.
  //
  // AND THE SEED NEVER HIDES WHAT YOU HAVE NOT SEEN: a stretch with a row past the seen mark (a routine that ran
  // overnight) stays open, so the New rule and the unread count still point at rows that are on screen.
  //
  // A REF PLUS A BUMP, like the Clear fold's anchor: the map is read during render, and every row's props must stay
  // identical across a click for Row's memo to hold.
  const busy = !!b && (b.status === "running" || b.status === "waiting");
  const folds = useMemo(() => foldStretches(evs, rxs, busy), [evs, rxs, busy]);
  const stretchRef = useRef({ bot: "\u0000", seeded: false, open: new Map<number, boolean>() });
  const [, bumpFold] = useState(0);
  if (stretchRef.current.bot !== botId) stretchRef.current = { bot: botId, seeded: false, open: new Map() };
  // THE SEED WAITS FOR THE EVENTS AND THE SEEN MARK: a bot's first paint can be an empty list (not loaded yet), and a
  // seed burnt on it would leave every stretch of the history open once it lands. "Seen as of opening" is `S.base`
  // (store setNewMark, written in select()'s own batch and by the first poll for a URL-picked bot), not `newMark`:
  // that one is null both before it is placed and when nothing is new, and the two must not read the same.
  const base = S.base[botId];
  if (!stretchRef.current.seeded && evs.length && base !== undefined) {
    // The newest task starts after the second-to-last closer: everything from there on is the one you came back for.
    const closers: number[] = [];
    for (let i = 0; i < evs.length; i++) if ((evs[i].role === "done" || evs[i].role === "error") && !isHandoff(evs[i]) && evs[i].source !== "build" && evs[i].source !== "seed") closers.push(i);
    const newestFrom = closers.length >= 2 ? closers[closers.length - 2] : -1;
    for (const t of folds) {
      const unread = t.hidden.some((j) => evs[j].seq > base);
      stretchRef.current.open.set(t.key, t.hidden[0] > newestFrom || unread);
    }
    stretchRef.current.seeded = true;
  }
  // A stretch first seen after the seed (the task that just settled) is open: only a click closes it.
  const foldAt = new Map<number, { fold: "open" | "closed"; key: number; hint: string; bare: boolean }>();
  const foldHidden = new Set<number>();
  // A session divider on a hidden row (a routine's first step after a night's silence) moves to the seat, so folding
  // the steps never folds the timestamp with them. A bare stretch keeps its own: its first row stays drawn.
  const dayMoved = new Set<number>();
  for (const t of folds) {
    const isOpen = stretchRef.current.open.get(t.key) ?? true;
    const state = isOpen ? "open" as const : "closed" as const, hint = foldHint(t);
    if (t.seat >= 0) foldAt.set(t.seat, { fold: state, key: t.key, hint, bare: false });
    else foldAt.set(t.hidden[0], { fold: state, key: t.key, hint, bare: true });
    if (isOpen || searchQ) continue;
    for (const j of t.hidden) {
      if (t.seat < 0 && j === t.hidden[0]) continue;  // the bare line itself stays
      foldHidden.add(j);
      if (t.seat >= 0 && sessionBreak(evs[j - 1], evs[j])) dayMoved.add(t.seat);
    }
  }
  // The toggle keeps the trigger where it sits: the steps open (or close) above a seated trigger, below a bare one,
  // never under your pointer.
  const onFoldStretch = useCallback((key: number) => {
    const th = threadRef.current; if (!th) return;
    const btn = th.querySelector(`[data-fold="${key}"]`), ev = btn?.closest<HTMLElement>(".ev") || null;
    const seq = ev?.dataset.seq, box = ev ? evBox(ev) : null;
    foldRef.current.anchor = box && seq ? { seq, y: box.getBoundingClientRect().top - th.getBoundingClientRect().top } : null;
    const m = stretchRef.current.open;
    m.set(key, !(m.get(key) ?? true));
    bumpFold((n) => n + 1);
  }, [threadRef]);

  // The search tint lives in the document's highlight registry, not the DOM: drop it when the thread goes.
  useEffect(() => clearSearchHighlight, []);

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
    // jumpTo(): land on one message of this bot (a "Read more" from another bot's chat), once it is in the DOM.
    // The .ev wrapper has no box (display: contents): scroll the message inside it.
    // The page keeps the last EVENT_CAP events only: a card older than the oldest one held is gone from here, so say so
    // (the hand-off line is then the only copy) instead of leaving scrollSeq armed and the thread pinned to the end.
    const js = s.scrollSeq, jumping = !!js && js.bot === botId;
    // A jump into the folded conversation opens it first; the next commit lands on the message.
    if (jumping && folded && js.seq < evs[ci].seq && js.seq >= evs[0].seq) { setOpenFold(foldKey); return; }
    // …and a jump at a step hidden behind its stretch's trigger opens that stretch first, the same way.
    if (jumping) {
      const hit = folds.find((t) => t.hidden.some((j) => foldHidden.has(j) && evs[j].seq === js.seq));
      if (hit) { stretchRef.current.open.set(hit.key, true); bumpFold((n) => n + 1); return; }
    }
    const fa = foldRef.current.anchor;
    foldRef.current.anchor = null;
    const jumpEv = jumping ? th.querySelector<HTMLElement>(`.ev[data-seq="${js.seq}"]`) : null;
    const jumpEl = jumpEv ? evBox(jumpEv) : null;
    if (fa) restoreAnchor(th, [fa]);
    else if (jumpEl) { jumpEl.scrollIntoView({ block: "start" }); clearScrollSeq(); }
    else if (jumping && evs.length && js.seq < evs[0].seq) { clearScrollSeq(); showBanner("That message is older than this chat keeps on the page."); pinToEnd(th, botId || undefined); }
    else if (bumped || s.scrollToEnd || wasAtEnd) pinToEnd(th, botId || undefined);
    // Anchors only ever fall off the top, so losing all of them means you were reading history the cap has now dropped:
    // the oldest message left is the closest thing to what you were looking at, so sit at the top of it.
    else if (rebuilt && !restoreAnchor(th, anchors)) th.scrollTop = 0;
    if (s.scrollToEnd) setScrollToEnd(false);

    // Search: filters the rendered thread in place and re-applies after each append.
    th.classList.toggle("filtering", !!searchQ);
    if (searchQ) {
      let n = 0;
      for (const el of th.children) { const hit = searchHit(el.textContent, searchQ); el.classList.toggle("hit", hit); if (hit) n++; }
      // The matched words themselves light up; the count is of those, or of rows where the browser cannot paint them.
      onSearchCount(searchCountText(highlightSearch(th, searchQ) || n));
    } else { highlightSearch(th, ""); onSearchCount(""); }

    // "Viewed" = a message has scrolled into the thread at least once. The .ev wrapper has no box: watch the message inside.
    const io = viewer.current;
    if (io) th.querySelectorAll<HTMLElement>(".ev[data-seq]:not([data-obs])").forEach((el) => { el.dataset.obs = "1"; const m = evBox(el); if (m) io.observe(m); });
    const c = cur();
    // Folded rows never scroll into view; they are behind the marker, not unread. Steps folded behind a result the
    // same: the seed only ever folds what was already seen, and a click that folds is a reader who has read them.
    if (folded && botId) { const set = viewedSet(botId); for (const e of evs.slice(0, ci)) set.add(e.seq); }
    if (foldHidden.size && botId) { const set = viewedSet(botId); for (const j of foldHidden) set.add(evs[j].seq); }
    if (c) setNewCount(unviewed(c, eventsOf(c.id)).length);
    updateToBottom(th);
    // Sitting at the end with the tab visible means you are following along: keep "seen" current so a reload or re-open
    // does not draw a New line above messages you already watched arrive. In Stage the thread stays beside the page,
    // except in the narrow fallback (.sfull), where the live view covers it: nothing is seen then.
    const s2 = getState();
    if (c && !document.hidden && s2.pinned && !(s2.fast && botsRoot().classList.contains("sfull"))) markSeen(c.id, c.seq);
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
    const jump = t.closest<HTMLElement>("[data-jump-bot]");
    if (jump) { jumpTo(jump.dataset.jumpBot || "", Number(jump.dataset.jumpSeq)); return; }
    const fold = t.closest("[data-fold]");
    if (fold) { onFoldStretch(Number(fold.getAttribute("data-fold"))); return; }
    const rc = t.closest("[data-react]");
    if (rc) { onReact(rc, Number(rc.getAttribute("data-react"))); return; }
    const dlg = t.closest("[data-dialog]");
    if (dlg && sel) { openDialog({ kind: "settings", id: sel, tab: dlg.getAttribute("data-dialog") || "general" }); return; }
    const rp = t.closest("[data-reply]");
    if (rp) {
      const seq = Number(rp.getAttribute("data-reply"));
      const e = eventsOf(sel).find((x) => x.seq === seq);
      if (e) onReply({ seq, text: (e.text || "").replace(/\s+/g, " ").trim() });
      return;
    }
    const opt = t.closest(".msg.question:not(.settled) .opt");
    if (opt && opt.hasAttribute("data-mopt")) return;  // a multi-select row: MultiQuestion handles its own clicks
    if (opt && sel) { const text = opt.getAttribute("data-opt") || ""; settleNow(opt, () => act(() => api.send(sel, text))); return; }
    const ok = t.closest(".msg.approval:not(.settled) [data-approve]"), no = t.closest(".msg.approval:not(.settled) [data-deny]");
    const fv = t.closest(".msg.approval:not(.settled) [data-forever]");
    if ((!ok && !no && !fv) || !sel) return;
    // The forever button sends readable words (the thread shows what you did) plus the flag the gate reads.
    settleNow((ok || no || fv)!, () => act(() => (fv ? api.send(sel, "Approve, don't ask again", null, true) : api.send(sel, ok ? "approve" : "deny"))));
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
    // No events (none yet, or not loaded) while you hold the browser: the note still carries the hand back.
    content = b.control ? [<HandoverNote key="handover" b={b} />] : <div className="empty">Say hello: give this bot a task.</div>;
  } else {
    const keys = rowKeys(evs, botId);
    // D12: delivery rows join the bubble they refer to (`ref`); they render nothing of their own (isNoise).
    const texted = new Map<number, BotEvent[]>();
    for (const e of evs) if (e.role === "delivery" && e.ref != null) texted.set(e.ref, [...(texted.get(e.ref) || []), e]);
    // Hand-off cards: the "Asked <Bot>" card follows its hand-off's state live; the waiting / result cards keep their own.
    const hds = new Map((b.handoffs || []).map((h) => [h.id, h.state]));
    const botIds = new Set(S.bots.map((x) => x.id));
    const hb = b.handback || null;
    // While you hold the browser, the hand-back row ends the bot's live hand-over question; failing that (a take-over of
    // yours, or the question is not loaded), a note at the end of the thread carries it.
    const hoSeq = b.control && b.control_by === "bot"
      && evs.some((e) => e.seq === b.waiting_on && e.role === "question" && !e.offer && !(e.multi && e.options?.length)) ? b.waiting_on : null;
    const heldSeqs = new Set((b.held || []).map((h) => h.seq));
    content = evs.map((e, i) => {
      const ho = e.handoff && isHandoff(e) ? e.handoff : null;
      const card = !ho && (e.role === "approval" || e.role === "question");
      return (
        <Row key={keys[i]} e={e} botId={botId} day={sessionBreak(evs[i - 1], e) || dayMoved.has(i)} isNew={i === firstNew} texted={texted.get(e.seq)}
          reaction={!ho && ACTABLE.has(e.role) ? rxs[e.seq] || "" : ""} live={card && live.has(e.seq) && !held.has(e.seq)}
          chosen={card && e.role === "question" ? chosenOption(evs, e.seq) : null} appsRoot={appsRoot} onBeside={onBeside}
          hstate={ho ? (e.role === "system" ? hds.get(ho.id) : undefined) || ho.state : undefined} targetLive={ho ? botIds.has(ho.target) : undefined}
          linkLive={e.link ? botIds.has(e.link.bot) : undefined} bopen={e.build ? buildOpen(evs, e) : undefined}
          hbSecs={hb && e.seq === hb.seq && e.role === "question" ? hb.secs : undefined}
          hbShot={hb && e.seq === hb.seq && e.role === "question" ? hb.shot : undefined}
          hbBot={hoSeq != null && e.seq === hoSeq ? b : undefined} queued={e.role === "user" && heldSeqs.has(e.seq)}
          folded={(folded && i < ci) || foldHidden.has(i)} foldN={i === ci ? foldN : undefined} foldOpen={i === ci ? !folded : undefined} onFold={i === ci ? onFold : undefined}
          fold={foldAt.get(i)?.fold} foldKey={foldAt.get(i)?.key} foldHint={foldAt.get(i)?.hint} bare={foldAt.get(i)?.bare} />
      );
    });
    if (b.control && hoSeq == null) content.push(<HandoverNote key="handover" b={b} />);
  }

  return (
    <div className="thread" id="thread" ref={threadRef} data-bot={botId || undefined}
      onScroll={() => updateToBottom(threadRef.current)} onClick={onClick}>
      {content}
    </div>
  );
}
