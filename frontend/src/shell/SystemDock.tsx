// The status bar's System chip: what fused-render's own processes cost right
// now — CPU and memory summed over the app and everything it runs (Claude
// runs, engines, model workers, terminals, installs, /api/run children).
//
// THE CHIP IS FUSED-RENDER'S COST, NOT THE MACHINE'S: `CPU 12% · 1.4 GB` is
// `totals` from the server, so it answers "what is this app costing me". The
// whole-machine figures go in the popover's header, where they give that
// number its context.
//
// Hover previews, click pins (`lib/statusChip.ts`), like every other chip.
// The popover holds a 60 s sparkline of fused-render's CPU, the top five
// processes by CPU, and "Open Monitor" — the whole-machine process monitor
// page at /monitor (shell/monitor/MonitorPage.tsx).
//
// SPLIT INTO A PURE VIEW (`SystemCardView`) AND A STATEFUL WRAPPER, the same
// split ModelsDock uses, so the test renders the view with a fixed payload.
import { useStatusChip, type StatusChipState } from "@platform/lib/statusChip";
import StatusChip from "@platform/ui/StatusChip";
import { isMac } from "@platform/lib/platform";
import { navigateUrl } from "@platform/lib/router";
import type { SystemActivity, SystemProc } from "@platform/lib/sysmon";
import { formatBytes, formatCpu, useSystemActivity } from "@shell/system-lib";
import { Sparkline } from "@shell/monitor/Sparkline";

/** The whole-machine Monitor page's route (App.tsx). */
export const MONITOR_PATH = "/monitor";

export const TOP_N = 5;

/** The chip's text: fused-render's own CPU and memory. */
export function chipLabel(data: SystemActivity | null): string {
  if (!data || !data.supported) return "System";
  const cpu = data.totals.cpuPct === null ? "–" : `${Math.round(data.totals.cpuPct)}%`;
  return `CPU ${cpu} · ${formatBytes(data.totals.memBytes, true)}`;
}

/** "This Mac: CPU 34% · Memory 10.8 of 16 GB". */
export function hostLine(data: SystemActivity | null, machine = isMac ? "This Mac" : "This computer"): string {
  const host = data?.host;
  if (!host) return machine;
  const cpu = host.cpuPct === null ? "–" : `${Math.round(host.cpuPct)}%`;
  const parts = [`CPU ${cpu}`];
  if (host.memUsed !== null && host.memTotal) {
    const gib = 1024 ** 3;
    parts.push(`Memory ${(host.memUsed / gib).toFixed(1)} of ${Math.round(host.memTotal / gib)} GB`);
  }
  return `${machine}: ${parts.join(" · ")}`;
}

/** The busiest processes: CPU first, memory breaks ties (and orders the
 *  first reading, before any CPU is known). */
export function topProcs(procs: SystemProc[], n = TOP_N): SystemProc[] {
  return [...procs]
    .sort((a, b) => (b.cpuPct ?? -1) - (a.cpuPct ?? -1) || (b.memBytes ?? 0) - (a.memBytes ?? 0))
    .slice(0, n);
}

export function SystemCardView({
  data,
  collapsed,
  onToggle,
  pinned = false,
  hostProps,
  onOpenMonitor,
}: {
  data: SystemActivity | null;
  collapsed: boolean;
  onToggle: () => void;
  pinned?: boolean;
  hostProps?: StatusChipState["hostProps"];
  onOpenMonitor: () => void;
}) {
  // Nothing until the first payload says the platform is supported: an idle
  // "System" chip painted while loading would vanish again where it is not.
  if (!data || !data.supported) return null;
  const label = chipLabel(data);
  const top = topProcs(data.procs);
  const cpuNow = data.totals.cpuPct;
  return (
    <div className="dl-host sys-chip" {...hostProps}>
      <StatusChip
        label={label}
        tone="on"
        open={!collapsed}
        pinned={pinned}
        title={collapsed ? "Show what fused-render is using" : "Hide"}
        ariaLabel={`fused-render is using ${label}`}
        onClick={onToggle}
      />
      {!collapsed && (
        <div className="dl-panel sys-panel">
          <div className="sys-host">{hostLine(data)}</div>
          <div className="dl-section">
            <div className="dl-section-head">
              fused-render CPU · last minute{" "}
              <span className="dl-section-count">{formatCpu(cpuNow)}</span>
            </div>
            <div className="sys-spark-wrap">
              <Sparkline
                points={data.history.map((h) => ({ t: h.t, v: h.appCpuPct }))}
                label="fused-render CPU, last 60 seconds"
              />
            </div>
          </div>
          <div className="dl-section">
            <div className="dl-section-head">
              Processes <span className="dl-section-count">{data.procs.length}</span>
            </div>
            <div className="sys-rows" role="table" aria-label="Top processes by CPU">
              {top.map((p) => (
                <div className="sys-row" role="row" key={p.pid}>
                  <span className="sys-name" role="cell" title={`${p.label} — pid ${p.pid}`}>
                    {p.label}
                  </span>
                  <span className="sys-num" role="cell">
                    {formatCpu(p.cpuPct)}
                  </span>
                  <span className="sys-num sys-mem" role="cell">
                    {formatBytes(p.memBytes)}
                  </span>
                </div>
              ))}
              {!top.length && <div className="dl-panel-empty">Reading…</div>}
            </div>
          </div>
          <div className="sys-foot">
            <a
              className="sys-link"
              href={MONITOR_PATH}
              onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                e.preventDefault();
                onOpenMonitor();
              }}
            >
              Open Monitor
            </a>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SystemDock() {
  const chip = useStatusChip("system");
  const data = useSystemActivity(chip.open);
  const onOpenMonitor = () => {
    chip.close();
    navigateUrl(MONITOR_PATH);
  };
  return (
    <SystemCardView
      data={data}
      collapsed={!chip.open}
      onToggle={chip.toggle}
      pinned={chip.pinned}
      hostProps={chip.hostProps}
      onOpenMonitor={onOpenMonitor}
    />
  );
}
