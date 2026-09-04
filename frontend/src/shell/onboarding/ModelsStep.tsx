// Step 4 — local models, and a head start on downloading them.
//
// The step exists because of a timing problem, not a capability one: a model
// this machine does not have is fetched on FIRST USE — the route answers
// `model_loading` and starts the download rather than refusing (ai_runtime.py
// `api_ai_download`/`_resolve_capability`) — so nothing here is required for
// anything to work. What it buys is that the multi-GB wait happens while the
// user is reading the next step and building their first app, instead of the
// first time they ask an app for a sentence.
//
// So: NOTHING BLOCKS. Start is fire-and-forget (`supervisor.load(...,
// weights_only=True)` returns the instant its thread is up), Next stays live
// the whole time, and leaving the wizard leaves the fetch running — it is a
// server-owned job, reported in the download manager like every other one.
// Skipping costs the user nothing but the wait, later, and the copy says so
// rather than implying a penalty.
//
// What it offers is ONE model per capability and WHETHER EACH ONE FITS: the
// `fit` verdict already on every catalog row (fit.py — measured footprint,
// curator's envelope, or the download's own bytes, in that order), drawn with
// `fitNote`'s own words so this step and the Playground's badge cannot come
// to different conclusions about the same machine. Comfortable ones start
// checked; a tight or too-big one is shown, unchecked, with the reason — the
// selection rule lives in `modelPicks.ts`.
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, HardDrive } from "lucide-react";

import { fitNote } from "@apps/ai_models/shared/fitNote";
import { modelSizeLabel } from "@apps/ai_models/shared/modelSize";
import { downloadAiModel, getAiCatalog } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import {
  fetchJobs,
  isRunning,
  isTerminal,
  jobAmount,
  jobFraction,
  jobStatusLine,
  pollInterval,
  type Job,
} from "@platform/lib/jobs";
import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Skeleton } from "@platform/shadcn/ui/skeleton";

import { comfortableIds, modelPicks, selectedTotal, type ModelPick } from "./modelPicks";
import { StepHeader } from "./StepHeader";

/** `null` while the catalog is still answering — the wizard reads that as
 *  "unknown", not "none", and keeps the step in the list meanwhile. */
export type ModelPicks = ModelPick[] | null;

// What this step knows, kept at MODULE level for the reason `state.ts` keeps
// the resume step there: the wizard mounts a step body per navigation, and
// every step is a link in both directions — so Start, Next, Back would come
// back to a step that had forgotten it had started anything. The selection
// would be re-seeded from the fit verdicts (re-checking boxes the user
// cleared), the jobs poll would not be running, and the button would offer
// the whole ~9 GB again. A second Download is harmless server-side (a fetch
// already in flight is JOINED, supervisor.load) but the step's one promise —
// that progress is visible HERE, since the wizard route renders without the
// download manager — would not survive a single Back.
//
// `started` is WHEN each model was asked for, not a flag — see `jobFor`: a
// job row outlives its work (D663 keeps a finished or failed one until it is
// dismissed), so "is this row mine" is a question about time, and a bare
// boolean cannot answer it. It is never the row's state either; that is read
// off the job, so a download that finishes or fails while the step is open
// turns into a tick or a sentence on its own.
let memory: {
  checked: Set<string> | null;
  /** model id -> `Date.now()` when its Download was sent. */
  started: Map<string, number>;
  errors: Record<string, string>;
} = { checked: null, started: new Map(), errors: {} };

/** Forget what the Models step started — called when the wizard completes,
 *  so a reopened wizard in the same page load offers a fresh selection
 *  rather than rows still claiming to be downloading. */
export function forgetModelsStep(): void {
  memory = { checked: null, started: new Map(), errors: {} };
}

/** Clock skew allowance when comparing a job's server-side `finished_at`
 *  against this page's `Date.now()`. Same machine, so the two clocks are the
 *  same clock; this covers the second-granularity rounding either side. */
const CLOCK_SLACK_MS = 2000;

/** The job that is telling the truth about `id` right now, or none.
 *
 *  Two rules, and the second one is the whole reason this function exists.
 *  A job still in flight is shown whoever started it: an app or another page
 *  pulling the same repo IS this model downloading, and `supervisor.load`
 *  joins rather than races, so reporting it is accurate and hiding it would
 *  leave the row looking idle while bytes move.
 *
 *  A TERMINAL job is shown only when it belongs to this visit — the step
 *  asked for the model, and the row finished after it asked. Without that,
 *  every catalog row was matched against the whole server job list: a `done`
 *  row from last week (kept until dismissed, D663) drew a tick on a model the
 *  user had not chosen and took its checkbox away, and an old failure drew a
 *  sentence about something that happened days ago. */
function jobFor(
  id: string,
  jobs: Map<string, Job>,
  started: Map<string, number>,
): Job | undefined {
  const job = jobs.get(id);
  if (!job) return undefined;
  if (!isTerminal(job)) return job;
  const askedAt = started.get(id);
  if (askedAt === undefined) return undefined;
  if (job.finished_at == null) return undefined;
  return job.finished_at * 1000 >= askedAt - CLOCK_SLACK_MS ? job : undefined;
}

/** The catalog, once, as picks. Lives at the WIZARD level (like
 *  `useClaudeSetup`) because the step list itself depends on the answer: a
 *  machine no engine serves has no Models step at all, and a hook per step
 *  would fetch twice to tell the same thing to two places. */
export function useModelPicks(): ModelPicks {
  const [picks, setPicks] = useState<ModelPicks>(null);
  useEffect(() => {
    let alive = true;
    getAiCatalog().then(
      ({ capabilities }) => {
        if (alive) setPicks(modelPicks(capabilities));
      },
      // A catalog that cannot be read is an empty offer, not an error face:
      // the step drops out and the user loses nothing they needed.
      () => {
        if (alive) setPicks([]);
      },
    );
    return () => {
      alive = false;
    };
  }, []);
  return picks;
}

/** Jobs, polled for as long as this step is on screen, at `pollInterval`'s
 *  own cadence (fast while anything runs, slow when nothing does). The wizard
 *  has no status bar and no download manager on screen (App.tsx renders this
 *  route alone), so progress has to be drawn in the step or it is invisible
 *  until the user leaves — including progress that was already running when
 *  the step opened, which is what a return to it after a Next looks like. */
function useJobs(): Job[] {
  const [jobs, setJobs] = useState<Job[]>([]);
  const lastRunning = useRef(Date.now());
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const tick = () => {
      fetchJobs().then(
        ({ jobs: next }) => {
          if (!alive) return;
          setJobs(next);
          if (next.some(isRunning)) lastRunning.current = Date.now();
          timer = window.setTimeout(tick, pollInterval(next, Date.now() - lastRunning.current));
        },
        () => {
          if (alive) timer = window.setTimeout(tick, 4000);
        },
      );
    };
    tick();
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);
  return jobs;
}

export function ModelsStep({ picks, eyebrow }: { picks: ModelPicks; eyebrow: string }) {
  // Each of the three is React state for THIS mount and module memory across
  // mounts — the setters below write both, so a remount seeds from what the
  // last one left rather than from scratch.
  const [checked, setCheckedState] = useState<Set<string> | null>(memory.checked);
  const [started, setStartedState] = useState<Map<string, number>>(memory.started);
  const [errors, setErrorsState] = useState<Record<string, string>>(memory.errors);
  const setChecked = (next: Set<string>) => {
    memory.checked = next;
    setCheckedState(next);
  };
  const setStarted = (next: Map<string, number>) => {
    memory.started = next;
    setStartedState(next);
  };
  const setErrors = (next: Record<string, string>) => {
    memory.errors = next;
    setErrorsState(next);
  };

  // The preselection is seeded ONCE, from the first picks that arrive: neither
  // a later render nor a return to this step may re-check a box the user has
  // cleared, which is why `null` (never seeded) and an empty set (seeded, then
  // emptied) are different states here.
  useEffect(() => {
    if (picks && checked === null) setChecked(new Set(comfortableIds(picks)));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `setChecked` is a per-render closure
  }, [picks, checked]);

  const jobs = useJobs();
  // Deliberately NOT `activeJobByModel`: that map drops a terminal row (the
  // stale-busy fix for pages that gate on mere presence), and this step reads
  // the STATE — a `done` row is how a finished download turns into a tick, an
  // `error` row is the only place the failure's sentence exists. Same key
  // (`job.title` is the repo id, `supervisor.load`'s own `title=model`) and
  // the same server-owned filter. Which of those rows this step may believe
  // is `jobFor`'s question, not this map's.
  const jobByModel = useMemo(
    () => new Map(jobs.filter((j) => j.owner === "server").map((j) => [j.title, j])),
    [jobs],
  );

  const selection = checked ?? new Set<string>();
  const total = useMemo(() => selectedTotal(picks ?? [], selection), [picks, selection]);

  // Every row's state, read off its job rather than off what was clicked — so
  // a download that finishes turns into a tick, one that fails gives its
  // checkbox back with the reason beside it, and neither needs the step to
  // still be mounted at the moment it happened. `pending` is what Start would
  // act on: selected, not here already, and nothing in flight for it.
  const rows = (picks ?? []).map((pick) => {
    const job = jobFor(pick.model.id, jobByModel, started);
    const busy = job !== undefined && !isTerminal(job);
    const here = pick.model.downloaded || job?.state === "done";
    return {
      pick,
      job,
      busy,
      here,
      pending: selection.has(pick.model.id) && !busy && !here,
    };
  });
  const pending = rows.filter((r) => r.pending);
  const busyCount = rows.filter((r) => r.busy).length;

  // Driven by the checkbox's own reported value, not by flipping what this
  // render happened to see: the primitive owns the state transition.
  const setRow = (id: string, on: boolean) => {
    const next = new Set(checked ?? []);
    if (on) next.add(id);
    else next.delete(id);
    setChecked(next);
  };

  const start = () => {
    const wanted = pending.map((r) => r.pick);
    if (wanted.length === 0) return;
    // The timestamp is what makes a job row believable afterwards (`jobFor`):
    // a retry rewrites it, so the failed row it replaces stops counting.
    const asked = new Map(started);
    const now = Date.now();
    for (const p of wanted) asked.set(p.model.id, now);
    setStarted(asked);
    // A retry drops the previous sentence for the rows it retries: an old 409
    // printed beside a fresh progress bar reads as a failure that just
    // happened.
    const cleared = { ...errors };
    for (const p of wanted) delete cleared[p.model.id];
    setErrors(cleared);
    // One request per model, each caught on its own: a 409 on one ("needs
    // Apple Silicon", a partly-fetched cache another page is already pulling)
    // must not take the others down with it. Each call starts its own worker
    // thread server-side and returns immediately, and a model already being
    // fetched JOINS that fetch rather than racing it (supervisor.load).
    for (const p of wanted) {
      downloadAiModel(p.model.id, p.capability).catch((e: unknown) => {
        // Through `memory` rather than the wrappers above: this lands after an
        // await, so the closure's `errors`/`started` are a snapshot from
        // before the other requests in this same batch reported.
        const message = e instanceof Error ? e.message : String(e);
        memory.errors = { ...memory.errors, [p.model.id]: message };
        setErrorsState(memory.errors);
        // The ask is withdrawn as well as reported: no job was opened, so the
        // row goes back to being one the user can check and Start again.
        const remaining = new Map(memory.started);
        remaining.delete(p.model.id);
        memory.started = remaining;
        setStartedState(remaining);
      });
    }
  };

  const pendingTotal = selectedTotal(
    picks ?? [],
    pending.map((r) => r.pick.model.id),
  );
  const models = `${pending.length} model${pending.length === 1 ? "" : "s"}`;
  // The figure only when there IS one: every selected model lacking a
  // published size would otherwise put "~0 B" on the button, which is a claim.
  const startLabel =
    pendingTotal.bytes > 0
      ? `Download ${models} · ~${formatSize(pendingTotal.bytes)}`
      : `Download ${models}`;

  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        eyebrow={eyebrow}
        title="AI models run on this machine"
        lead="No account, no API key, no cloud: an app that generates text or images loads a model into this machine's own memory. Download the ones that fit now and they are ready when you need them — skip, and an app fetches what it needs the first time it asks."
      />

      {picks === null ? (
        <div className="flex flex-col gap-3">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-16 w-full rounded-xl" />
          ))}
        </div>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-3 p-0">
          {rows.map((r) => (
            <ModelRow
              key={r.pick.model.id}
              pick={r.pick}
              checked={selection.has(r.pick.model.id)}
              busy={r.busy}
              here={r.here}
              job={r.job}
              error={errors[r.pick.model.id]}
              onToggle={(on) => setRow(r.pick.model.id, on)}
            />
          ))}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={start} disabled={pending.length === 0}>
          {pending.length > 0
            ? startLabel
            : busyCount > 0
              ? "Downloading in the background"
              : rows.length > 0 && rows.every((r) => r.here)
                ? "Every model is already here"
                : "Nothing selected"}
        </Button>
        <span className="text-xs text-muted-foreground">
          {busyCount > 0
            ? "Carry on — the download keeps running while you finish setup, and appears in the download manager."
            : "Downloads run in the background. You can go to the next step straight away."}
        </span>
      </div>

      {total.unknown > 0 && (
        <p className="text-xs text-muted-foreground">
          {total.unknown === 1 ? "One model does" : `${total.unknown} models do`} not publish a
          file size, so {total.unknown === 1 ? "it is" : "they are"} not in the figure above.
        </p>
      )}

      <p className="text-xs text-muted-foreground">
        You can add, swap or delete models any time under AI Models — including a video model,
        which is left out here because it is a far larger download than the rest put together.
      </p>
    </div>
  );
}

function ModelRow({
  pick,
  checked,
  busy,
  here,
  job,
  error,
  onToggle,
}: {
  pick: ModelPick;
  checked: boolean;
  /** A job of this model's is in flight — no checkbox, draw the bar. */
  busy: boolean;
  /** It is on this disk: the catalog said so, or this visit's download said
      `done`. A tick either way. */
  here: boolean;
  job: Job | undefined;
  error: string | undefined;
  onToggle: (on: boolean) => void;
}) {
  const { model } = pick;
  const fit = fitNote(model.fit);
  // `modelSizeLabel`, not a local formatting of `size_gb`: it is the same
  // never-understate reading /ai-models prints (a live download's own total
  // may raise the catalog's constant, a `phase` total may not lower it), and
  // the same em-dash for a model that publishes no size.
  const size = modelSizeLabel(model.size_gb, job);
  // The NICKNAME is the whole name here (`catalog.py`: "Qwen 3.5", not
  // "Qwen3.5 4B (OptiQ 4-bit)"), and the parameter count that /ai-models
  // prints beside it as a chip is left out on purpose — this reader is
  // deciding whether to spend the download, and "4B" is not an input to that
  // (owner, 2026-09-04). Same for the engine: "MLX LM" answers a question
  // only somebody comparing backends is asking.
  const name = model.nickname || model.label;
  // A fresh install builds the runner's environment before any bytes move
  // (`supervisor._env_install_worker`, states `venv` -> `downloading`), so the
  // job's own caption is what makes a row at 0 bytes read as working rather
  // than stuck. `jobAmount` is the byte count beside it, once there is one.
  const caption = job ? [jobStatusLine(job), jobAmount(job)].filter(Boolean).join(" · ") : "";
  const fraction = job ? jobFraction(job) : null;
  // A row shows its failure whether the REQUEST failed (no job was ever
  // opened) or the JOB did — and only for a job this visit is entitled to
  // read, which is `jobFor`'s filter upstream, not a state check here.
  const failure = error || (job?.state === "error" ? job.message || "The download failed." : null);

  return (
    <li className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4">
      <div className="flex items-start gap-3">
        {here ? (
          <span className="mt-0.5 grid size-4 place-items-center rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
            <Check className="size-3" strokeWidth={3} />
          </span>
        ) : busy ? (
          <span className="mt-0.5 grid size-4 place-items-center text-muted-foreground">
            <HardDrive className="size-3.5" />
          </span>
        ) : (
          <Checkbox
            id={`model-${model.id}`}
            className="mt-0.5"
            checked={checked}
            onCheckedChange={(next) => onToggle(!!next)}
          />
        )}
        <div className="min-w-0 flex-1">
          <label
            htmlFor={busy || here ? undefined : `model-${model.id}`}
            className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm font-medium"
          >
            {name}
            <span className="text-xs font-normal text-muted-foreground">{pick.capabilityLabel}</span>
          </label>
          {fit && (
            <div
              className="mt-1 inline-flex items-center gap-1.5 text-xs text-muted-foreground"
              title={fit.title}
            >
              <span className={`size-1.5 rounded-full ${fit.dot}`} />
              {fit.text}
            </div>
          )}
          {model.note && !job && (
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{model.note}</p>
          )}
        </div>

        {/* The size is the number this step is ASKING FOR — disk space and a
            wait — so it gets the right edge and its own weight rather than
            third place in a row of captions. Tabular figures so a column of
            them lines up. */}
        <div className="shrink-0 text-right">
          <div className="text-sm font-semibold tabular-nums">{size}</div>
          <div className="text-xs text-muted-foreground">
            {here ? "already here" : "download"}
          </div>
        </div>
      </div>

      {busy && (
        <div className="flex flex-col gap-1.5">
          <div className="h-1 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-primary transition-[width] duration-500"
              style={{ width: fraction === null ? "15%" : `${Math.round(fraction * 100)}%` }}
            />
          </div>
          <span className="text-xs text-muted-foreground" role="status">
            {caption || "Starting…"}
          </span>
        </div>
      )}

      {failure && (
        <p className="flex items-start gap-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          {failure}
        </p>
      )}
    </li>
  );
}
