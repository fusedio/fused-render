// An expert's preview column (docs/experts.md §10): no browser, so in place of the screenshot, live view and info
// sections it shows what the expert knows and how it is doing, on three tabs in the Browser | Terminal strip's style:
//   Memory   the lessons it keeps (most seen first; Forget per row) and the observations still waiting to be folded in
//   Consults the questions agent sessions asked it (newest first), each opening to the full answer and its file refs
//   Checks   its fixed questions (questions.md) graded on the last run; "Run checks" starts one and shows it move
// Settings' "Expert" tab mounts the same panels (ExpertPanels) so the phone, which hides this column, reaches them.
// Data: /api/experts/<id>/… (lib/api.ts expertApi), fetched for the open tab on a bot change, again when the bot's
// `seq` moves (a chat turn may have taught it something) and every 10 s while the page is visible; the checks every
// 2 s while a run is going; the proposals (shown on the Memory tab) every 5 s while one is still being prepared. A failed call shows the page banner once (not again on every refresh while it keeps failing).
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { expertApi, type Bot, type ExpertChecks, type ExpertConsult, type ExpertItem, type ExpertObs, type ExpertProposal } from "../lib/api";
import { anyPreparing, basename, checkProgress, consultMeta, lessonTags, openProposals, pendingLine, proposalLine, scoreText, seenLabel, SEEN_ONCE_LINE,
  sortConsults, splitLessons } from "../lib/expert";
import { fmtAgo, fmtWhen } from "../lib/format";
import { errMsg, getState, hideBanner, showBanner } from "../state/store";
import { askConfirm } from "../dialogs/ask";
import { isLanHost } from "@platform/lib/host";

export type ExpertTab = "memory" | "consults" | "checks";
const TABS: [ExpertTab, string][] = [["memory", "Memory"], ["consults", "Consults"], ["checks", "Checks"]];
const REFRESH_MS = 10_000, CHECKS_MS = 2_000, PROPOSALS_MS = 5_000;

type Confirm = (title: string, text: string, okLabel?: string, danger?: boolean) => Promise<boolean>;

interface Data {
  memory: ExpertItem[] | null;
  pending: ExpertObs[] | null;
  due: boolean;
  consults: ExpertConsult[] | null;
  checks: ExpertChecks | null;
  proposals: ExpertProposal[] | null;
}
const EMPTY: Data = { memory: null, pending: null, due: false, consults: null, checks: null, proposals: null };

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

  const load = useCallback(async (which: ExpertTab, loud = false) => {
    const want = id;
    if (which === "memory") {
      const [m, o, p] = await Promise.all([guard(() => expertApi.memory(want), loud), guard(() => expertApi.observations(want), loud),
        guard(() => expertApi.proposals(want), loud)]);
      if (live.current !== want) return;
      setData((d) => ({ ...d, ...(m ? { memory: m.items || [] } : {}), ...(o ? { pending: o.pending || [], due: !!o.due } : {}),
        ...(p ? { proposals: p.proposals || [] } : {}) }));
    } else if (which === "consults") {
      const c = await guard(() => expertApi.consults(want), loud);
      if (live.current === want && c) setData((d) => ({ ...d, consults: c.consults || [] }));
    } else {
      const k = await guard(() => expertApi.checks(want), loud);
      if (live.current === want && k) setData((d) => ({ ...d, checks: k }));
    }
  }, [id, guard]);

  // A new bot starts empty (never another expert's rows under this one's name).
  useEffect(() => { setData(EMPTY); lastErr.current = ""; }, [id]);
  useEffect(() => { void load(tab); }, [load, tab, seq]);
  useEffect(() => {
    const t = window.setInterval(() => { if (!document.hidden) void load(tab); }, REFRESH_MS);
    return () => window.clearInterval(t);
  }, [load, tab]);
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
          {lessonTags(it).map((t) => <span key={t} className={`xp-tag ${t}`}>{t}</span>)}
        </span>
      </div>
      <button type="button" className="xp-forget" title="Forget this lesson" onClick={() => onForget(it)}>Forget</button>
    </li>
  );
}

export function MemoryPanel({ items, pending, due, onForget, onConsolidate, busy, proposals = null, lan = false }: {
  items: ExpertItem[] | null; pending: ExpertObs[] | null; due: boolean;
  onForget: (it: ExpertItem) => void; onConsolidate: () => void; busy: boolean;
  proposals?: ExpertProposal[] | null;
  /** On the phone: the LAN door does not serve `consolidate` (it writes memory), so the button stays off. */
  lan?: boolean;
}) {
  if (items == null) return <div className="xp-empty">Loading…</div>;
  const { indexed, seenOnce } = splitLessons(items), n = pending?.length || 0;
  const open = openProposals(proposals);
  return (
    <>
      <div className="xp-bar">
        <span className="xp-line">{pending == null ? "" : pendingLine(n, due)}</span>
        <button type="button" className={busy ? "busy" : undefined} disabled={busy || !n || lan}
          title={lan ? "Consolidate on the Mac" : n ? "Fold the waiting observations into lessons now" : "Nothing is waiting"} onClick={onConsolidate}>
          {busy ? "Consolidating…" : "Consolidate now"}
        </button>
      </div>
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
  // Consolidation answers at once (a proposal it raises is drafted in the background: the panel shows it as
  // "preparing" and polls), so the button is busy only for the request itself; then the memory and proposals refetch.
  const onConsolidate = async () => {
    setConsolidating(true);
    try { await guard(() => expertApi.consolidate(id, true), true); } finally { setConsolidating(false); }
    void reload("memory");
  };
  const onRun = async () => {
    setStarting(true);
    try { await guard(() => expertApi.runChecks(id), true); await reload("checks"); } finally { setStarting(false); }
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const i = TABS.findIndex(([t]) => t === tab), next = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length][0];
    setTab(next);
    document.getElementById(`${uid}-${next}`)?.focus();
  };

  return (
    <>
      <div className="viewsw xp-tabs" role="tablist" aria-label="Expert" onKeyDown={onKey}>
        {TABS.map(([t, l]) => (
          <button key={t} type="button" id={`${uid}-${t}`} role="tab" className={`satab${tab === t ? " on" : ""}`} aria-selected={tab === t}
            aria-controls={`${uid}-panel`} tabIndex={tab === t ? 0 : -1} onClick={() => setTab(t)}>{l}</button>
        ))}
      </div>
      <div className="xp-panel" id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-${tab}`} data-tab={tab}>
        {tab === "memory" ? <MemoryPanel items={data.memory} pending={data.pending} due={data.due} busy={consolidating} proposals={data.proposals} lan={isLanHost()}
          onForget={(it) => { void onForget(it); }} onConsolidate={() => { void onConsolidate(); }} /> : null}
        {tab === "consults" ? <ConsultsPanel consults={data.consults} /> : null}
        {tab === "checks" ? <ChecksPanel checks={data.checks} starting={starting} onRun={() => { void onRun(); }} /> : null}
      </div>
    </>
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
