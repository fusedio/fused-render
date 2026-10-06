// The persistent surface for the self-update's two decision points. The same
// two moments UpdateNotifier raises as transient notifications — "download it?"
// and "restart for it?" — are easy to miss there, so they also live here, next
// to the version chip, until they are acted on. Deliberately NOT tied to the
// notification's "Later" dismissal: dismissing the popup does not hide this.
import { ArrowUp } from "lucide-react";
import type { MouseEvent } from "react";

import type { UpdateStatus } from "@platform/lib/api";
import { restartInFlight, type RestartStage } from "@platform/lib/restart-flow";
import { requestRestart, useRestartFlow } from "@platform/lib/restart-store";
import { installUpdate, useUpdateStatus } from "@platform/lib/update-status";

/** Whether the chip is showing something the user can act on (or is mid-act):
 *  the collapsed rail mirrors this as a dot. */
export function updateChipActive(status: UpdateStatus | null, stage: RestartStage): boolean {
  if (!status) return false;
  if (status.state === "available") return !status.check_only;
  if (status.state === "installing") return true;
  return status.state === "installed" && (stage === "ready" || restartInFlight(stage));
}

export default function UpdateChip() {
  const status = useUpdateStatus();
  const flow = useRestartFlow();
  if (!status) return null;
  const version = status.latest_version;

  // A click here must not reach the Settings row's own toggle.
  const guard = (fn: () => void) => (e: MouseEvent) => {
    e.stopPropagation();
    fn();
  };

  let label: string;
  let title: string;
  let onClick: ((e: MouseEvent) => void) | undefined;

  if (status.state === "available" && !status.check_only) {
    label = "Update available";
    title = version ? `Download v${version}` : "Download the update";
    onClick = guard(() => void installUpdate(status));
  } else if (status.state === "installing") {
    label = "Updating…";
    title = version ? `Installing v${version}` : "Installing the update";
  } else if (status.state === "installed" && restartInFlight(flow.stage)) {
    label = "Restarting…";
    title = "Restarting fused-render";
  } else if (status.state === "installed" && flow.stage === "ready") {
    label = "Restart to update";
    title = version
      ? `v${version} installed — restart to start using it`
      : "Installed — restart to start using it";
    onClick = guard(() => requestRestart());
  } else {
    return null;
  }

  return (
    <button
      type="button"
      className={"update-chip" + (onClick ? "" : " is-busy")}
      title={title}
      disabled={!onClick}
      onClick={onClick}
    >
      {onClick && <ArrowUp size={10} strokeWidth={2.5} aria-hidden />}
      {label}
    </button>
  );
}
