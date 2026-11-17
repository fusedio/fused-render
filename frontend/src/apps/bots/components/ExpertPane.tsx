// An expert's preview column (docs/experts.md §10): no browser, so in place of the screenshot, live view and info
// sections it shows what the expert knows and how it is doing, on four tabs in the Browser | Terminal strip's style:
//   Memory       the lessons it keeps (most seen first; Forget per row), the ones not used in answers yet, and the
//                skill proposals in progress
//   Observations everything it noted from consults and sessions: "Waiting" (oldest first) and "Consolidated" (newest
//                first, each with what came of it); "Consolidate now" and its live stage line on top; an unsure consult
//                (a gap) opens to what research found and can be dropped while it is unresolved
//   Consults     the questions agent sessions asked it (newest first), each opening to the full answer and its file refs
//   Checks       its fixed questions (questions.md) graded on the last run; "Run checks" starts one and shows it move
// Settings' "Expert" tab mounts the same panels (ExpertPanels) so the phone, which hides this column, reaches them.
// Data: /api/experts/<id>/… (lib/api.ts expertApi), fetched for the open tab on a bot change, again when the bot's
// `seq` moves (a chat turn may have taught it something) and every 10 s while the page is visible; the observations
// (which carry a consolidation's progress) with every refresh whichever tab is open, and every 5 s while a
// consolidation runs or a gap is being researched; the checks every 2 s while a run is going; the proposals (shown on
// the Memory tab) every 5 s while one is still being prepared. A failed call shows the page banner once (not again on
// every refresh while it keeps failing).
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { expertApi, type Bot, type ExpertChecks, type ExpertConsolidating, type ExpertConsult, type ExpertGap, type ExpertItem, type ExpertObs,
  type ExpertProposal } from "../lib/api";
import { anyPreparing, askedLabel, basename, checkProgress, consolidatingLine, consultMeta, gapUnresolved, lessonTagClass, lessonTags, OBS_EMPTY, OBS_INTRO,
  observationGaps, observationMeta, observationResultLabel, observationTypeLabel, openProposals, proposalLine, researchingGap, scoreText, seenLabel,
  SEEN_ONCE_LINE, sortConsults, sortObservations, splitLessons, unresolvedGapCount } from "../lib/expert";
import { fmtAgo, fmtWhen } from "../lib/format";
import { errMsg, getState, hideBanner, showBanner } from "../state/store";
import { askConfirm } from "../dialogs/ask";
import { isLanHost } from "@platform/lib/host";

export type ExpertTab = "memory" | "observations" | "consults" | "checks";
const TABS: [ExpertTab, string][] = [["memory", "Memory"], ["observations", "Observations"], ["consults", "Consults"], ["checks", "Checks"]];
const REFRESH_MS = 10_000, CHECKS_MS = 2_000, PROPOSALS_MS = 5_000, WORK_MS = 5_000;

type Confirm = (title: string, text: string, okLabel?: string, danger?: boolean) => Promise<boolean>;

interface Data {
  memory: ExpertItem[] | null;
  pending: ExpertObs[] | null;
  done: ExpertObs[] | null;
  consults: ExpertConsult[] | null;
  checks: ExpertChecks | null;
  proposals: ExpertProposal[] | null;
  /** A background consolidation's progress (from the observations route). */
  consolidating: ExpertConsolidating | null;
}
const EMPTY: Data = { memory: null, pending: null, done: null, consults: null, checks: null, proposals: null, consolidating: null };

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
      const [m, p] = await Promise.all([guard(() => expertApi.memory(want), loud), guard(() => expertApi.proposals(want), loud)]);
      if (live.current !== want) return;
      setData((d) => ({ ...d, ...(m ? { memory: m.items || [] } : {}), ...(p ? { proposals: p.proposals || [] } : {}) }));
    } else if (which === "observations") {
      await loadObs(loud);
    } else if (which === "consults") {
      const c = await guard(() => expertApi.consults(want), loud);
      if (live.current === want && c) setData((d) => ({ ...d, consults: c.consults || [] }));
    } else {
      const k = await guard(() => expertApi.checks(want), loud);
      if (live.current === want && k) setData((d) => ({ ...d, checks: k }));
    }
  }, [id, guard, loadObs]);
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

function LessonRow({ it, onForget }: { it: ExpertItem; onForget: (it: ExpertItem) => void }) {
  return (
    <li className="xp-row">
      <div className="xp-main">
        <span className="xp-claim">{it.claim}</span>
        <span className="xp-meta">
          <span title={it.updated ? `last seen ${fmtWhen(it.updated)}` : undefined}>{seenLabel(it.count)}</span>
          {lessonTags(it).map((t) => <span key={t} className={`xp-tag ${lessonTagClass(t)}`}>{t}</span>)}
        </span>
      </div>
      <button type="button" className="xp-forget" title="Forget this lesson" onClick={() => onForget(it)}>Forget</button>
    </li>
  );
}

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

export function MemoryPanel({ items, onForget, proposals = null }: {
  items: ExpertItem[] | null; onForget: (it: ExpertItem) => void; proposals?: ExpertProposal[] | null;
}) {
  if (items == null) return <div className="xp-empty">Loading…</div>;
  const { indexed, seenOnce } = splitLessons(items), open = openProposals(proposals);
  return (
    <>
      {open.length ? (
        <ul className="xp-proposals">
          {open.map((p) => <li key={p.id} className={`xp-proposal ${p.status}`}>{proposalLine(p)}</li>)}
        </ul>
      ) : null}
      {indexed.length || seenOnce.length ? (
        <>
          {indexed.length ? <ul className="xp-list">{indexed.map((it) => <LessonRow key={it.id} it={it} onForget={onForget} />)}</ul> : null}
          {seenOnce.length ? (
            <>
              <div className="xp-divider" role="separator">{SEEN_ONCE_LINE}</div>
              <ul className="xp-list xp-seen-once">{seenOnce.map((it) => <LessonRow key={it.id} it={it} onForget={onForget} />)}</ul>
            </>
          ) : null}
        </>
      ) : (
        <div className="xp-empty">No lessons yet. What you teach it in chat, and what it learns from the sessions that consult it, appears here once consolidated.</div>
      )}
    </>
  );
}

export function ConsultsPanel({ consults }: { consults: ExpertConsult[] | null }) {
  if (consults == null) return <div className="xp-empty">Loading…</div>;
  if (!consults.length) return <div className="xp-empty">No consults yet. When an agent session asks this expert a question, the question and its answer appear here.</div>;
  return (
    <ul className="xp-list">
      {sortConsults(consults).map((c) => (
        <li key={c.id} className="xp-row xp-fold">
          <details>
            <summary>
              <span className="xp-claim">{c.question}</span>
              <span className="xp-meta">
                <span title={fmtWhen(c.ts)}>{consultMeta(c)}</span>
                {c.unsure ? <span className="xp-tag unsure" title="The expert was not sure of this answer">unsure</span> : null}
              </span>
            </summary>
            <div className="xp-body">
              {c.context ? <p className="xp-ctx">{c.context}</p> : null}
              <pre>{c.answer || "(no answer)"}</pre>
              {c.refs?.length ? (
                <div className="xp-refs">
                  <span className="xp-meta">Files</span>
                  {c.refs.map((r) => <code key={r} title={r}>{basename(r) || r}</code>)}
                </div>
              ) : null}
              {c.caller?.target ? <div className="xp-meta" title={c.caller.target}>From {c.caller.target}</div> : null}
            </div>
          </details>
        </li>
      ))}
    </ul>
  );
}

function ObsRow({ o, consolidated, researching, lan, onDrop }: {
  o: ExpertObs; consolidated: boolean; researching: string | null; lan: boolean; onDrop: (g: ExpertGap) => void;
}) {
  const g = o.gap || null, result = consolidated || g ? observationResultLabel(o, researching) : "";
  const status = g ? (researching === g.id && gapUnresolved(g) ? "researching" : g.status) : o.outcome?.op || "";
  const text = o.claim || g?.question || "";
  const meta = (
    <span className="xp-meta">
      <span className={`xp-tag kind-${(o.kind || "learning").replace(/[^a-z_]/gi, "")}`}>{observationTypeLabel(o.kind, o.outcome)}</span>
      <span title={o.ts ? fmtWhen(o.ts) : undefined}>{observationMeta(o)}</span>
      {result ? <span className={`xp-tag res-${status}`}>{result}</span> : null}
      {g && (g.count || 0) > 1 ? <span>{askedLabel(g.count)}</span> : null}
    </span>
  );
  const err = g?.error ? <span className="xp-meta xp-gap-err">{g.error}</span> : null;
  const f = g?.finding;
  return (
    <li className={`xp-row${f ? " xp-fold" : ""}`}>
      {f ? (
        <details>
          <summary>
            <span className="xp-claim">{text}</span>
            {meta}
            {err}
          </summary>
          <div className="xp-body">
            <pre>{f.answer || "(no answer)"}</pre>
            {f.refs?.length ? (
              <div className="xp-refs">
                <span className="xp-meta">Files</span>
                {f.refs.map((r) => <code key={r} title={r}>{basename(r) || r}</code>)}
              </div>
            ) : null}
          </div>
        </details>
      ) : (
        <div className="xp-main">
          <span className="xp-claim">{text}</span>
          {meta}
          {err}
        </div>
      )}
      {g && gapUnresolved(g) && !lan ? (
        <button type="button" className="xp-forget" title="Drop this question: it is not looked into again" onClick={() => onDrop(g)}>Drop</button>
      ) : null}
    </li>
  );
}

export function ObservationsPanel({ pending, done, consolidating = null, busy = false, onConsolidate, onDrop, lan = false }: {
  pending: ExpertObs[] | null; done: ExpertObs[] | null; consolidating?: ExpertConsolidating | null;
  busy?: boolean; onConsolidate: () => void; onDrop: (g: ExpertGap) => void;
  /** On the phone: the LAN door does not serve `consolidate` or `dismiss` (they write memory), so the buttons are hidden. */
  lan?: boolean;
}) {
  if (pending == null) return <div className="xp-empty">Loading…</div>;
  const { waiting, done: folded } = sortObservations(pending, done), running = !!consolidating?.running;
  const researching = researchingGap(consolidating), work = waiting.length > 0 || unresolvedGapCount(observationGaps(waiting, folded)) > 0;
  const row = (consolidated: boolean) => (o: ExpertObs) =>
    <ObsRow key={o.id} o={o} consolidated={consolidated} researching={researching} lan={lan} onDrop={onDrop} />;
  return (
    <>
      <div className="xp-bar">
        <span className="xp-line">{OBS_INTRO}{running ? <span className="xp-status">{consolidatingLine(consolidating)}</span> : null}</span>
        {lan ? null : <ConsolidateButton busy={busy} running={running} work={work} onClick={onConsolidate} />}
      </div>
      {!waiting.length && !folded.length ? <div className="xp-empty">{OBS_EMPTY}</div> : (
        <>
          {waiting.length ? (
            <section className="xp-sec" aria-label="Waiting">
              <h4 className="xp-h">Waiting</h4>
              <ul className="xp-list">{waiting.map(row(false))}</ul>
            </section>
          ) : null}
          {folded.length ? (
            <section className="xp-sec" aria-label="Consolidated">
              <h4 className="xp-h">Consolidated</h4>
              <ul className="xp-list">{folded.map(row(true))}</ul>
            </section>
          ) : null}
        </>
      )}
    </>
  );
}

export function ChecksPanel({ checks, onRun, starting }: { checks: ExpertChecks | null; onRun: () => void; starting: boolean }) {
  if (checks == null) return <div className="xp-empty">Loading…</div>;
  if (!checks.questions) return <div className="xp-empty">This expert has no check questions yet. Checks grade its answers to a fixed set of questions (its questions.md) before and after a skill change.</div>;
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
        {tab === "memory" ? <MemoryPanel items={data.memory} proposals={data.proposals} onForget={(it) => { void onForget(it); }} /> : null}
        {tab === "observations" ? <ObservationsPanel pending={data.pending} done={data.done} consolidating={data.consolidating} busy={consolidating}
          lan={isLanHost()} onConsolidate={() => { void onConsolidate(); }} onDrop={(g) => { void onDrop(g); }} /> : null}
        {tab === "consults" ? <ConsultsPanel consults={data.consults} /> : null}
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
