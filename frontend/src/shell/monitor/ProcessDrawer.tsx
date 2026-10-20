// The Processes tab's right-hand detail drawer for one process: who owns it,
// its command line and working directory, the chain of parents, the venv it
// runs in and the ports it listens on (`GET /api/system/activity/detail`,
// fused_render/sysmon/detail.py). Opened by clicking a row's name; the row's
// own figures come from the live snapshot, so the drawer only fetches what the
// snapshot lacks, once per pid.
import { useEffect, useState } from "react";
import { formatBytes, formatCpu, type MachineProc } from "@platform/lib/sysmon";
import type { ProcessDetail } from "./monitor-api";

export default function ProcessDrawer({
  pid,
  proc,
  load,
  onClose,
}: {
  pid: number;
  proc: MachineProc | null;
  load: (pid: number) => Promise<ProcessDetail>;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ProcessDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setDetail(null);
    setError(null);
    load(pid).then(
      (d) => live && setDetail(d),
      (e: Error) => live && setError(e.message || "Could not read this process"),
    );
    return () => {
      live = false;
    };
  }, [pid, load]);

  const title = proc ? (proc.fused ? proc.label : proc.command) : `pid ${pid}`;
  return (
    <aside className="monitor-drawer" aria-label={`Details for ${title}`}>
      <div className="monitor-drawer-head">
        <h2 className="monitor-drawer-title">{title}</h2>
        <button type="button" className="monitor-link-btn" aria-label="Close details" onClick={onClose}>
          Close
        </button>
      </div>
      {proc && (
        <p className="monitor-drawer-figures">
          pid {proc.pid} · {formatCpu(proc.cpuPct)} CPU · {formatBytes(proc.memBytes)}
        </p>
      )}
      {error && <p className="monitor-denied">{error}</p>}
      {!detail && !error && <p className="monitor-drawer-note">Reading…</p>}
      {detail && (
        <dl className="monitor-drawer-list">
          <dt>Owner</dt>
          <dd>{detail.owner.label}</dd>
          <dt>Command</dt>
          <dd className="monitor-drawer-mono">{detail.argv.join(" ") || "unknown"}</dd>
          <dt>Folder</dt>
          <dd className="monitor-drawer-mono">{detail.cwd ?? "unknown"}</dd>
          {detail.venv && (
            <>
              <dt>Python env</dt>
              <dd className="monitor-drawer-mono">{detail.venv}</dd>
            </>
          )}
          <dt>Started by</dt>
          <dd>
            {detail.parents.length
              ? detail.parents.map((p) => `${p.label} (${p.pid})`).join(" › ")
              : "nothing we can see"}
          </dd>
          <dt>Ports</dt>
          <dd className="monitor-drawer-mono">
            {detail.ports.length ? detail.ports.map((x) => JSON.stringify(x)).join(", ") : "none"}
          </dd>
        </dl>
      )}
    </aside>
  );
}
