// The status bar's Updates chip: the self-update, at the bar's LEFT end just
// before the System chip (CPU · memory). Akshil, 2026-10-08: "let's show it on
// the bottom left side, left side of where we show cpu and memory usage. let's
// show a check for updates button, version number and status for download
// update restart etc the whole thing."
//
// WHY A CHIP AT ALL when the sidebar already carries `UpdateCard` and
// Preferences › Updates the manual check: Fused Bot (platform/lib/flavor) has
// NO sidebar — the Bots page is the whole window — so until this chip the only
// way to learn the version or press "Check for updates" there was the native
// menu's Preferences item. Both flavors get the chip (owner's call) so the
// bottom-left reads the same in either app; Render's sidebar card stays, it is
// the decision surface for a found update and this chip only mirrors it.
//
// THE CHIP'S LABEL IS THE STATE, one line: "v0.6.22" at rest, "Update
// available", "Downloading" over a progress line, "Installing", "Restart to
// update", "Restarting…", "Update failed" (red). NEXT TO IT IN THE BAR, not
// inside the popover (Akshil: "check for updates button should be outside the
// popover next to version number"), the Check for updates button — its own
// phases "Checking…" / "Up to date" / "Couldn't check" for a few seconds after
// a press; hidden while a found update is pending its decision, disabled with
// a tooltip on a build with no updater (a dev run). The POPOVER holds the
// rest: the running version, the state's detail sentence and its action
// (Download / Restart now / Try again — the same `installUpdate` /
// `requestRestart` paths UpdateCard takes), and the auto-download toggle
// Preferences also offers.
//
// THE CHECK BUTTON IS AN ICON ALONE, BEFORE THE VERSION (Akshil: "prefix icon only button, on
// hover instant tooltip") — a refresh glyph with the instant `data-hint`
// tooltip (platform/lib/hints.ts; a native `title` waits seconds on the
// first hover). The press's answer has no words of its own to show, so the
// glyph says it: spinning while checking, a tick for "Up to date", a cross
// for "Couldn't check", each with the matching hint, for the few seconds the
// phase holds.
//
// SPLIT INTO A PURE VIEW (`UpdatesCardView`) AND A STATEFUL WRAPPER, the
// SystemDock/ModelsDock split, so the test renders the view with a fixed
// status and no poll.
import { AlertCircle, Check, Download, Loader2, RefreshCw, RotateCcw, X } from "lucide-react";
import { useEffect, useState } from "react";

import { getPrefs, putAutoDownloadUpdates, type UpdateStatus } from "@platform/lib/api";
import { restartInFlight, restartStageLabel, type RestartStage } from "@platform/lib/restart-flow";
import { requestRestart, useRestartFlow } from "@platform/lib/restart-store";
import { useStatusChip, type StatusChipState } from "@platform/lib/statusChip";
import { useManualUpdateCheck } from "@platform/lib/update-check";
import { checkNowLabel, installUpdate, updateRelevant, useUpdateStatus, type ManualCheckPhase } from "@platform/lib/update-status";
import StatusChip, { type ChipTone } from "@platform/ui/StatusChip";

/** The disabled Check button's hint on a build with no updater. */
export const NO_UPDATER_HINT = "Updates aren’t managed from inside the app on this build — a packaged Fused app updates itself; a dev run does not.";

export interface UpdatesChip {
  label: string;
  tone: ChipTone;
  /** A fraction while a download streams, null for an indeterminate bar, absent otherwise. */
  progress?: number | null;
}

/** What the chip reads for a given state. Pure so the wording is tested once. */
export function updatesChip(
  status: UpdateStatus | null,
  stage: RestartStage,
  version: string | null | undefined,
  phase: ManualCheckPhase = "rest",
): UpdatesChip {
  // `phase` only matters for the progress line while a press is in flight;
  // the words for a press live on the Check button beside the chip.
  const rest = version ? `v${version}` : "Updates";
  if (!status) return { label: rest, tone: "idle" };
  if (status.state === "available") return { label: "Update available", tone: "on" };
  if (status.state === "installing") {
    if (status.phase === "installing") return { label: "Installing", tone: "on", progress: null };
    const fraction =
      status.progress !== null && status.progress_total ? Math.min(1, status.progress / status.progress_total) : null;
    return { label: "Downloading", tone: "on", progress: fraction };
  }
  if (status.state === "installed") {
    if (restartInFlight(stage)) return { label: "Restarting…", tone: "on", progress: null };
    if (stage === "ready") return { label: "Restart to update", tone: "on" };
    return { label: rest, tone: "idle" };
  }
  if (status.state === "error") return { label: "Update failed", tone: "failure" };
  if (phase === "checking" || status.state === "checking") return { label: rest, tone: "idle", progress: null };
  return { label: rest, tone: "idle" };
}

/** The popover's state sentence and its one action, UpdateCard's own copy. */
export function updatesDetail(
  status: UpdateStatus | null,
  stage: RestartStage,
): { title: string; detail: string; action: { label: string; run: () => void } | null; error?: boolean; busy?: boolean } | null {
  if (!status) return null;
  const v = status.latest_version ? `v${status.latest_version}` : "The update";
  if (status.state === "available") {
    return status.check_only
      ? { title: "Update available", detail: `${v} is out. This build does not install updates itself.`, action: null }
      : { title: "Update available", detail: `${v} is ready to download.`, action: { label: "Download", run: () => void installUpdate(status) } };
  }
  if (status.state === "installing") {
    const phase = status.phase === "installing" ? `Installing ${v}…` : `Downloading ${v}…`;
    return { title: status.phase === "installing" ? "Installing update" : "Downloading update", detail: phase, action: null, busy: true };
  }
  if (status.state === "installed" && restartInFlight(stage)) {
    return { title: "Restarting…", detail: restartStageLabel(stage), action: null, busy: true };
  }
  if (status.state === "installed" && stage === "ready") {
    return { title: "Update ready", detail: `${v} is installed. Restart to start using it.`, action: { label: "Restart now", run: () => requestRestart() } };
  }
  if (status.state === "error") {
    return { title: "Update failed", detail: status.error || "Couldn't reach the update server.", action: { label: "Try again", run: () => void installUpdate(status) }, error: true };
  }
  return null;
}

function formatBytesShort(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  return `${Math.round(n / 1024 ** 2)} MB`;
}

/** "12 of 180 MB" / "12 MB" while a download streams, else "". */
export function downloadProgressLine(status: UpdateStatus | null): string {
  if (!status || status.state !== "installing" || status.phase === "installing" || status.progress === null) return "";
  if (status.progress_total) return `${formatBytesShort(status.progress)} of ${formatBytesShort(status.progress_total)}`;
  return formatBytesShort(status.progress);
}

export function UpdatesCardView({
  status,
  hasUpdater = status !== null,
  stage,
  version,
  phase,
  onCheck,
  autoDownload,
  autoBusy,
  autoError,
  onToggleAuto,
  collapsed,
  onToggle,
  pinned = false,
  hostProps,
}: {
  status: UpdateStatus | null;
  /** Whether this build has an updater at all (`/api/config` carries `update`
   *  only then). Decided from the boot config, not from `status === null`:
   *  the store is null until its first poll too, and a cold start must not
   *  flash "not managed on this build" for that second. */
  hasUpdater?: boolean;
  stage: RestartStage;
  version: string | null | undefined;
  phase: ManualCheckPhase;
  onCheck: () => void;
  /** null while the prefs read is still out; the row renders disabled. */
  autoDownload: boolean | null;
  autoBusy?: boolean;
  autoError?: string | null;
  onToggleAuto: () => void;
  collapsed: boolean;
  onToggle: () => void;
  pinned?: boolean;
  hostProps?: StatusChipState["hostProps"];
}) {
  const chip = updatesChip(status, stage, version, phase);
  const detail = updatesDetail(status, stage);
  const running = version ? `Running v${version}` : "Version unknown";
  const progressLine = downloadProgressLine(status);
  // The Check button leaves the bar while a found update waits on its
  // decision (download? restart?) — pressing it then could only contradict
  // the chip beside it with "Up to date".
  const showCheck = !updateRelevant(status);
  // The Check button is a SIBLING of the chip's hover host, not a child:
  // inside it, hovering the icon would open the popover under the tooltip.
  // It comes FIRST — a prefix to the version (Akshil, 2026-10-08).
  return (
    <>
      {showCheck && (
        <button
          type="button"
          className={"upd-check-btn" + (phase !== "rest" ? " is-" + phase : "")}
          disabled={!hasUpdater || phase === "checking"}
          data-hint={hasUpdater ? checkNowLabel(phase, version) : NO_UPDATER_HINT}
          aria-label={checkNowLabel(phase, version)}
          onClick={onCheck}
        >
          {phase === "checking" ? <Loader2 size={13} className="update-card-spin" aria-hidden />
            : phase === "current" ? <Check size={13} aria-hidden />
            : phase === "failed" ? <X size={13} aria-hidden />
            : <RefreshCw size={13} aria-hidden />}
        </button>
      )}
    <div className="dl-host upd-chip" {...hostProps}>
      <StatusChip
        label={chip.label}
        tone={chip.tone}
        progress={chip.progress}
        open={!collapsed}
        pinned={pinned}
        title={collapsed ? `${running} · updates` : "Hide"}
        ariaLabel={`Updates: ${chip.label}`}
        onClick={onToggle}
      />
      {!collapsed && (
        <div className="dl-panel upd-panel" role="status">
          <div className="upd-head">{running}</div>
          {detail ? (
            <div className={"upd-state" + (detail.error ? " is-error" : "") + (detail.busy ? " is-busy" : "")}>
              <div className="upd-state-title">
                {detail.error ? <AlertCircle size={13} aria-hidden /> : detail.busy ? <Loader2 size={13} className="update-card-spin" aria-hidden /> : status?.state === "installed" ? <RotateCcw size={13} aria-hidden /> : <Download size={13} aria-hidden />}
                {detail.title}
              </div>
              <div className="upd-state-detail">{detail.detail}</div>
              {progressLine && <div className="upd-state-detail upd-progress">{progressLine}</div>}
              {detail.busy && <div className="update-card-bar" aria-hidden />}
              {detail.action && (
                <div className="update-card-actions">
                  <button type="button" className="update-card-btn" onClick={detail.action.run}>
                    {detail.action.label}
                  </button>
                </div>
              )}
            </div>
          ) : hasUpdater ? (
            <div className={"upd-state-detail" + (phase === "failed" ? " is-error" : "")}>
              {phase === "checking" ? "Checking…"
                : phase === "current" ? "You have the latest version."
                : phase === "failed" ? "Couldn't check for updates — offline, or the update server didn't answer."
                : "No update is waiting."}
            </div>
          ) : (
            <div className="upd-state-detail upd-none">Updates aren&rsquo;t managed from inside the app on this build &mdash; a packaged Fused app updates itself; a dev run does not.</div>
          )}
          {hasUpdater && (
            <label className="upd-auto">
              <input
                type="checkbox"
                checked={autoDownload === true}
                disabled={autoDownload === null || !!autoBusy}
                onChange={onToggleAuto}
              />
              <span>Automatically download updates</span>
            </label>
          )}
          {autoError && <div className="upd-state-detail is-error">{autoError}</div>}
        </div>
      )}
    </div>
    </>
  );
}

export default function UpdatesDock({ version, hasUpdater }: { version: string | null | undefined; hasUpdater: boolean }) {
  const chip = useStatusChip("updates");
  const status = useUpdateStatus();
  const flow = useRestartFlow();
  const { phase, check } = useManualUpdateCheck(status);
  // The auto-download preference is read on EACH open of the panel, never at
  // rest: Preferences can flip it behind this chip's back, and a reopen is the
  // moment the reader looks. The last value stays on screen while the re-read
  // is out, so the row never blinks.
  const [autoDownload, setAutoDownload] = useState<boolean | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);
  const [autoError, setAutoError] = useState<string | null>(null);
  useEffect(() => {
    if (!chip.open || !hasUpdater) return;
    let cancelled = false;
    void getPrefs()
      .then((p) => {
        if (!cancelled) setAutoDownload(p.update?.auto_download === true);
      })
      .catch(() => {
        if (!cancelled) setAutoDownload((v) => v ?? false);
      });
    return () => {
      cancelled = true;
    };
  }, [chip.open, hasUpdater]);
  const toggleAuto = async () => {
    if (autoBusy || autoDownload === null) return;
    setAutoBusy(true);
    setAutoError(null);
    try {
      const p = await putAutoDownloadUpdates(!autoDownload);
      setAutoDownload(p.update?.auto_download === true);
    } catch (e) {
      setAutoError((e as Error).message);
    } finally {
      setAutoBusy(false);
    }
  };
  // Unpacked dev runs and non-mac builds have no updater (`hasUpdater` false):
  // the chip still shows the version — that is half of what it is for — with
  // the "not managed here" line behind it.
  return (
    <UpdatesCardView
      status={status}
      hasUpdater={hasUpdater}
      stage={flow.stage}
      version={version}
      phase={phase}
      onCheck={() => void check()}
      autoDownload={autoDownload}
      autoBusy={autoBusy}
      autoError={autoError}
      onToggleAuto={() => void toggleAuto()}
      collapsed={!chip.open}
      onToggle={chip.toggle}
      pinned={chip.pinned}
      hostProps={chip.hostProps}
    />
  );
}
