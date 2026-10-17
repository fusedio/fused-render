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
// The popover holds the verdict (headline, and while open the reasons as links
// into the Monitor), the stacked ten-minute timeline (CPU stays one row; before
// the dashboard has loaded, and in a bare view, the 60 s CPU sparkline), the
// Claude CLI line, key numbers against their usual, the top five processes by
// CPU, "Record 60s" and "Open Monitor" — the process monitor page at /monitor
// (shell/monitor/MonitorPage.tsx).
//
// THE CHIP MAKES NO EXTRA REQUESTS: the verdict dot and the Claude line ride on
// `/api/system/activity`. Only the OPEN popover also polls
// `/api/system/dashboard` (2 s), for the reasons, timeline and key numbers.
//
// SPLIT INTO A PURE VIEW (`SystemCardView`) AND A STATEFUL WRAPPER, the same
// split ModelsDock uses, so the test renders the view with a fixed payload.
import { useStatusChip, type StatusChipState } from "@platform/lib/statusChip";
import StatusChip from "@platform/ui/StatusChip";
import { isMac } from "@platform/lib/platform";
import { navigateUrl } from "@platform/lib/router";
import { useState } from "react";
import type { SystemActivity, SystemProc } from "@platform/lib/sysmon";
import { formatBytes, formatCpu, useSystemActivity } from "@shell/system-lib";
import { Sparkline } from "@shell/monitor/Sparkline";
import TimelineStack from "@shell/monitor/TimelineStack";
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

/** Tiles the popover shows as "key numbers", each with its usual. */
const KEY_TILES = ["requests", "queue", "server", "memory", "cpu"];

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
  const cpuNow = data.totals.cpuPct;
  const verdict = dash?.verdict ?? data.verdict;
  const dot = verdict && verdict.level !== "ok" ? verdict.level : "none";
  const timeline = dash && sectionOk(dash.timeline) ? dash.timeline : null;
  const keyTiles = dash ? overviewTiles(dash).filter((t) => KEY_TILES.includes(t.key)) : [];
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
        dot={dot}
      />
      {!collapsed && (
        <div className="dl-panel sys-panel">
          <div className="sys-host">{hostLine(data)}</div>
          {verdict && verdict.headline && (
            <div className={`sys-verdict is-${verdict.level}`}>
              <p className="sys-verdict-head">{verdict.headline}</p>
              {dash && dash.verdict.reasons.length > 0 && (
                <ul className="sys-reasons">
                  {dash.verdict.reasons.map((r) => (
                    <li key={r.key}>
                      <a
                        className="sys-reason"
                        href={r.link}
                        onClick={(e) => {
                          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                          e.preventDefault();
                          onOpenLink?.(r.link);
                        }}
                      >
                        {r.text}
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {data.claude && data.claude.total > 0 && (
            <div className="sys-claude">{claudeLine(data.claude)}</div>
          )}
          {timeline && timeline.points.length > 1 ? (
            <div className="dl-section">
              <div className="dl-section-head">
                Last 10 minutes{" "}
                <span className="dl-section-count">CPU {formatCpu(cpuNow)}</span>
              </div>
              <div className="sys-tl-wrap">
                <TimelineStack points={timeline.points} events={timeline.events} compact />
              </div>
            </div>
          ) : (
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
          )}
          {keyTiles.length > 0 && (
            <dl className="sys-keys" aria-label="Key numbers">
              {keyTiles.map((t) => (
                <div className={`sys-key is-${t.level}`} key={t.key}>
                  <dt>{t.title}</dt>
                  <dd>
                    {t.value}
                    {t.baseline && <span className="sys-key-base"> · {t.baseline}</span>}
                  </dd>
                </div>
              ))}
            </dl>
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

export default function SystemDock() {
  const chip = useStatusChip("system");
  const data = useSystemActivity(chip.open);
  const onOpenMonitor = () => {
    chip.close();
    navigateUrl(MONITOR_PATH);
  };
  // The dashboard only while the popover is open: the closed chip's cost stays
  // the one `/api/system/activity` poll.
  const [dash, setDash] = useState<Dashboard | null>(null);
  usePolled(
    async () => {
      if (chip.open) setDash(await getDashboard());
    },
    chip.open ? 2_000 : 3_600_000,
    chip.open,
  );
  const rec = useRecord();
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
