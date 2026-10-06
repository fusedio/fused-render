// The persistent surface for the self-update's decision points: a card above
// the Settings row. The same moments UpdateNotifier raises as transient
// notifications are easy to miss there, so they also live here until acted on.
// Deliberately NOT tied to the notification's "Later": that dismisses the
// popup only. This card's own "Later" collapses it to a one-line row for the
// session; it never disappears while an update is waiting.
import { AlertCircle, Download, Loader2, RotateCcw } from "lucide-react";
import { useState, type ReactNode } from "react";

import type { UpdateStatus } from "@platform/lib/api";
import { restartInFlight, restartStageLabel, type RestartStage } from "@platform/lib/restart-flow";
import { requestRestart, useRestartFlow } from "@platform/lib/restart-store";
import { installUpdate, useUpdateStatus } from "@platform/lib/update-status";

/** Whether the card is showing something (the collapsed rail mirrors it as a dot). */
export function updateCardActive(status: UpdateStatus | null, stage: RestartStage): boolean {
  if (!status) return false;
  if (status.state === "available") return !status.check_only;
  if (status.state === "installing" || status.state === "error") return true;
  return status.state === "installed" && (stage === "ready" || restartInFlight(stage));
}

const collapsedKey = (version: string | null | undefined) => `fr.updateCard.collapsed.${version ?? ""}`;

function readCollapsed(version: string | null | undefined): boolean {
  try {
    return sessionStorage.getItem(collapsedKey(version)) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(version: string | null | undefined) {
  try {
    sessionStorage.setItem(collapsedKey(version), "1");
  } catch {
    /* storage unavailable: the collapse just lasts until unmount */
  }
}

export default function UpdateCard() {
  const status = useUpdateStatus();
  const flow = useRestartFlow();
  const [laterFor, setLaterFor] = useState<string | null>(null);
  if (!status) return null;
  const version = status.latest_version;
  const v = version ? `v${version}` : "The update";

  let tone = "";
  let icon: ReactNode;
  let title: string;
  let detail: string;
  let progress = false;
  let primary: { label: string; run: () => void } | null = null;
  let later = false;

  if (status.state === "available" && !status.check_only) {
    icon = <Download size={13} aria-hidden />;
    title = "Update available";
    detail = `${v} is ready to download.`;
    primary = { label: "Download", run: () => void installUpdate(status) };
  } else if (status.state === "installing") {
    tone = " is-busy";
    icon = <Loader2 size={13} className="update-card-spin" aria-hidden />;
    title = "Downloading update";
    detail = `Downloading ${v}…`;
    progress = true;
  } else if (status.state === "installed" && restartInFlight(flow.stage)) {
    tone = " is-busy";
    icon = <Loader2 size={13} className="update-card-spin" aria-hidden />;
    title = "Restarting…";
    detail = restartStageLabel(flow.stage);
  } else if (status.state === "installed" && flow.stage === "ready") {
    if (laterFor === (version ?? "") || readCollapsed(version)) {
      return (
        <button
          type="button"
          className="update-card-compact"
          title={`${v} installed. Restart to start using it`}
          onClick={() => requestRestart()}
        >
          <RotateCcw size={13} aria-hidden />
          <b>Restart to update</b>
          {version && <span className="update-card-ver">v{version}</span>}
        </button>
      );
    }
    icon = <RotateCcw size={13} aria-hidden />;
    title = "Update ready";
    detail = `${v} is installed. Restart to start using it.`;
    primary = { label: "Restart now", run: () => requestRestart() };
    later = true;
  } else if (status.state === "error") {
    tone = " is-error";
    icon = <AlertCircle size={13} aria-hidden />;
    title = "Update failed";
    detail = status.error || "Couldn't reach the update server.";
    primary = { label: "Try again", run: () => void installUpdate(status) };
  } else {
    return null;
  }

  return (
    <div className={"update-card" + tone} role="status">
      <div className="update-card-title">
        {icon}
        {title}
      </div>
      <div className="update-card-detail">{detail}</div>
      {progress && <div className="update-card-bar" aria-hidden />}
      {primary && (
        <div className="update-card-actions">
          <button type="button" className="update-card-btn" onClick={primary.run}>
            {primary.label}
          </button>
          {later && (
            <button
              type="button"
              className="update-card-ghost"
              onClick={() => {
                writeCollapsed(version);
                setLaterFor(version ?? "");
              }}
            >
              Later
            </button>
          )}
        </div>
      )}
    </div>
  );
}
