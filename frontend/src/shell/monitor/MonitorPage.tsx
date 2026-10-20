// /monitor — the one place to debug "fused-render is slow". Entered from the
// System chip's "Open Monitor" (and its verdict links); offered only with the
// `monitor_enabled` pref on (App.tsx). Three tabs:
//   Overview   the verdict, health tiles and ten-minute timeline (OverviewTab)
//   Network    request and connection-slot data (NetworkTab)
//   Processes  every process, live, with stop/kill (ProcessesTab)
//
// `?tab=`, `source`, `route` and `pid` are read ONCE, at mount. A tab click or
// a verdict link writes the URL by `replaceSearch` in the click handler (user
// action), never from an effect. The shell remounts this page per navigation
// (App.tsx's epoch key), so a deep link to /monitor?tab=network&source=... always
// lands on its tab with its filters. Only the open tab is mounted, so a hidden
// tab polls nothing.
import { useCallback, useState } from "react";
import { replaceSearch } from "@platform/lib/router";
import {
  getMachineSnapshot,
  getProcHistory,
  type MachineSnapshot,
  type ProcHistoryPoint,
} from "@platform/lib/sysmon";
import NetworkTab from "./NetworkTab";
import OverviewTab, { plainClick } from "./OverviewTab";
import ProcessesTab from "./ProcessesTab";
import { CONFIRM_MS, POLL_MS } from "./monitor-lib";
import {
  monitorUrl,
  paramsFromLink,
  parseMonitorParams,
  TABS,
  type MonitorParams,
  type Tab,
} from "./monitor-net-lib";

const LABELS: Record<Tab, string> = {
  overview: "Overview",
  network: "Network",
  processes: "Processes",
};

export default function MonitorPage({
  load = getMachineSnapshot,
  loadHistory = getProcHistory,
  pollMs = POLL_MS,
  confirmMs = CONFIRM_MS,
  initial,
}: {
  load?: () => Promise<MachineSnapshot>;
  loadHistory?: (pids: number[]) => Promise<Record<string, ProcHistoryPoint[]>>;
  pollMs?: number;
  confirmMs?: number;
  /** Test seam: the params the URL would have given at mount. */
  initial?: MonitorParams;
} = {}) {
  const [params, setParams] = useState<MonitorParams>(
    () => initial ?? parseMonitorParams(typeof location === "undefined" ? "" : location.search),
  );
  // A tab switch carries its own filters (or none): the state and the URL move
  // together, and the URL write is in the handler that was clicked.
  const open = useCallback((next: MonitorParams) => {
    setParams(next);
    replaceSearch(monitorUrl(next));
  }, []);

  return (
    <div className={`monitor-page${params.tab === "processes" ? "" : " is-page-scroll"}`}>
      <header className="monitor-head monitor-head-tabs">
        <h1 className="monitor-title">Monitor</h1>
        <nav className="monitor-tabs" role="tablist" aria-label="Monitor sections">
          {TABS.map((t) => (
            <a
              key={t}
              role="tab"
              aria-selected={params.tab === t}
              className={`monitor-tab-btn${params.tab === t ? " is-on" : ""}`}
              href={monitorUrl({ tab: t })}
              onClick={(e) => {
                if (!plainClick(e)) return;
                e.preventDefault();
                open({ tab: t, source: "", route: "", pid: null });
              }}
            >
              {LABELS[t]}
            </a>
          ))}
        </nav>
      </header>
      {params.tab === "overview" && <OverviewTab onNavigate={(l) => open(paramsFromLink(l))} />}
      {params.tab === "network" && (
        <NetworkTab initialSource={params.source} initialRoute={params.route} />
      )}
      {params.tab === "processes" && (
        <ProcessesTab
          load={load}
          loadHistory={loadHistory}
          pollMs={pollMs}
          confirmMs={confirmMs}
          initialPid={params.pid}
        />
      )}
    </div>
  );
}
