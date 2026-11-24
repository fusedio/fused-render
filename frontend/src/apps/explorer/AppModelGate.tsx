// First-open overview of the AI models an app declares in its pyproject
// (`[tool.fused-render.app.models]`). Wraps the app's view: holds the iframe
// back while the overview loads, shows a review dialog until the user accepts,
// then a progress dialog while the missing models download, and opens the app
// once they are on disk. Declining records nothing, so the next open asks again.
//
// Fails OPEN on every error: an overview that cannot be read must never keep an
// app from opening. Hooks stay above every early return.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Cloud, Cpu, Download, Sparkles } from "lucide-react";
import { Modal } from "@platform/ui/modal/Modal";
import { AppIcon, StatusCallout } from "@platform/ui/appstore";
import {
  acceptAppModels,
  downloadAiModel,
  fetchAppModelOverview,
  type AppModelOverview,
  type AppModelRow,
} from "@platform/lib/api";
import { cancelJob, fetchJobs, type Job } from "@platform/lib/jobs";
import { IS_PREVIEW, IS_SNAPSHOT } from "@platform/lib/router";
import {
  chosenRow,
  footerHint,
  noneRunning,
  pendingRows,
  rowDetail,
  rowProgress,
  rowValue,
  substitutionsOf,
  formatGb,
} from "./appModelGateLogic";

type Phase = "loading" | "ready" | "review" | "progress";

const HTML_ENTRY = /\.html?$/i;
const POLL_MS = 1500;

export function AppModelGate({
  path,
  children,
  onDecline,
  pollMs = POLL_MS,
}: {
  path: string;
  children: ReactNode;
  /** Called when the user turns the overview down; the host decides where the
   *  user goes. Nothing is recorded. */
  onDecline: () => void;
  pollMs?: number;
}) {
  const skip = IS_PREVIEW || IS_SNAPSHOT || !HTML_ENTRY.test(path);
  const [phase, setPhase] = useState<Phase>(skip ? "ready" : "loading");
  const [overview, setOverview] = useState<AppModelOverview | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [jobIds, setJobIds] = useState<Record<string, string>>({});
  const [jobs, setJobs] = useState<Job[]>([]);
  const [hidden, setHidden] = useState(false);
  const [declined, setDeclined] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  // None of the tracked jobs is running yet the weights are not on disk.
  const [stalled, setStalled] = useState(false);
  const choicesRef = useRef(choices);
  choicesRef.current = choices;
  const opening = useRef(false);

  useEffect(() => {
    if (skip) return;
    let cancelled = false;
    setPhase("loading");
    fetchAppModelOverview(path).then(
      (ov) => {
        if (cancelled) return;
        if (!ov.declares || ov.accepted || !ov.rows?.length) {
          setPhase("ready");
          return;
        }
        setOverview(ov);
        setChoices(Object.fromEntries(ov.rows.map((r) => [r.capability, r.choice])));
        setJobIds(Object.fromEntries(ov.rows.filter((r) => r.jobId).map((r) => [r.model, r.jobId as string])));
        setPhase("review");
      },
      () => {
        if (!cancelled) setPhase("ready");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [path, skip]);

  const decline = useCallback(() => {
    setDeclined(true);
    onDecline();
  }, [onDecline]);

  // While downloading: poll the jobs list; once nothing tracked is running,
  // re-read the overview and open the app if every model is on disk.
  const tracked = Object.values(jobIds).join("|");
  useEffect(() => {
    if (phase !== "progress") return;
    let stopped = false;
    const tick = async () => {
      try {
        const snap = await fetchJobs();
        if (stopped) return;
        setJobs(snap.jobs);
        if (!noneRunning(tracked ? tracked.split("|") : [], snap.jobs)) {
          setStalled(false);
          return;
        }
        const ov = await fetchAppModelOverview(path);
        if (stopped) return;
        if (ov.rows) {
          setOverview(ov);
          if (pendingRows(ov.rows, choicesRef.current).length === 0) {
            // Every download finished: the app opens now, so record acceptance now.
            if (opening.current) return;
            opening.current = true;
            try {
              await acceptAppModels(path, substitutionsOf(ov.rows, choicesRef.current));
            } catch {
              /* fail open: the app opens, the next open asks again */
            }
            if (!stopped) setPhase("ready");
          } else setStalled(true);
        }
      } catch {
        /* a missed poll is retried on the next tick */
      }
    };
    void tick();
    const id = setInterval(tick, pollMs);
    return () => {
      stopped = true;
      clearInterval(id);
    };
  }, [phase, tracked, path, pollMs]);

  const start = useCallback(
    async (rows: AppModelRow[]) => {
      const picks = choicesRef.current;
      setBusy(true);
      setFailed(null);
      try {
        const todo = pendingRows(rows, picks);
        if (todo.length === 0) {
          // Nothing to download: the app opens now, so record acceptance now.
          await acceptAppModels(path, substitutionsOf(rows, picks));
          setPhase("ready");
          return;
        }
        const started: Record<string, string> = {};
        for (const row of todo) {
          const res = await downloadAiModel(row.model, row.capability);
          if (res?.jobId) started[row.model] = res.jobId;
        }
        setJobIds((prev) => ({ ...prev, ...started }));
        setHidden(false);
        setPhase("progress");
      } catch (e) {
        setFailed(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [path],
  );

  const cancelAll = useCallback(async () => {
    await Promise.allSettled(Object.values(jobIds).map((id) => cancelJob(id)));
    setPhase("review");
    decline();
  }, [jobIds, decline]);

  if (phase === "ready") return <>{children}</>;
  if (phase === "loading" || !overview?.rows) return null;

  const rows = overview.rows;
  const appName = overview.appName ?? "This app";

  if (declined && phase === "review") {
    return (
      <div className="am-held">
        <p>This app needs its AI models reviewed before it opens.</p>
        <button type="button" className="btn btn-primary" onClick={() => setDeclined(false)}>
          Review models
        </button>
      </div>
    );
  }

  if (phase === "progress" && hidden) {
    return (
      <div className="am-held">
        <p>Downloading AI models for {appName}. It opens when they are ready.</p>
        <button type="button" className="btn btn-secondary" onClick={() => setHidden(false)}>
          Show progress
        </button>
      </div>
    );
  }

  const none = pendingRows(rows, choices).length === 0;
  const swapNotes = rows.filter((r) => choices[r.capability] && choices[r.capability] !== r.model);

  if (phase === "review") {
    return (
      <Modal
        title="Before you open"
        onClose={decline}
        width={440}
        dialogClassName="mp-sheet"
        busy={busy}
        footer={
          <>
            <span className="am-footer-hint">{footerHint(rows, choices, overview.etaMin)}</span>
            <button type="button" className="btn btn-secondary" onClick={decline} disabled={busy}>
              Not now
            </button>
            <button type="button" className="btn btn-primary" onClick={() => void start(rows)} disabled={busy}>
              {!none && <Download size={14} strokeWidth={2.2} aria-hidden="true" />}
              {none ? "Open" : "Download"}
            </button>
          </>
        }
      >
        <SheetHead appName={appName} appDir={overview.appDir} meta={overview.appDir ?? ""} />
        <p className="mp-sheet-lede">
          {none
            ? "Everything this app uses is ready. Nothing will download."
            : `This app runs ${rows.length === 1 ? "an AI model" : `${rows.length} AI models`} on this Mac. Nothing downloads until you continue.`}
        </p>
        <ul className="as-trust-list">
          {rows.map((row) => {
            const pick = chosenRow(row, choices[row.capability] ?? row.model);
            const shown = { ...row, label: pick.label, sizeGb: pick.sizeGb, downloaded: row.downloaded || pick.id !== row.model };
            return (
              <ModelRow
                key={row.capability}
                row={shown}
                detail={
                  <>
                    {rowDetail(row)}
                    {row.candidates.length > 0 && (
                      <ChangeLink row={row} choice={pick.id} onPick={(id) => setChoices((c) => ({ ...c, [row.capability]: id }))} />
                    )}
                  </>
                }
              />
            );
          })}
        </ul>
        {swapNotes.map((r) => (
          <StatusCallout
            key={r.capability}
            tone="warn"
            className="am-callout"
            title={`${appName} was built with ${r.label}. Results may differ.`}
          />
        ))}
        {failed && (
          <StatusCallout tone="warn" className="am-callout" title={failed} />
        )}
      </Modal>
    );
  }

  // progress
  return (
    <Modal
      title="Getting ready"
      onClose={() => setHidden(true)}
      width={440}
      dialogClassName="mp-sheet"
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={() => void cancelAll()}>
            Cancel download
          </button>
          <button type="button" className="btn btn-primary" onClick={() => setHidden(true)}>
            Hide
          </button>
        </>
      }
    >
      <SheetHead appName={appName} appDir={overview.appDir} meta="Opens when the download finishes" />
      <p className="mp-sheet-lede">You can hide this and keep working.</p>
      <ul className="as-trust-list">
        {rows.map((row) => {
          const swapped = !!choices[row.capability] && choices[row.capability] !== row.model;
          const progress = rowProgress(swapped ? { ...row, downloaded: true } : row, jobIds[row.model] ?? null, jobs);
          return (
            <ProgressRow
              key={row.capability}
              row={row}
              progress={progress}
              stalled={stalled}
              onRetry={() => {
                setStalled(false);
                void downloadAiModel(row.model, row.capability).then((res) => {
                  if (res?.jobId) setJobIds((prev) => ({ ...prev, [row.model]: res.jobId }));
                });
              }}
            />
          );
        })}
      </ul>
    </Modal>
  );
}

function SheetHead({ appName, appDir, meta }: { appName: string; appDir?: string; meta: string }) {
  return (
    <div className="mp-sheet-head">
      <AppIcon src={null} name={appName} seed={appDir ?? appName} size={56} />
      <div className="mp-sheet-id">
        <div className="mp-sheet-name">{appName}</div>
        <div className="mp-sheet-meta">{meta}</div>
      </div>
    </div>
  );
}

function glyphOf(row: AppModelRow) {
  if (row.provider === "cloud") return <Cloud size={15} aria-hidden="true" />;
  if (row.provider === "apple") return <Sparkles size={15} aria-hidden="true" />;
  return <Cpu size={15} aria-hidden="true" />;
}

function ModelRow({ row, detail }: { row: AppModelRow; detail: ReactNode }) {
  const ok = row.provider !== "cloud" && row.downloaded;
  return (
    <li className={"as-trust-item " + (ok ? "is-ok" : "is-default")}>
      <span className="as-trust-glyph" aria-hidden="true">
        {glyphOf(row)}
      </span>
      <span className="as-trust-copy">
        <span className="as-trust-title">{row.label}</span>
        <span className="as-trust-detail">{detail}</span>
      </span>
      <span className="am-row-value">{rowValue(row)}</span>
    </li>
  );
}

function ChangeLink({
  row,
  choice,
  onPick,
}: {
  row: AppModelRow;
  choice: string;
  onPick: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <>
        {" · "}
        <button type="button" className="am-change" onClick={() => setOpen(true)}>
          Change
        </button>
      </>
    );
  }
  return (
    <>
      {" · "}
      <select
        className="am-select"
        aria-label={`Model for ${row.use}`}
        value={choice}
        onChange={(e) => {
          onPick(e.target.value);
          setOpen(false);
        }}
      >
        <option value={row.model}>{row.label}</option>
        {row.candidates.map((c) => (
          <option key={c.id} value={c.id}>
            {c.label}
          </option>
        ))}
      </select>
    </>
  );
}

function ProgressRow({
  row,
  progress,
  stalled,
  onRetry,
}: {
  row: AppModelRow;
  progress: ReturnType<typeof rowProgress>;
  stalled: boolean;
  onRetry: () => void;
}) {
  const failed = progress.state === "error" || (progress.state === "waiting" && stalled);
  const ready = progress.state === "ready";
  const pct = progress.state === "running" ? progress.pct : null;
  const detail =
    progress.state === "error"
      ? progress.message
      : failed
        ? "Download did not start"
        : progress.state === "running" && progress.done != null && progress.total
        ? `${formatGb(progress.done / 1e9)} of ${formatGb(progress.total / 1e9)}`
        : row.use;
  return (
    <li className={"as-trust-item " + (failed ? "is-caution" : ready ? "is-ok" : "is-default")}>
      <span className="as-trust-glyph" aria-hidden="true">
        {glyphOf(row)}
      </span>
      <span className="as-trust-copy">
        <span className="as-trust-title">{row.label}</span>
        <span className="as-trust-detail">{detail}</span>
        {!ready && !failed && (
          <span className="am-progress" role="progressbar" aria-valuenow={pct ?? undefined}>
            <span className="am-progress-bar" style={{ width: pct == null ? "30%" : `${pct}%` }} />
          </span>
        )}
      </span>
      {failed ? (
        <button type="button" className="am-change" onClick={onRetry}>
          Retry
        </button>
      ) : (
        <span className="am-row-value">{ready ? "On this Mac" : pct == null ? "" : `${pct}%`}</span>
      )}
    </li>
  );
}
