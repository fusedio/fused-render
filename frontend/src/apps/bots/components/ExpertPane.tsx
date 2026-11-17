// An expert's preview column (docs/experts-autonomous.md "UI"): no browser, so in place of the screenshot, live view
// and info sections it shows what the expert did and knows, on three tabs in the Browser | Terminal strip's style:
//   Activity  (default) everything it did by itself, newest first under day dividers, each a card: what it did, when,
//             the summary and why; opened: what it rests on (your words, where it came from, the file). A change that
//             can be taken back has Undo; one taken back says "undone"; one that failed shows its error. On top: how
//             many things wait to be learned, "Learn now" (its step while it runs), filter chips All / Skills / Memory
//             and "Show everything" (the quiet lines too). Opening the tab marks the activity as seen.
//   Memory    the budget line on top ("32 of 50 lessons", a meter, what tidying does, the last tidy), then the lessons
//             it keeps (most used first), each a card: In use / Not used yet / Contradicted, where it came from, how
//             often used; opened: earlier wording, the lessons it was merged from, what it is about, sessions / answers
//   Consults  the questions agent sessions asked it (newest first, under day dividers, filterable by outcome), each a
//             card (status, source, when, the question, small facts) opening to the context, the answer as markdown,
//             the files (click copies the path) and the lessons it used
// All three tabs draw the same card (XCard); its detail renders only when the card is open.
// Settings' "Expert" tab mounts the same panels (ExpertPanels) so the phone, which hides this column, reaches them.
// Data: /api/experts/<id>/… (lib/api.ts expertApi), fetched for the open tab on a bot change, again when the bot's
// `seq` moves (a chat turn may have taught it something) and every 10 s while the page is visible; the activity (which
// carries the learning run's progress) with every refresh whichever tab is open, and every 5 s while it learns. A
// failed call shows the page banner once (not again on every refresh while it keeps failing).
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { expertApi, type Bot, type ExpertActivity, type ExpertBudget, type ExpertConsult, type ExpertItem, type ExpertLearning, type ExpertMemory } from "../lib/api";
import { actionLabel, actionTone, activityGroup, basename, budgetState, canUndo, CONSULT_STATUS_LABEL, consultFacts, consultSource, consultStatus, evidenceParts,
  filterActivity, filterConsults, filterLessons, groupByDay, groupConsultsByDay, isLongContext, learningLine, LESSON_STATUS_LABEL, lessonFacts, lessonSource,
  lessonStatus, shownActivity, sortLessons, tidySummary, undoError, usedLabel, usedLessons, usesLine, waitingLine,
  type ActivityFilter, type ConsultFilter, type ConsultStatus, type LessonFilter, type LessonStatus } from "../lib/expert";
import { setExpertUnread } from "../lib/expertUnread";
import { fmtAgo, fmtWhen } from "../lib/format";
import { md } from "../lib/md";
import { errMsg, getState, hideBanner, showBanner } from "../state/store";
import { isLanHost } from "@platform/lib/host";

export type ExpertTab = "activity" | "memory" | "consults";
const TABS: [ExpertTab, string][] = [["activity", "Activity"], ["memory", "Memory"], ["consults", "Consults"]];
const REFRESH_MS = 10_000, WORK_MS = 5_000, PAGE = 100;

/** The memory's budget fields (all absent on a server that sends none: no budget line). */
export type MemoryBudget = ExpertBudget;

interface Data {
  memory: ExpertItem[] | null;
  budget: MemoryBudget | null;
  consults: ExpertConsult[] | null;
  /** The activity lines loaded so far (newest first), whether older ones may remain, and what rode with them. */
  activity: ExpertActivity[] | null;
  more: boolean;
  waiting: number;
  learning: ExpertLearning | null;
}
const EMPTY: Data = { memory: null, budget: null, consults: null, activity: null, more: false, waiting: 0, learning: null };

/** The memory route's reply as Data: the lessons and the budget (`budget`, else the same fields beside `items`). */
export const fromMemory = (m: ExpertMemory): Pick<Data, "memory" | "budget"> => {
  const b = m.budget || m;
  return { memory: m.items || [], budget: { limit: b.limit, tidy_at: b.tidy_at, total: b.total ?? (m.items || []).length, last_tidy: b.last_tidy ?? null } };
};

/** A fresh first page laid over the lines already loaded: the page first, then the older lines it does not hold. */
export function mergeActivity(page: readonly ExpertActivity[], had: readonly ExpertActivity[] | null): ExpertActivity[] {
  const ids = new Set(page.map((a) => a.id)), oldest = page.length ? page[page.length - 1].ts : Infinity;
  return [...page, ...(had || []).filter((a) => !ids.has(a.id) && a.ts <= oldest)];
}

/** The open tab's data for expert `id`, kept fresh (see the header). `everything`: the quiet lines too. */
function useExpertData(id: string, seq: number, tab: ExpertTab, everything: boolean) {
  const [data, setData] = useState<Data>(EMPTY);
  const lastErr = useRef("");
  const live = useRef(id);
  live.current = id;
  const tabRef = useRef(tab);
  tabRef.current = tab;
  // A failed fetch shows the banner once per distinct error; a success after it takes the banner down again.
  const guard = useCallback(async <T,>(call: () => Promise<T>, loud = false): Promise<T | undefined> => {
    try {
      const r = await call();
      // Take the banner down only if it still shows this pane's error (never another one's).
      if (lastErr.current) { if (getState().banner.text === lastErr.current) hideBanner(); lastErr.current = ""; }
      return r;
    } catch (e) {
      const m = errMsg(e);
      if (loud || m !== lastErr.current) showBanner(m);
      lastErr.current = m;
      return undefined;
    }
  }, []);

  /** The newest page of the activity. With the Activity tab open it is seen at once: the server is told, and the dot
   *  on the expert's row goes. */
  const loadActivity = useCallback(async (loud = false) => {
    const want = id;
    const r = await guard(() => expertApi.activity(want, { limit: PAGE, quiet: everything }), loud);
    if (live.current !== want || !r) return;
    const page = Array.isArray(r.items) ? r.items : [];
    setData((d) => ({ ...d, activity: mergeActivity(page, d.activity), more: d.activity && d.activity.length > page.length ? d.more : page.length >= PAGE,
      waiting: r.waiting || 0, learning: r.learning || null }));
    const unread = r.unread || 0;
    if (tabRef.current === "activity" && !document.hidden) {
      setExpertUnread(want, 0);
      if (unread > 0) void expertApi.seen(want).catch(() => {});
    } else setExpertUnread(want, unread);
  }, [id, guard, everything]);

  const loadMore = useCallback(async (before: string) => {
    const want = id;
    const r = await guard(() => expertApi.activity(want, { limit: PAGE, before, quiet: everything }), true);
    if (live.current !== want || !r) return;
    const page = Array.isArray(r.items) ? r.items : [];
    setData((d) => {
      const ids = new Set((d.activity || []).map((a) => a.id)), fresh = page.filter((a) => !ids.has(a.id));
      return { ...d, activity: [...(d.activity || []), ...fresh], more: fresh.length > 0 && page.length >= PAGE };
    });
  }, [id, guard, everything]);

  const load = useCallback(async (which: ExpertTab, loud = false) => {
    const want = id;
    if (which === "activity") { await loadActivity(loud); return; }
    // The Consults tab reads the memory too: an opened consult names the lessons it used by their claims.
    const [m, c] = await Promise.all([guard(() => expertApi.memory(want), loud), which === "consults" ? guard(() => expertApi.consults(want), loud) : undefined]);
    if (live.current !== want) return;
    setData((d) => ({ ...d, ...(m ? fromMemory(m) : {}), ...(c ? { consults: c.consults || [] } : {}) }));
  }, [id, guard, loadActivity]);
  /** The open tab, plus (off the Activity tab) the activity, so a learning run is noticed whichever tab is open. */
  const refresh = useCallback((which: ExpertTab) => {
    void load(which);
    if (which !== "activity") void loadActivity();
  }, [load, loadActivity]);

  // A new bot starts empty (never another expert's rows under this one's name).
  useEffect(() => { setData(EMPTY); lastErr.current = ""; }, [id]);
  // "Show everything" changes which lines the server sends: start the list again.
  useEffect(() => { setData((d) => ({ ...d, activity: null, more: false })); }, [everything]);
  useEffect(() => { refresh(tab); }, [refresh, tab, seq]);
  useEffect(() => {
    const t = window.setInterval(() => { if (!document.hidden) refresh(tab); }, REFRESH_MS);
    return () => window.clearInterval(t);
  }, [refresh, tab]);
  // Learning in the background: follow it until it is done; when it ends, the open tab refetches (new lessons).
  const working = !!data.learning?.running;
  const wasWorking = useRef(false);
  const latest = useRef({ refresh, tab });
  latest.current = { refresh, tab };
  useEffect(() => {
    if (wasWorking.current && !working) latest.current.refresh(latest.current.tab);
    wasWorking.current = working;
    if (!working) return;
    const t = window.setInterval(() => { if (!document.hidden) void loadActivity(); }, WORK_MS);
    return () => window.clearInterval(t);
  }, [working, loadActivity]);

  return { data, setData, reload: load, loadMore, guard };
}

// ------------------------------------------------------------------ panels ----

const CHEVRON = (
  <svg className="xp-chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="m6 9 6 6 6-6" />
  </svg>
);

export type CardTone = "ok" | "warn" | "muted" | "accent";

/** The one card all three tabs use: a header button (status marker, source, time or count, a chevron that turns
 *  when open; `aria-expanded`), the text clamped to two lines while closed, a footer of small facts, and `detail()`
 *  rendered only while open. `note`: a muted line under the facts. `foot`: a row under the header that shows open or
 *  closed (it holds what a header button cannot: the Activity card's Undo). */
export function XCard({ tone, mark, source, sourceTitle, right, rightTitle, text, facts, note, initialOpen = false, className = "", detail, foot }: {
  tone: CardTone; mark: string; source: string; sourceTitle?: string; right: string; rightTitle?: string; text: string; facts: string[];
  note?: string; initialOpen?: boolean; className?: string; detail: () => ReactNode; foot?: ReactNode;
}) {
  const [open, setOpen] = useState(initialOpen);
  const bodyId = useId();
  return (
    <li className={`xp-card${open ? " open" : ""}${className ? ` ${className}` : ""}`}>
      <button type="button" className="xp-card-head" aria-expanded={open} aria-controls={open ? bodyId : undefined} onClick={() => setOpen((o) => !o)}>
        <span className="xp-card-line">
          <span className={`xp-cstat tone-${tone}`}><span className="xp-dot" aria-hidden="true" />{mark}</span>
          {source ? <span className="xp-src" title={sourceTitle || undefined}>{source}</span> : null}
          {right ? <span className="xp-when" title={rightTitle || undefined}>{right}</span> : null}
          {CHEVRON}
        </span>
        <span className="xp-q">{text}</span>
        {facts.length ? <span className="xp-facts">{facts.join(" · ")}</span> : null}
        {note ? <span className="xp-facts xp-note">{note}</span> : null}
      </button>
      {foot ? <div className="xp-card-foot">{foot}</div> : null}
      {open ? <div className="xp-card-body" id={bodyId}>{detail()}</div> : null}
    </li>
  );
}

/** A labelled part of an opened card. */
function Part({ label, children }: { label: string; children: ReactNode }) {
  return <section className="xp-part"><h5 className="xp-lbl">{label}</h5>{children}</section>;
}

/** Markdown through md() (the chat's renderer and its sanitiser). */
function Md({ text }: { text: string }) {
  return text ? <div className="xp-md" dangerouslySetInnerHTML={{ __html: md(text) }} /> : <p className="xp-none">(no answer)</p>;
}

/** A file chip: the ref's basename (full path as the title); a click copies the full path and says "Copied" briefly. */
function FileChip({ path }: { path: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const onClick = () => {
    try {
      navigator.clipboard.writeText(path).then(() => {
        setCopied(true);
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setCopied(false), 1400);
      }).catch(() => {});
    } catch { /* no clipboard here (plain-http LAN page): the title still shows the path */ }
  };
  return (
    <button type="button" className={`xp-file${copied ? " copied" : ""}`} title={`${path}\nClick to copy the full path`} onClick={onClick}>
      {copied ? "Copied" : basename(path) || path}
    </button>
  );
}

function Files({ refs }: { refs: readonly string[] }) {
  return <div className="xp-files">{refs.map((r) => <FileChip key={r} path={r} />)}</div>;
}

/** "All N" · … filter chips; hidden under 2 items. A chip with nothing to show is off (unless it is the one on). */
function Filters<F extends string>({ label, options, counts, value, onChange, total }: {
  label: string; options: [F, string][]; counts: Record<F, number>; value: F; onChange: (f: F) => void; total: number;
}) {
  if (total < 2) return null;
  return (
    <div className="xp-filters" role="group" aria-label={label}>
      {options.map(([f, l], i) => (
        <button key={f} type="button" className={`xp-chip${value === f ? " on" : ""}`} aria-pressed={value === f}
          disabled={i > 0 && !counts[f] && value !== f} onClick={() => onChange(f)}>
          {l} <span className="xp-n">{counts[f]}</span>
        </button>
      ))}
    </div>
  );
}

// ---- Memory ----

const LESSON_TONE: Record<LessonStatus, CardTone> = { "in-use": "ok", "not-used": "muted", contradicted: "warn" };

export function LessonCard({ it, initialOpen = false, nowMs }: { it: ExpertItem; initialOpen?: boolean; nowMs?: number }) {
  const st = lessonStatus(it);
  // Last used: when an answer last relied on it (a newer server), else when it was last seen.
  const used = typeof it.last_used === "number" && it.last_used > 0 ? it.last_used : 0;
  const when = [it.created ? `first seen ${fmtAgo(it.created, nowMs)}` : "",
    used ? `last used ${fmtAgo(used, nowMs)}` : it.updated ? `last seen ${fmtAgo(it.updated, nowMs)}` : ""].filter(Boolean).join(" · ");
  return (
    <XCard tone={LESSON_TONE[st]} mark={LESSON_STATUS_LABEL[st]} source={lessonSource(it)} right={usedLabel(it)}
      rightTitle={used ? `last used ${fmtWhen(used)}` : it.updated ? `last seen ${fmtWhen(it.updated)}` : undefined} text={it.claim} facts={lessonFacts(it)}
      initialOpen={initialOpen}
      detail={() => (
        <>
          {Array.isArray(it.history) && it.history.length ? (
            <Part label="Earlier wording">
              <ul className="xp-used xp-merged">{it.history.map((h, i) => <li key={i}>{h.claim}</li>)}</ul>
            </Part>
          ) : null}
          {Array.isArray(it.merged_from) && it.merged_from.length ? (
            <Part label="Merged from">
              <ul className="xp-used xp-merged">{it.merged_from.map((m, i) => <li key={m.id || i}>{m.claim}</li>)}</ul>
            </Part>
          ) : null}
          {it.about?.length ? <Part label="About"><div className="xp-files">{it.about.map((a) => <span key={a} className="xp-about-chip">{a}</span>)}</div></Part> : null}
          <div>
            <p className="xp-facts">{usesLine(it)}</p>
            {when ? <p className="xp-facts">{when}</p> : null}
          </div>
        </>
      )} />
  );
}

const LESSON_FILTERS: [LessonFilter, string][] = [["all", "All"], ["in-use", "In use"], ["not-used", "Not used yet"]];

/** "32 of 50 lessons", a thin meter (neutral up to `tidy_at`, warning beyond it), what tidying does at this size, and
 *  a recent tidy's summary. Nothing without a `limit` (an older server). */
export function MemoryBudgetLine({ budget, listed, nowMs }: { budget: MemoryBudget | null | undefined; listed: number; nowMs?: number }) {
  const b = budgetState(budget, listed);
  if (!b) return null;
  const tidy = tidySummary(budget?.last_tidy, nowMs);
  return (
    <div className={`xp-budget${b.above ? " above" : ""}`}>
      <div className="xp-budget-head"><span className="xp-budget-n">{b.label}</span></div>
      <div className="xp-meter" role="meter" aria-label="Lessons in memory" aria-valuemin={0} aria-valuemax={b.limit} aria-valuenow={Math.min(b.total, b.limit)}
        aria-valuetext={b.label}>
        <span className="xp-meter-fill" style={{ width: `${b.fill}%` }} />
        {b.over ? <span className="xp-meter-over" style={{ width: `${b.over}%` }} /> : null}
        {b.tidyAt < b.limit ? <span className="xp-meter-tick" style={{ left: `${(b.tidyAt / Math.max(b.limit, b.total)) * 100}%` }} aria-hidden="true" /> : null}
      </div>
      <p className="xp-facts">{b.note}</p>
      {tidy ? <p className="xp-facts xp-tidy">{tidy}</p> : null}
    </div>
  );
}

export function MemoryPanel({ items, budget = null, nowMs }: {
  items: ExpertItem[] | null;
  /** The memory route's budget (the line on top). */
  budget?: MemoryBudget | null; nowMs?: number;
}) {
  const [filter, setFilter] = useState<LessonFilter>("all");
  if (items == null) return <div className="xp-empty">Loading…</div>;
  const sorted = sortLessons(items), shown = filterLessons(sorted, filter);
  const counts = { all: items.length, "in-use": filterLessons(items, "in-use").length, "not-used": filterLessons(items, "not-used").length };
  return (
    <>
      <MemoryBudgetLine budget={budget} listed={items.length} nowMs={nowMs} />
      {items.length ? (
        <>
          <Filters label="Show lessons" options={LESSON_FILTERS} counts={counts} value={filter} onChange={setFilter} total={items.length} />
          {shown.length ? <ul className="xp-cards">{shown.map((it) => <LessonCard key={it.id} it={it} nowMs={nowMs} />)}</ul>
            : <div className="xp-empty">No lessons match this filter.</div>}
        </>
      ) : (
        <div className="xp-empty">No lessons yet. What you teach it in chat, and what it learns from the sessions that ask it questions, appears here. To make it forget something, tell it in the chat.</div>
      )}
    </>
  );
}

// ---- Consults ----

const CONSULT_TONE: Record<ConsultStatus, CardTone> = { answered: "ok", unsure: "warn" };

/** One consult: status, source, when; the question; small facts. Opened: the session's context, the answer as markdown,
 *  files, lessons used. Closed, it shows none of the answer. */
export function ConsultCard({ c, items, initialOpen = false, nowMs }: {
  c: ExpertConsult; items: ExpertItem[] | null; initialOpen?: boolean; nowMs?: number;
}) {
  const [allCtx, setAllCtx] = useState(false);
  const status = consultStatus(c), longCtx = isLongContext(c.context);
  return (
    <XCard tone={CONSULT_TONE[status]} mark={CONSULT_STATUS_LABEL[status]} className={status} source={consultSource(c)} sourceTitle={c.caller?.target}
      right={fmtAgo(c.ts, nowMs)} rightTitle={fmtWhen(c.ts)} text={c.question} facts={consultFacts(c)} initialOpen={initialOpen}
      detail={() => (
        <>
          {c.context ? (
            <Part label="Context from the session">
              <p className={`xp-ctx${longCtx && !allCtx ? " clamp" : ""}`}>{c.context}</p>
              {longCtx ? <button type="button" className="xp-more" aria-expanded={allCtx} onClick={() => setAllCtx((v) => !v)}>{allCtx ? "Show less" : "Show all"}</button> : null}
            </Part>
          ) : null}
          <Part label="Answer"><Md text={c.answer} /></Part>
          {c.refs?.length ? <Part label="Files"><Files refs={c.refs} /></Part> : null}
          {c.used?.length ? (
            <Part label="Lessons used">
              <ul className="xp-used">{usedLessons(c.used, items).map((l) => <li key={l.id}>{l.text}</li>)}</ul>
            </Part>
          ) : null}
        </>
      )} />
  );
}

const CONSULT_FILTERS: [ConsultFilter, string][] = [["all", "All"], ["answered", "Answered"], ["unsure", "Unsure"]];

export function ConsultsPanel({ consults, items = null, nowMs }: { consults: ExpertConsult[] | null; items?: ExpertItem[] | null; nowMs?: number }) {
  const [filter, setFilter] = useState<ConsultFilter>("all");
  if (consults == null) return <div className="xp-empty">Loading…</div>;
  if (!consults.length) return <div className="xp-empty">No consults yet. When an agent session asks this expert a question, the question and its answer appear here.</div>;
  const counts = { all: consults.length, answered: filterConsults(consults, "answered").length, unsure: filterConsults(consults, "unsure").length };
  const groups = groupConsultsByDay(filterConsults(consults, filter), nowMs);
  return (
    <>
      <Filters label="Show consults" options={CONSULT_FILTERS} counts={counts} value={filter} onChange={setFilter} total={consults.length} />
      {groups.length ? groups.map((g) => (
        <section key={g.key} className="xp-dayg" aria-label={g.label}>
          <h4 className="xp-day">{g.label}</h4>
          <ul className="xp-cards">{g.items.map((c) => <ConsultCard key={c.id} c={c} items={items} nowMs={nowMs} />)}</ul>
        </section>
      )) : <div className="xp-empty">No consults match this filter.</div>}
    </>
  );
}

// ---- Activity ----

/** One thing the expert did: what, when, the summary, why. Under the header: Undo (a change that can be taken back),
 *  "undone", or the error of a change that failed; `undoError`: why this line's Undo was refused. Opened: what it
 *  rests on. `onUndo` absent (the phone): no button. */
export function ActivityCard({ a, onUndo, undoing = false, undoError: refused = "", initialOpen = false, nowMs }: {
  a: ExpertActivity; onUndo?: (a: ExpertActivity) => void; undoing?: boolean; undoError?: string; initialOpen?: boolean; nowMs?: number;
}) {
  const ev = evidenceParts(a.evidence), failed = a.ok === false, undo = !!onUndo && canUndo(a);
  const foot = undo || a.undone || failed || refused ? (
    <>
      <span className="xp-foot-msg">
        {a.undone ? <span className="xp-undone">undone</span> : null}
        {failed ? <span className="xp-err">{a.error ? `This did not work: ${a.error}` : "This did not work."}</span> : null}
        {refused ? <span className="xp-err" role="alert">{refused}</span> : null}
      </span>
      {undo ? <button type="button" className={`xp-undo${undoing ? " busy" : ""}`} disabled={undoing} title="Put this back the way it was" onClick={() => onUndo!(a)}>Undo</button> : null}
    </>
  ) : null;
  return (
    <XCard tone={actionTone(a)} mark={actionLabel(a.action)} className={`act-${activityGroup(a) || "note"}${a.undone ? " undone" : ""}${failed ? " failed" : ""}`}
      source={a.target || ""} right={fmtAgo(a.ts, nowMs)} rightTitle={a.ts ? fmtWhen(a.ts) : undefined} text={a.summary} facts={a.why ? [a.why] : []}
      initialOpen={initialOpen} foot={foot}
      detail={() => (
        <>
          {ev.quote ? <Part label="You said"><p className="xp-txt">“{ev.quote}”</p></Part> : null}
          {ev.from ? <Part label="Came from"><p className="xp-txt" title={ev.fromTitle || undefined}>{ev.from}</p></Part> : null}
          {ev.file ? <Part label="Read in"><Files refs={[ev.file]} /></Part> : null}
          {!ev.quote && !ev.from && !ev.file ? <p className="xp-facts">Nothing more is recorded for this.</p> : null}
        </>
      )} />
  );
}

const ACTIVITY_FILTERS: [ActivityFilter, string][] = [["all", "All"], ["skills", "Skills"], ["memory", "Memory"]];

export function ActivityPanel({ items, waiting = 0, learning = null, everything = false, onEverything, onLearn, onUndo, onMore, more = false, busy = false,
  undoing = null, undoErrors = {}, lan = false, off = false, nowMs }: {
  items: ExpertActivity[] | null; waiting?: number; learning?: ExpertLearning | null;
  /** "Show everything": the quiet lines too. */
  everything?: boolean; onEverything: (on: boolean) => void;
  onLearn: () => void; onUndo: (a: ExpertActivity) => void;
  /** Older lines may remain: "Show earlier" asks for them. */
  more?: boolean; onMore?: () => void;
  busy?: boolean; undoing?: string | null; undoErrors?: Record<string, string>;
  /** On the phone: nothing that changes the expert (no Learn now, no Undo). `off`: learning is switched off in Settings. */
  lan?: boolean; off?: boolean; nowMs?: number;
}) {
  const [filter, setFilter] = useState<ActivityFilter>("all");
  if (items == null) return <div className="xp-empty">Loading…</div>;
  const all = shownActivity(items, everything), shown = filterActivity(all, filter), running = !!learning?.running, on = busy || running;
  const counts = { all: all.length, skills: filterActivity(all, "skills").length, memory: filterActivity(all, "memory").length };
  const wait = waitingLine(waiting);
  return (
    <>
      <div className="xp-bar">
        <span className="xp-line">
          {running ? <span className="xp-status">{learningLine(learning)}</span>
            : off ? "Learning is off. It still answers and notes what happens, and changes nothing."
            : wait || "Nothing is waiting to be learned."}
          {running && wait ? <span className="xp-wait">{wait}</span> : null}
        </span>
        {lan || off ? null : (
          <button type="button" className={on ? "busy" : undefined} disabled={on} onClick={onLearn}
            title={running ? "It is learning now" : "Learn from what is waiting now, and tidy memory"}>{on ? "Learning…" : "Learn now"}</button>
        )}
      </div>
      <div className="xp-toolbar">
        <Filters label="Show activity" options={ACTIVITY_FILTERS} counts={counts} value={filter} onChange={setFilter} total={all.length} />
        <button type="button" className={`xp-chip xp-every${everything ? " on" : ""}`} aria-pressed={everything}
          title="Also show the small things: what it noticed and what it saw again" onClick={() => onEverything(!everything)}>Show everything</button>
      </div>
      {!all.length ? <div className="xp-empty">Nothing yet. Everything this expert learns, changes in its skills or looks into by itself is listed here, and you can undo it.</div>
        : !shown.length ? <div className="xp-empty">Nothing matches this filter.</div>
        : groupByDay(shown, nowMs).map((g) => (
          <section key={g.key} className="xp-dayg" aria-label={g.label}>
            <h4 className="xp-day">{g.label}</h4>
            <ul className="xp-cards">
              {g.items.map((a) => <ActivityCard key={a.id} a={a} onUndo={lan ? undefined : onUndo} undoing={undoing === a.id} undoError={undoErrors[a.id] || ""} nowMs={nowMs} />)}
            </ul>
          </section>
        ))}
      {more && onMore && all.length ? <div className="xp-acts xp-earlier"><button type="button" onClick={onMore}>Show earlier</button></div> : null}
    </>
  );
}

// ------------------------------------------------------------------ tabs + data ----

/** The tab strip and the open panel, with their data. */
export function ExpertPanels({ b }: { b: Bot }) {
  const [tab, setTab] = useState<ExpertTab>("activity");
  const [everything, setEverything] = useState(false);
  const { data, setData, reload, loadMore, guard } = useExpertData(b.id, b.seq, tab, everything);
  const [starting, setStarting] = useState(false);
  const [undoing, setUndoing] = useState<string | null>(null);
  const [undoErrors, setUndoErrors] = useState<Record<string, string>>({});
  const id = b.id, uid = useId();  // Settings may mount these panels while the preview column shows them too

  // Opening the Activity tab marks what is there as seen (the dot on the expert's row goes). Silent: a nicety.
  useEffect(() => {
    if (tab !== "activity") return;
    setExpertUnread(id, 0);
    void expertApi.seen(id).catch(() => {});
  }, [id, tab]);

  // Learning answers at once and runs in the background (the activity's `learning` follows it, polled while it runs),
  // so the button is busy only for the request itself.
  const onLearn = async () => {
    setStarting(true);
    try {
      const r = await guard(() => expertApi.learn(id), true);
      if (r?.started) setData((d) => ({ ...d, learning: d.learning?.running ? d.learning : { running: true, stage: "" } }));
    } finally { setStarting(false); }
    void reload("activity");
  };
  // A refusal (409: the skill changed since, undo the later change first) stays on that card, never the page banner.
  const onUndo = async (a: ExpertActivity) => {
    setUndoing(a.id);
    setUndoErrors(({ [a.id]: _gone, ...rest }) => rest);
    try {
      await expertApi.undo(id, a.id);
      setData((d) => ({ ...d, activity: (d.activity || []).map((x) => (x.id === a.id ? { ...x, undone: true } : x)) }));
    } catch (e) {
      setUndoErrors((m) => ({ ...m, [a.id]: undoError(e) }));
    } finally { setUndoing(null); }
    void reload("activity");
  };
  const onMore = () => {
    const last = data.activity?.[data.activity.length - 1];
    if (last) void loadMore(last.id);
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const i = TABS.findIndex(([t]) => t === tab), next = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length][0];
    setTab(next);
    document.getElementById(`${uid}-${next}`)?.focus();
  };

  return (
    <div className="xpanels">
      <div className="viewsw xp-tabs" role="tablist" aria-label="Expert" onKeyDown={onKey}>
        {TABS.map(([t, l]) => (
          <button key={t} type="button" id={`${uid}-${t}`} role="tab" className={`satab${tab === t ? " on" : ""}`} aria-selected={tab === t}
            aria-controls={`${uid}-panel`} tabIndex={tab === t ? 0 : -1} onClick={() => setTab(t)}>{l}</button>
        ))}
      </div>
      <div className="xp-panel" id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-${tab}`} data-tab={tab}>
        {tab === "activity" ? <ActivityPanel items={data.activity} waiting={data.waiting} learning={data.learning} everything={everything} onEverything={setEverything}
          more={data.more} onMore={onMore} busy={starting} undoing={undoing} undoErrors={undoErrors} lan={isLanHost()} off={b.expert?.learning === false}
          onLearn={() => { void onLearn(); }} onUndo={(a) => { void onUndo(a); }} /> : null}
        {tab === "memory" ? <MemoryPanel items={data.memory} budget={data.budget} /> : null}
        {tab === "consults" ? <ConsultsPanel consults={data.consults} items={data.memory} /> : null}
      </div>
    </div>
  );
}

/** The preview column's body for an expert: what it is about, whether it can answer, then the panels. */
export function ExpertPane({ b }: { b: Bot }) {
  const about = b.expert?.ask_me_about || "";
  return (
    <div className="xpane" id="xpane">
      {about ? <p className="xp-about" title="What this expert answers">{about}</p> : null}
      {b.expert_available === false ? <p className="xp-warn">{b.expert_reason || "No engine is available for this expert."}</p> : null}
      <ExpertPanels key={b.id} b={b} />
    </div>
  );
}
