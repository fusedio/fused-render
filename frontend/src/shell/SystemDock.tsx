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
// The popover holds the verdict (only when not ok: the reasons as links
// into the Monitor), the Claude CLI line, one row of three stats (requests,
// browser wait, server lag), the top five processes by CPU (before the dashboard loads, the 60 s CPU sparkline stands in for the stats), "Record 60s" and "Open Monitor" — the process monitor page at /monitor
// (shell/monitor/MonitorPage.tsx).
//
// THE CHIP MAKES NO EXTRA REQUESTS: the verdict dot and the Claude line ride on
// `/api/system/activity`. Only the OPEN popover also polls
// `/api/system/dashboard` (2 s), for the reasons and the three stats.
//
// SPLIT INTO A PURE VIEW (`SystemCardView`) AND A STATEFUL WRAPPER, the same
// split ModelsDock uses, so the test renders the view with a fixed payload.
import { useStatusChip, type StatusChipState } from "@platform/lib/statusChip";
import StatusChip from "@platform/ui/StatusChip";
import { isMac } from "@platform/lib/platform";
import { navigateUrl } from "@platform/lib/router";
import { useEffect, useRef, useState, type MouseEvent } from "react";
import type { SystemActivity, SystemProc } from "@platform/lib/sysmon";
import { formatBytes, formatCpu, useSystemActivity } from "@shell/system-lib";
import { Sparkline } from "@shell/monitor/Sparkline";
import { getDashboard, sectionOk, type Dashboard } from "@shell/monitor/monitor-api";
import { claudeLine, overviewTiles } from "@shell/monitor/monitor-net-lib";
import { useRecord } from "@shell/monitor/useRecord";
import { usePolled } from "@shell/monitor/usePolled";

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

const msText = (n: number | null | undefined) => (n == null ? "–" : `${Math.round(n)} ms`);

/** The popover's three stats: requests/s, browser wait (p95), server lag, each
 *  tinted by its Overview tile's level. */
export function quickStats(dash: Dashboard): { label: string; value: string; level: string }[] {
  const level = (key: string) => overviewTiles(dash).find((t) => t.key === key)?.level ?? "unavailable";
  const net = sectionOk(dash.net) ? dash.net.summary : null;
  const lag = sectionOk(dash.server) ? dash.server.loop_lag?.current_ms : null;
  return [
    { label: "Requests", value: net ? `${net.rps_10s}/s` : "–", level: level("requests") },
    { label: "Browser wait", value: msText(net?.browser_queue_p95_ms), level: level("queue") },
    { label: "Server lag", value: msText(lag), level: level("server") },
  ];
}

export interface RecordView {
  recording: boolean;
  secondsLeft: number;
  onStart: () => void;
}

export function SystemCardView({
  data,
  collapsed,
  onToggle,
  pinned = false,
  hostProps,
  onOpenMonitor,
  dash = null,
  record,
  onOpenLink,
}: {
  data: SystemActivity | null;
  collapsed: boolean;
  onToggle: () => void;
  pinned?: boolean;
  hostProps?: StatusChipState["hostProps"];
  onOpenMonitor: () => void;
  /** The dashboard, polled only while the popover is open. */
  dash?: Dashboard | null;
  record?: RecordView;
  /** A verdict reason's `/monitor?...` link, clicked. */
  onOpenLink?: (link: string) => void;
}) {
  // Nothing until the first payload says the platform is supported: an idle
  // "System" chip painted while loading would vanish again where it is not.
  if (!data || !data.supported) return null;
  const label = chipLabel(data);
  const top = topProcs(data.procs);
  const verdict = dash?.verdict ?? data.verdict;
  // Only warn/bad paint the dot or speak: "ok" and "unknown" (the server could
  // not compute a verdict) are neutral.
  const alert = verdict && (verdict.level === "warn" || verdict.level === "bad") ? verdict : null;
  const dot: "warn" | "bad" | "none" = alert ? (alert.level as "warn" | "bad") : "none";
  const stats = dash ? quickStats(dash) : [];
  // Nothing when ok; when not, just the reason lines (as on the Monitor Overview).
  const reasons = dash && alert ? dash.verdict.reasons : [];
  const openLink = (link: string) => (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    onOpenLink?.(link);
  };
  return (
    <div className="dl-host sys-chip" {...hostProps}>
      <StatusChip
        label={label}
        tone="on"
        open={!collapsed}
        pinned={pinned}
        title={collapsed ? "Show what fused-render is using" : "Hide"}
        ariaLabel={`fused-render is using ${label}${
          alert?.headline ? ` · ${alert.headline}` : ""
        }`}
        onClick={onToggle}
        dot={dot}
      />
      {!collapsed && (
        <div className="dl-panel sys-panel">
          <div className="sys-host">{hostLine(data)}</div>
          {alert && reasons.length > 0 && (
            <ul className="sys-reasons" aria-label="What looks wrong">
              {reasons.map((r) => (
                <li key={r.key} className={`is-${r.level}`}>
                  <span className="sys-reason-dot" aria-hidden="true" />
                  <a className="sys-reason" href={r.link} onClick={openLink(r.link)}>
                    {r.text}
                  </a>
                </li>
              ))}
            </ul>
          )}
          {data.claude && data.claude.total > 0 && (
            <div className="sys-claude">{claudeLine(data.claude)}</div>
          )}
          {stats.length === 0 && (
            <div className="dl-section">
              <div className="dl-section-head">
                fused-render CPU · last minute{" "}
                <span className="dl-section-count">{formatCpu(data.totals.cpuPct)}</span>
              </div>
              <div className="sys-spark-wrap">
                <Sparkline
                  points={data.history.map((h) => ({ t: h.t, v: h.appCpuPct }))}
                  label="fused-render CPU, last 60 seconds"
                />
              </div>
            </div>
          )}
          {stats.length > 0 && (
            <a
              className="sys-stats"
              href={MONITOR_PATH}
              aria-label="Key numbers"
              onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                e.preventDefault();
                onOpenMonitor();
              }}
            >
              {stats.map((t) => (
                <span className={`sys-stat is-${t.level}`} key={t.label} data-stat={t.label}>
                  <span className="sys-stat-label">{t.label}</span>
                  <span className="sys-stat-value">{t.value}</span>
                </span>
              ))}
            </a>
          )}
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
            {record && (
              <button
                type="button"
                className="sys-record"
                disabled={record.recording}
                onClick={record.onStart}
              >
                {record.recording ? `Recording… ${Math.ceil(record.secondsLeft)}s` : "Record 60s"}
              </button>
            )}
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

/** The dashboard, read every 2 s only while `open`. Closing drops it, so a
 *  reopen never paints the old verdict as current while the first fresh read
 *  is in flight (and a read that lands after the close is discarded). */
export function useOpenDashboard(
  open: boolean,
  load: () => Promise<Dashboard> = getDashboard,
  pollMs = 2_000,
): Dashboard | null {
  const [dash, setDash] = useState<Dashboard | null>(null);
  // Incremented on each open: a read belongs to the opening that started it.
  const opening = useRef(0);
  const wasOpen = useRef(false);
  if (open && !wasOpen.current) opening.current += 1;
  wasOpen.current = open;
  const openRef = useRef(open);
  openRef.current = open;
  useEffect(() => {
    if (!open) setDash(null);
  }, [open]);
  usePolled(
    async () => {
      if (!open) return;
      const mine = opening.current;
      const d = await load();
      if (openRef.current && opening.current === mine) setDash(d);
    },
    open ? pollMs : 3_600_000,
    open,
  );
  return dash;
}

export default function SystemDock() {
  const chip = useStatusChip("system");
  const data = useSystemActivity(chip.open);
  const onOpenMonitor = () => {
    chip.close();
    navigateUrl(MONITOR_PATH);
  };
  // The dashboard only while the popover is open: the closed chip's cost stays
  // the one `/api/system/activity` poll.
  const dash = useOpenDashboard(chip.open);
  const rec = useRecord(undefined, undefined, chip.open);
  return (
    <SystemCardView
      data={data}
      collapsed={!chip.open}
      onToggle={chip.toggle}
      pinned={chip.pinned}
      hostProps={chip.hostProps}
      onOpenMonitor={onOpenMonitor}
      dash={chip.open ? dash : null}
      record={{
        recording: rec.recording,
        secondsLeft: rec.status?.seconds_left ?? 0,
        onStart: () => void rec.start(),
      }}
      onOpenLink={(link) => {
        chip.close();
        navigateUrl(link);
      }}
    />
  );
}
