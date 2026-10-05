// Live CPU and memory — fused-render's own processes, or with `?scope=all`
// every process on the machine — the wire shape of `GET /api/system/activity`
// (fused_render/sysmon) plus the formatting every surface that shows it shares
// (the System chip, the /monitor page). The chip's poll lives in
// `shell/system-lib.ts`; the Monitor page polls on its own.
import { getJson, mutateJson } from "@platform/lib/api";

export type SystemProcKind =
  | "app"
  | "claude"
  | "engine"
  | "model"
  | "terminal"
  | "install"
  | "run"
  | "other";

export interface SystemProc {
  pid: number;
  ppid: number | null;
  kind: SystemProcKind;
  label: string;
  /** null until the sampler has two readings of this pid. 100 = one core. */
  cpuPct: number | null;
  memBytes: number | null;
  startedAt: number | null;
}

export interface SystemHost {
  /** Whole machine, as a share of ALL cores (100 = every core busy). */
  cpuPct: number | null;
  cpuUser: number | null;
  cpuSystem: number | null;
  ncpu: number;
  load: number[] | null;
  memTotal: number | null;
  memUsed: number | null;
  memApp: number | null;
  memWired: number | null;
  memCompressed: number | null;
}

export interface SystemHistoryPoint {
  t: number;
  cpuPct: number | null;
  memUsed: number | null;
  appCpuPct: number | null;
  appMemBytes: number | null;
}

export interface SystemActivity {
  supported: boolean;
  host: SystemHost | null;
  procs: SystemProc[];
  history: SystemHistoryPoint[];
  /** Summed over fused-render's processes: what fused-render costs you. */
  totals: { cpuPct: number | null; memBytes: number | null };
}

/** One row of `?scope=all` — every process on the machine. `fused` rows keep
 *  the labeller's kind/label; every other row is kind "system", labelled by its
 *  command. Another user's process has null figures (the kernel refuses to
 *  measure it) but is still listed. */
export interface MachineProc {
  pid: number;
  ppid: number | null;
  user: string | null;
  /** Executable basename. */
  command: string;
  /** Full command line, cut at 200 characters. */
  args: string;
  cpuPct: number | null;
  memBytes: number | null;
  /** Epoch seconds; with the pid, what tells a recycled pid apart. */
  startedAt: number | null;
  fused: boolean;
  kind: SystemProcKind | "system";
  label: string;
}

export interface MachineSnapshot extends Omit<SystemActivity, "procs"> {
  procs: MachineProc[];
}

export interface ProcHistoryPoint {
  t: number;
  cpuPct: number | null;
  memBytes: number | null;
}

export function getSystemActivity(): Promise<SystemActivity> {
  return getJson<SystemActivity>("/api/system/activity");
}

/** The whole machine (`?scope=all`) — the shell's /monitor page. */
export function getMachineSnapshot(): Promise<MachineSnapshot> {
  return getJson<MachineSnapshot>("/api/system/activity?scope=all");
}

/** The last minute of each pid's (cpuPct, memBytes), keyed by pid. */
export function getProcHistory(pids: number[]): Promise<Record<string, ProcHistoryPoint[]>> {
  return getJson<Record<string, ProcHistoryPoint[]>>(
    `/api/system/activity/history?pids=${pids.join(",")}`,
  );
}

/** SIGTERM (SIGKILL with `force`) any listed process, named by `(pid,
 *  startedAt)`. `{ok:false, error}` is an answer ("permission denied"), not a
 *  throw; a 4xx (gone, changed, refused) throws. */
export function killSystemProcess(
  pid: number,
  startedAt: number | null,
  force = false,
): Promise<{ ok: boolean; signal?: string; error?: string }> {
  return mutateJson("POST", "/api/system/activity/kill", { pid, startedAt, force });
}

/** End one of fused-render's own processes through its owner. `startedAt` is
 *  the process as the caller saw it: the server answers 404 "process changed"
 *  if that pid has since become a different process. */
export function stopSystemProcess(
  pid: number,
  startedAt: number | null,
): Promise<{ ok: boolean; via: string }> {
  return mutateJson<{ ok: boolean; via: string }>("POST", "/api/system/activity/stop", {
    pid,
    startedAt,
  });
}

/** Bytes the way Activity Monitor writes them: "1.02 GB", "194.3 MB",
 *  "812 KB". `compact` drops to one decimal for GB ("1.4 GB"), for the chip. */
export function formatBytes(bytes: number | null | undefined, compact = false): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "–";
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  const gb = mb / 1024;
  return `${gb.toFixed(compact ? 1 : 2)} GB`;
}

/** CPU % with one decimal ("12.3%"), or "–" when not known yet. */
export function formatCpu(pct: number | null | undefined, decimals = 1): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return "–";
  return `${pct.toFixed(decimals)}%`;
}

