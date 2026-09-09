// The chat's transport to `templates/claude/agent.py`: POST /api/run with the
// template folder's `agent.py` and string-shaped params. Pure TS, no React.
//
// Two runtime.js behaviours the template leaned on are re-provided here
// (R:2539-2580): per-key SUPERSEDE — a newer call on the same `key` aborts the
// older one, whose promise then never settles so its stale continuation never
// runs (`key: null` opts out, which is what every chat call does, T:16620) —
// and `needs_install` surfaced as a typed error instead of a loader flow.
import { runPy, statPath, type NeedsInstall } from "@platform/lib/api";
import type {
  Action,
  AgentRequests,
  AgentResponses,
  AppEntryResponse,
  ArtifactsListResponse,
} from "./types";

/** A run that raised in Python (`/api/run` `ok:false`, D69). */
export class AgentError extends Error {
  readonly type: string | undefined;
  readonly traceback: string | undefined;
  readonly stdout: string | undefined;
  constructor(err: { type?: string; message?: string; traceback?: string } | undefined, stdout?: string) {
    super(err?.message || "agent.py failed");
    this.name = "AgentError";
    this.type = err?.type;
    this.traceback = err?.traceback;
    this.stdout = stdout;
  }
}

/** The project venv is not built yet (engine.py `_needs_install_dict`). The
 *  chat shows this as a trouble card rather than running the installer. */
export class AgentNeedsInstall extends Error {
  readonly needs: NeedsInstall;
  constructor(needs: NeedsInstall, message: string | undefined) {
    super(message || `${needs.name} declares dependencies that are not installed yet`);
    this.name = "AgentNeedsInstall";
    this.needs = needs;
  }
}

export interface RunOpts {
  /** Supersede channel. `undefined` = the script path; `null` = no channel. */
  key?: string | null;
  signal?: AbortSignal;
}

const inflightByKey = new Map<string, AbortController>();
const superseded = new WeakSet<AbortController>();

/** Never settles — the runtime's spelling for "a newer call owns the result". */
function hang<T>(): Promise<T> {
  return new Promise<T>(() => {});
}

/** Low-level: run any script under the template dir with the supersede rule. */
export async function runScript<T>(py: string, params: Record<string, unknown>, opts: RunOpts = {}): Promise<T> {
  const key = opts.key === undefined ? py : opts.key;
  const controller = new AbortController();
  if (key !== null) {
    const prev = inflightByKey.get(key);
    if (prev) {
      superseded.add(prev);
      prev.abort();
    }
    inflightByKey.set(key, controller);
  }
  let detach: (() => void) | null = null;
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort();
    else {
      const onAbort = () => controller.abort();
      opts.signal.addEventListener("abort", onAbort);
      detach = () => opts.signal?.removeEventListener("abort", onAbort);
    }
  }
  const cleanup = () => {
    detach?.();
    if (key !== null && inflightByKey.get(key) === controller) inflightByKey.delete(key);
  };
  try {
    const data = await runPy(py, params, { signal: controller.signal });
    cleanup();
    if (superseded.has(controller)) return hang<T>();
    if (data.needs_install) throw new AgentNeedsInstall(data.needs_install, data.error?.message);
    if (!data.ok) throw new AgentError(data.error, data.stdout);
    return data.result as T;
  } catch (err) {
    cleanup();
    // The caller's own abort wins (their finally must run); a supersede hangs.
    if (opts.signal?.aborted) throw err;
    if (superseded.has(controller)) return hang<T>();
    throw err;
  }
}

/** One `agent.py` action, typed by name (`04-core-chat.md §B`). */
export function runAgent<K extends Action>(
  dir: string,
  action: K,
  fields: AgentRequests[K],
  opts: RunOpts = {},
): Promise<AgentResponses[K]> {
  return runScript<AgentResponses[K]>(`${dir}/agent.py`, { action, ...fields }, opts);
}

/** `./app.py {dir}` — the folder's entry html, if it is an app (T:5417). */
export function runAppEntry(dir: string, target: string, opts: RunOpts = {}): Promise<AppEntryResponse> {
  return runScript<AppEntryResponse>(`${dir}/app.py`, { dir: target }, opts);
}

/** `./artifacts.py` (T:18482). */
export function runArtifacts(
  dir: string,
  fields: Record<string, string>,
  opts: RunOpts = { key: null },
): Promise<ArtifactsListResponse> {
  return runScript<ArtifactsListResponse>(`${dir}/artifacts.py`, fields, opts);
}

// ---- template dir resolution (TaskCards.tsx:90-118 idiom) ------------------

/** `null` = asked, and this path has no claude template (kept, so a folder
 *  without one is not re-stat'd per mount). Honors user template overrides. */
const dirCache = new Map<string, string | null>();
const dirInFlight = new Map<string, Promise<string | null>>();

function dirname(p: string): string {
  const i = p.lastIndexOf("/");
  return i <= 0 ? p : p.slice(0, i);
}

/** The folder holding `agent.py` for `file`'s claude template, via
 *  `statPath(file).templates.find(mode === "claude").path` (00-shell-infra §1d). */
export function resolveAgentDir(file: string): Promise<string | null> {
  if (dirCache.has(file)) return Promise.resolve(dirCache.get(file) ?? null);
  const running = dirInFlight.get(file);
  if (running) return running;
  const p = statPath(file)
    .then((st) => {
      const tpl = st.templates?.find((t) => t.mode === "claude")?.path ?? null;
      const dir = tpl ? dirname(tpl) : null;
      dirCache.set(file, dir);
      return dir;
    })
    .catch(() => {
      // Not cached: a failed stat is not an answer about the folder.
      return null;
    })
    .finally(() => {
      dirInFlight.delete(file);
    });
  dirInFlight.set(file, p);
  return p;
}

/** Test-only. */
export function resetAgentDirCacheForTests(): void {
  dirCache.clear();
  dirInFlight.clear();
}
