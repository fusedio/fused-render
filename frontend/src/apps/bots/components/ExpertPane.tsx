// An expert's preview column (docs/experts.md §10): no browser, so in place of the screenshot, live view and info
// sections it shows what the expert knows and how it is doing, on four tabs in the Browser | Terminal strip's style:
//   Memory       the budget line on top ("32 of 50 lessons", a meter, what tidying does, the last tidy), then the
//                lessons it keeps (most used first), each a card: In use / Not used yet / Contradicted, where it came
//                from, how often used (and "merged from N"); opened: the lessons it was merged from, what it is about, sessions seen in / answers relied on, first seen /
//                last used, and Forget. The skill proposals in progress above them; filter chips by status; at the
//                bottom a collapsed "Recently removed" list (the archive: what left memory and why)
//   Observations everything it noted from consults and sessions: "Waiting" (oldest first) and "Consolidated" (newest
//                first, under day dividers, each with what came of it); "Consolidate now", its live stage line and the
//                proposal status lines on top; an unsure row opens to what research found and can be dropped while it is
//                unresolved; a lesson row opens to the lesson it landed on; filter chips Unsure / Learnings
//   Consults     the questions agent sessions asked it (newest first, under day dividers, filterable by outcome), each a
//                card (status, source, when, the question, small facts) opening to the context, the answer as markdown,
//                the files (click copies the path) and the lessons it used
//   Checks       its fixed questions (questions.md) graded on the last run; "Run checks" starts one and shows it move
// All three list tabs draw the same card (XCard); its detail renders only when the card is open.
// Settings' "Expert" tab mounts the same panels (ExpertPanels) so the phone, which hides this column, reaches them.
// Data: /api/experts/<id>/… (lib/api.ts expertApi), fetched for the open tab on a bot change, again when the bot's
// `seq` moves (a chat turn may have taught it something) and every 10 s while the page is visible; the observations
// (which carry a consolidation's progress) with every refresh whichever tab is open, and every 5 s while a
// consolidation runs or a gap is being researched; the checks every 2 s while a run is going; the proposals (shown on
// the Memory and Observations tabs) every 5 s while one is still being prepared. A failed call shows the page banner once (not again on
// every refresh while it keeps failing).
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { expertApi, type Bot, type ExpertArchived, type ExpertChecks, type ExpertConsolidating, type ExpertConsult, type ExpertGap, type ExpertItem,
  type ExpertMemory, type ExpertObs, type ExpertProposal } from "../lib/api";
import { anyPreparing, basename, budgetState, checkProgress, consolidatingLine, CONSULT_STATUS_LABEL, consultFacts, consultSource, consultStatus, filterConsults,
  filterLessons, filterObservations, gapNote, gapProposal, gapUnresolved, groupByDay, groupConsultsByDay, isLongContext, LESSON_STATUS_LABEL, lessonFacts,
  lessonSource, lessonStatus, OBS_EMPTY, OBS_INTRO, observationFacts, observationGaps, observationSource, observationText, observationTone,
  observationTypeLabel, openProposals, proposalLine, removedReasonLabel, researchingGap, scoreText, sortLessons, sortObservations, tidySummary, unresolvedGapCount,
  usedLabel, usedLessons, usesLine,
  type ConsultFilter, type ConsultStatus, type LessonFilter, type LessonStatus, type ObservationFilter } from "../lib/expert";
import { fmtAgo, fmtWhen } from "../lib/format";
import { md } from "../lib/md";
import { errMsg, getState, hideBanner, showBanner } from "../state/store";
import { askConfirm } from "../dialogs/ask";
import { isLanHost } from "@platform/lib/host";

export type ExpertTab = "memory" | "observations" | "consults" | "checks";
const TABS: [ExpertTab, string][] = [["memory", "Memory"], ["observations", "Observations"], ["consults", "Consults"], ["checks", "Checks"]];
const REFRESH_MS = 10_000, CHECKS_MS = 2_000, PROPOSALS_MS = 5_000, WORK_MS = 5_000, ARCHIVE_SHOWN = 20;

/** The memory route's budget fields (all absent on an older server). */
export type MemoryBudget = Pick<ExpertMemory, "limit" | "tidy_at" | "total" | "last_tidy">;

type Confirm = (title: string, text: string, okLabel?: string, danger?: boolean) => Promise<boolean>;

interface Data {
  memory: ExpertItem[] | null;
  /** The memory's budget (from the memory route) and what recently left it (the archive route, fetched with it). */
  budget: MemoryBudget | null;
  archive: ExpertArchived[] | null;
  pending: ExpertObs[] | null;
  done: ExpertObs[] | null;
  consults: ExpertConsult[] | null;
  checks: ExpertChecks | null;
  proposals: ExpertProposal[] | null;
  /** A background consolidation's progress (from the observations route). */
  consolidating: ExpertConsolidating | null;
}
const EMPTY: Data = { memory: null, budget: null, archive: null, pending: null, done: null, consults: null, checks: null, proposals: null, consolidating: null };

/** The memory route's reply as Data: the lessons and the budget fields (whatever of them the server sent). */
const fromMemory = (m: ExpertMemory): Pick<Data, "memory" | "budget"> =>
  ({ memory: m.items || [], budget: { limit: m.limit, tidy_at: m.tidy_at, total: m.total, last_tidy: m.last_tidy ?? null } });

/** The open tab's data for expert `id`, kept fresh (see the header). `reload` refetches the tab now. */
function useExpertData(id: string, seq: number, tab: ExpertTab) {
  const [data, setData] = useState<Data>(EMPTY);
  const lastErr = useRef("");
  const live = useRef(id);
  live.current = id;
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

  const loadProposals = useCallback(async (loud = false) => {
    const want = id;
    const p = await guard(() => expertApi.proposals(want), loud);
    if (live.current === want && p) setData((d) => ({ ...d, proposals: p.proposals || [] }));
  }, [id, guard]);

  const loadObs = useCallback(async (loud = false) => {
    const want = id;
    const o = await guard(() => expertApi.observations(want), loud);
    if (live.current === want && o) {
      setData((d) => ({ ...d, pending: o.pending || [], done: o.done || [], consolidating: o.consolidating || null }));
    }
  }, [id, guard]);

  const load = useCallback(async (which: ExpertTab, loud = false) => {
    const want = id;
    if (which === "memory") {
      // The archive quietly: an older server (or the LAN door) has no such route, and then there is just no section.
      const [m, p, a] = await Promise.all([guard(() => expertApi.memory(want), loud), guard(() => expertApi.proposals(want), loud),
        expertApi.memoryArchive(want).catch(() => undefined)]);
      if (live.current !== want) return;
      setData((d) => ({ ...d, ...(m ? fromMemory(m) : {}), ...(p ? { proposals: p.proposals || [] } : {}),
        ...(a ? { archive: Array.isArray(a.archived) ? a.archived : [] } : {}) }));
    } else if (which === "observations") {
      // The proposals too (their status lines show here, and a proposed gap says where its proposal stands), and the
      // memory (an opened row names the lesson it landed on).
      const [, , m] = await Promise.all([loadObs(loud), loadProposals(loud), guard(() => expertApi.memory(want), loud)]);
      if (live.current === want && m) setData((d) => ({ ...d, ...fromMemory(m) }));
    } else if (which === "consults") {
      // The memory too: an opened consult names the lessons it used by their claims.
      const [c, m] = await Promise.all([guard(() => expertApi.consults(want), loud), guard(() => expertApi.memory(want), loud)]);
      if (live.current !== want) return;
      setData((d) => ({ ...d, ...(c ? { consults: c.consults || [] } : {}), ...(m ? fromMemory(m) : {}) }));
    } else {
      const k = await guard(() => expertApi.checks(want), loud);
      if (live.current === want && k) setData((d) => ({ ...d, checks: k }));
    }
  }, [id, guard, loadObs, loadProposals]);
  /** The open tab, plus (off the Observations tab) the observations, so a consolidation started or running is noticed
   *  and followed whichever tab is open. */
  const refresh = useCallback((which: ExpertTab) => {
    void load(which);
    if (which !== "observations") void loadObs();
  }, [load, loadObs]);

  // A new bot starts empty (never another expert's rows under this one's name).
  useEffect(() => { setData(EMPTY); lastErr.current = ""; }, [id]);
  useEffect(() => { refresh(tab); }, [refresh, tab, seq]);
  useEffect(() => {
    const t = window.setInterval(() => { if (!document.hidden) refresh(tab); }, REFRESH_MS);
    return () => window.clearInterval(t);
  }, [refresh, tab]);
  // A consolidation running in the background, or a gap being researched: follow it until it is done (lessons folded,
  // a proposal card or a question in the chat). When it ends, the open tab refetches (new lessons, new proposals).
  const working = !!data.consolidating?.running || observationGaps(data.pending, data.done).some((g) => g.status === "researching");
  const wasWorking = useRef(false);
  const latest = useRef({ refresh, tab });
  latest.current = { refresh, tab };
  useEffect(() => {
    if (wasWorking.current && !working) latest.current.refresh(latest.current.tab);
    wasWorking.current = working;
    if (!working) return;
    const t = window.setInterval(() => { if (!document.hidden) void loadObs(); }, WORK_MS);
    return () => window.clearInterval(t);
  }, [working, loadObs]);
  // A gate run in progress: follow it closely, whichever tab is open (the Checks tab shows it; the others do not mind).
  const running = !!data.checks?.status?.running;
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => { if (!document.hidden) void load("checks"); }, CHECKS_MS);
    return () => window.clearInterval(t);
  }, [running, load]);
  // A skill proposal is drafted and checked by a background worker: follow it until its card is up (or it failed).
  const preparing = anyPreparing(data.proposals);
  useEffect(() => {
    if (!preparing) return;
    const t = window.setInterval(() => { if (!document.hidden) void loadProposals(); }, PROPOSALS_MS);
    return () => window.clearInterval(t);
  }, [preparing, loadProposals]);

  return { data, setData, reload: load, guard };
}

// ------------------------------------------------------------------ panels ----

const CHEVRON = (
  <svg className="xp-chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="m6 9 6 6 6-6" />
  </svg>
);

export type CardTone = "ok" | "warn" | "muted" | "accent";

/** The one card all three list tabs use: a header button (status marker, source, time or count, a chevron that turns
 *  when open; `aria-expanded`), the text clamped to two lines while closed, a footer of small facts, and `detail()`
 *  rendered only while open. `note`: a muted line under the facts (a failed proposal's error). */
export function XCard({ tone, mark, source, sourceTitle, right, rightTitle, text, facts, note, initialOpen = false, className = "", detail }: {
  tone: CardTone; mark: string; source: string; sourceTitle?: string; right: string; rightTitle?: string; text: string; facts: string[];
  note?: string; initialOpen?: boolean; className?: string; detail: () => ReactNode;
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

export function LessonCard({ it, onForget, lan = false, initialOpen = false, nowMs }: {
  it: ExpertItem; onForget: (it: ExpertItem) => void; lan?: boolean; initialOpen?: boolean; nowMs?: number;
}) {
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
          {lan ? null : (
            <div className="xp-acts">
              <button type="button" className="xp-forget" title="Forget this lesson" onClick={() => onForget(it)}>Forget</button>
            </div>
          )}
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

/** "Recently removed (N)", collapsed: what left memory (newest first, at most ARCHIVE_SHOWN), each its claim (two lines)
 *  and why and when it went. Nothing while the archive is empty or not loaded. */
export function RemovedLessons({ archived, items = null, nowMs }: {
  archived: ExpertArchived[] | null | undefined; items?: ExpertItem[] | null; nowMs?: number;
}) {
  const rows = (archived || []).filter((a) => a && typeof a.claim === "string").slice(0, ARCHIVE_SHOWN);
  if (!rows.length) return null;
  return (
    <details className="xp-removed">
      <summary className="xp-h">{CHEVRON}Recently removed ({rows.length})</summary>
      <ul className="xp-removed-list">
        {rows.map((a, i) => (
          <li key={`${a.id}-${a.removed_at || i}`} className="xp-removed-row">
            <span className="xp-removed-claim">{a.claim}</span>
            <span className="xp-facts" title={a.removed_at ? fmtWhen(a.removed_at) : undefined}>
              {[removedReasonLabel(a, items), a.removed_at ? fmtAgo(a.removed_at, nowMs) : ""].filter(Boolean).join(" · ")}
            </span>
            {a.why ? <span className="xp-facts xp-note">{a.why}</span> : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

export function MemoryPanel({ items, onForget, proposals = null, budget = null, archived = null, lan = false, nowMs }: {
  items: ExpertItem[] | null; onForget: (it: ExpertItem) => void; proposals?: ExpertProposal[] | null;
  /** The memory route's budget fields (the line on top) and the archive (the "Recently removed" section). */
  budget?: MemoryBudget | null; archived?: ExpertArchived[] | null;
  /** On the phone: the LAN door does not serve `forget` (it writes memory), so the button is hidden. */
  lan?: boolean; nowMs?: number;
}) {
  const [filter, setFilter] = useState<LessonFilter>("all");
  if (items == null) return <div className="xp-empty">Loading…</div>;
  const open = openProposals(proposals), sorted = sortLessons(items), shown = filterLessons(sorted, filter);
  const counts = { all: items.length, "in-use": filterLessons(items, "in-use").length, "not-used": filterLessons(items, "not-used").length };
  return (
    <>
      <MemoryBudgetLine budget={budget} listed={items.length} nowMs={nowMs} />
      <ProposalLines open={open} />
      {items.length ? (
        <>
          <Filters label="Show lessons" options={LESSON_FILTERS} counts={counts} value={filter} onChange={setFilter} total={items.length} />
          {shown.length ? <ul className="xp-cards">{shown.map((it) => <LessonCard key={it.id} it={it} onForget={onForget} lan={lan} nowMs={nowMs} />)}</ul>
            : <div className="xp-empty">No lessons match this filter.</div>}
        </>
      ) : (
        <div className="xp-empty">No lessons yet. What you teach it in chat, and what it learns from the sessions that consult it, appears here once consolidated.</div>
      )}
      <RemovedLessons archived={archived} items={items} nowMs={nowMs} />
    </>
  );
}

function ProposalLines({ open }: { open: ExpertProposal[] }) {
  return open.length ? (
    <ul className="xp-proposals">
      {open.map((p) => <li key={p.id} className={`xp-proposal ${p.status}`}>{proposalLine(p)}</li>)}
    </ul>
  ) : null;
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

// ---- Observations ----

/** "Consolidate now" (the Observations tab). Off while a consolidation runs or there is nothing to fold in or look into. */
function ConsolidateButton({ busy, running, work, onClick }: { busy: boolean; running: boolean; work: boolean; onClick: () => void }) {
  const on = busy || running;
  return (
    <button type="button" className={on ? "busy" : undefined} disabled={on || !work} onClick={onClick}
      title={running ? "A consolidation is running" : work ? "Fold the waiting observations into lessons and look into the open questions now" : "Nothing is waiting"}>
      {on ? "Consolidating…" : "Consolidate now"}
    </button>
  );
}

const claimOf = (items: ExpertItem[] | null, id: string | undefined): string => (id ? items?.find((it) => it.id === id)?.claim || id : "");

export function ObsCard({ o, consolidated, researching, proposals, items, lan, onDrop, initialOpen = false, nowMs }: {
  o: ExpertObs; consolidated: boolean; researching: string | null; proposals: ExpertProposal[] | null; items: ExpertItem[] | null;
  lan: boolean; onDrop: (g: ExpertGap) => void; initialOpen?: boolean; nowMs?: number;
}) {
  const g = o.gap || null, op = o.outcome?.op || "";
  const prop = g?.status === "proposed" ? gapProposal(g, proposals) : null;
  const note = gapNote(g?.error), f = g?.finding;
  return (
    <XCard tone={observationTone(o.kind, o.outcome)} mark={observationTypeLabel(o.kind, o.outcome)} source={observationSource(o)} sourceTitle={o.source?.target}
      right={fmtAgo(o.ts, nowMs)} rightTitle={o.ts ? fmtWhen(o.ts) : undefined} text={observationText(o)}
      facts={observationFacts(o, consolidated, researching, proposals)} note={prop?.status === "failed" ? prop.error : ""} initialOpen={initialOpen}
      detail={() => (
        <>
          {op === "contradict" ? (
            <>
              {o.outcome?.item ? <Part label="Contradicted"><p className="xp-txt">{claimOf(items, o.outcome.item)}</p></Part> : null}
              {o.outcome?.new ? <Part label="Corrected rule"><p className="xp-txt">{claimOf(items, o.outcome.new)}</p></Part> : null}
            </>
          ) : (op === "add" || op === "bump") && o.outcome?.item ? (
            <Part label="Lesson"><p className="xp-txt">{claimOf(items, o.outcome.item)}</p></Part>
          ) : null}
          {note ? <p className="xp-facts">{note}</p> : null}
          {f ? (
            <>
              <Part label="Found"><Md text={f.answer} /></Part>
              {f.refs?.length ? <Part label="Files"><Files refs={f.refs} /></Part> : null}
            </>
          ) : null}
          {g && gapUnresolved(g) && !lan ? (
            <div className="xp-acts">
              <button type="button" className="xp-forget" title="Drop this question: it is not looked into again" onClick={() => onDrop(g)}>Drop</button>
            </div>
          ) : null}
        </>
      )} />
  );
}

const OBS_FILTERS: [ObservationFilter, string][] = [["all", "All"], ["unsure", "Unsure"], ["learnings", "Learnings"]];

export function ObservationsPanel({ pending, done, consolidating = null, proposals = null, items = null, busy = false, onConsolidate, onDrop, lan = false, nowMs }: {
  pending: ExpertObs[] | null; done: ExpertObs[] | null; consolidating?: ExpertConsolidating | null;
  /** The skill proposals (as on the Memory tab): their status lines under the intro, and what a proposed gap says. */
  proposals?: ExpertProposal[] | null;
  /** The memory items: an opened row names the lesson it landed on by its claim. */
  items?: ExpertItem[] | null;
  busy?: boolean; onConsolidate: () => void; onDrop: (g: ExpertGap) => void;
  /** On the phone: the LAN door does not serve `consolidate` or `dismiss` (they write memory), so the buttons are hidden. */
  lan?: boolean; nowMs?: number;
}) {
  const [filter, setFilter] = useState<ObservationFilter>("all");
  if (pending == null) return <div className="xp-empty">Loading…</div>;
  const { waiting, done: folded } = sortObservations(pending, done), running = !!consolidating?.running;
  const researching = researchingGap(consolidating), work = waiting.length > 0 || unresolvedGapCount(observationGaps(waiting, folded)) > 0;
  const all = [...waiting, ...folded];
  const counts = { all: all.length, unsure: filterObservations(all, "unsure").length, learnings: filterObservations(all, "learnings").length };
  const w = filterObservations(waiting, filter), d = filterObservations(folded, filter);
  const card = (consolidated: boolean) => (o: ExpertObs) =>
    <ObsCard key={o.id} o={o} consolidated={consolidated} researching={researching} proposals={proposals} items={items} lan={lan} onDrop={onDrop} nowMs={nowMs} />;
  return (
    <>
      <div className="xp-bar">
        <span className="xp-line">{OBS_INTRO}{running ? <span className="xp-status">{consolidatingLine(consolidating)}</span> : null}</span>
        {lan ? null : <ConsolidateButton busy={busy} running={running} work={work} onClick={onConsolidate} />}
      </div>
      <ProposalLines open={openProposals(proposals)} />
      {!all.length ? <div className="xp-empty">{OBS_EMPTY}</div> : (
        <>
          <Filters label="Show observations" options={OBS_FILTERS} counts={counts} value={filter} onChange={setFilter} total={all.length} />
          {w.length ? (
            <section className="xp-sec" aria-label="Waiting">
              <h4 className="xp-h">Waiting</h4>
              <ul className="xp-cards">{w.map(card(false))}</ul>
            </section>
          ) : null}
          {d.length ? (
            <section className="xp-sec" aria-label="Consolidated">
              <h4 className="xp-h">Consolidated</h4>
              {groupByDay(d, nowMs).map((g) => (
                <section key={g.key} className="xp-dayg" aria-label={g.label}>
                  <h5 className="xp-day">{g.label}</h5>
                  <ul className="xp-cards">{g.items.map(card(true))}</ul>
                </section>
              ))}
            </section>
          ) : null}
          {!w.length && !d.length ? <div className="xp-empty">No observations match this filter.</div> : null}
        </>
      )}
    </>
  );
}

export function ChecksPanel({ checks, onRun, starting }: { checks: ExpertChecks | null; onRun: () => void; starting: boolean }) {
  if (checks == null) return <div className="xp-empty">Loading…</div>;
  if (!checks.questions) return <div className="xp-empty">This expert has no check questions yet. Checks grade its answers to a fixed set of questions (its questions.md) whenever you run them.</div>;
  const st = checks.status, run = checks.latest, running = !!st?.running || starting;
  return (
    <>
      <div className="xp-bar">
        <span className="xp-line">
          {running ? checkProgress(st) || "Starting checks…"
            : run ? <><b className="xp-score">{scoreText(run)}</b> passed · {fmtAgo(run.ts)}{run.engine ? ` · ${run.engine}` : ""}</>
            : `${checks.questions} question${checks.questions === 1 ? "" : "s"}, not run yet.`}
        </span>
        <button type="button" className={running ? "busy" : undefined} disabled={running} onClick={onRun}
          title="Ask the expert every check question and grade the answers">{running ? "Running…" : "Run checks"}</button>
      </div>
      {running && st?.total ? <div className="xp-prog"><div style={{ width: `${Math.round((100 * st.done) / st.total)}%` }} /></div> : null}
      {run?.results?.length ? (
        <ul className="xp-list">
          {run.results.map((r, i) => (
            <li key={`${i}:${r.q}`} className="xp-row xp-fold">
              <details>
                <summary>
                  <span className={`xp-mark ${r.passed ? "pass" : "fail"}`} aria-label={r.passed ? "passed" : "failed"}>{r.passed ? "✓" : "✗"}</span>
                  <span className="xp-claim">{r.q}</span>
                  {r.why ? <span className="xp-meta xp-why">{r.why}</span> : null}
                </summary>
                <div className="xp-body"><pre>{r.answer || "(no answer)"}</pre></div>
              </details>
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

// ------------------------------------------------------------------ tabs + data ----

/** The tab strip and the open panel, with their data. `confirm`: the Settings dialog passes its own (it must hold
 *  itself open while the confirm is up); the preview column uses the page's. */
export function ExpertPanels({ b, confirm = askConfirm }: { b: Bot; confirm?: Confirm }) {
  const [tab, setTab] = useState<ExpertTab>("memory");
  const { data, setData, reload, guard } = useExpertData(b.id, b.seq, tab);
  const [consolidating, setConsolidating] = useState(false);
  const [starting, setStarting] = useState(false);
  const id = b.id, uid = useId();  // Settings may mount these panels while the preview column shows them too

  const onForget = async (it: ExpertItem) => {
    if (!(await confirm("Forget this lesson?", `“${it.claim}” leaves ${b.name}'s memory. It may learn it again later.`, "Forget"))) return;
    const r = await guard(() => expertApi.forgetMemory(id, it.id), true);
    if (r) setData((d) => ({ ...d, memory: (d.memory || []).filter((x) => x.id !== it.id) }));
    void reload("memory");
  };
  // Consolidation answers at once and runs in the background (observations' `consolidating` follows it, polled while it
  // runs; a proposal it raises is drafted in the background too and shows on the Memory tab as "preparing"), so the
  // button is busy only for the request itself.
  const onConsolidate = async () => {
    setConsolidating(true);
    try {
      const r = await guard(() => expertApi.consolidate(id, true), true);
      if (r?.started) setData((d) => ({ ...d, consolidating: d.consolidating?.running ? d.consolidating : { running: true, stage: "", gap: null } }));
    } finally { setConsolidating(false); }
    void reload("observations");
  };
  const onRun = async () => {
    setStarting(true);
    try { await guard(() => expertApi.runChecks(id), true); await reload("checks"); } finally { setStarting(false); }
  };
  const onDrop = async (g: ExpertGap) => {
    const r = await guard(() => expertApi.dismissGap(id, g.id), true);
    const swap = (os: ExpertObs[] | null) => os && os.map((o) => (o.gap?.id === g.id ? { ...o, gap: r || o.gap } : o));
    if (r) setData((d) => ({ ...d, pending: swap(d.pending), done: swap(d.done) }));
    void reload("observations");
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
        {tab === "memory" ? <MemoryPanel items={data.memory} proposals={data.proposals} budget={data.budget} archived={data.archive} lan={isLanHost()} onForget={(it) => { void onForget(it); }} /> : null}
        {tab === "observations" ? <ObservationsPanel pending={data.pending} done={data.done} consolidating={data.consolidating} proposals={data.proposals} items={data.memory} busy={consolidating}
          lan={isLanHost()} onConsolidate={() => { void onConsolidate(); }} onDrop={(g) => { void onDrop(g); }} /> : null}
        {tab === "consults" ? <ConsultsPanel consults={data.consults} items={data.memory} /> : null}
        {tab === "checks" ? <ChecksPanel checks={data.checks} starting={starting} onRun={() => { void onRun(); }} /> : null}
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
