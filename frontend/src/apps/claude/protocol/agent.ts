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
  /** What the chat is open ON (`_file`) — `X-Fused-Target`. The PAGE half is
   *  derived from the script's own dir, so only this needs handing in. */
  target?: string | null;
}

const inflightByKey = new Map<string, AbortController>();
const superseded = new WeakSet<AbortController>();

// ---- call-log attribution (SPEC CL-5, `fused_render/calls.py`) --------------
//
// FLAG-ON, A CHAT'S CALLS WERE ANONYMOUS. `runtime.js` builds these headers
// (R:1434-1448) off the EMBEDDED PAGE's own URL — `ownQuery("path")` and
// `ownQuery("_file")` — and the native chat has no such URL, so nothing set
// them: `fused-render calls`, `--page <chat template>` and the `.calls.jsonl`
// viewer all showed an empty history for a conversation, and the failed-call
// digests for the chat went with it. Observability only; no behaviour depends
// on it, which is exactly why it was easy to lose.
//
// The page is the template's own `template.html`, which is what `--page` names
// and what a reader looking for "the chat's calls" would type.

/** The correlation id, `runtime.js`'s shape (R:1442's `newCallId`). */
function newCallId(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : String(Date.now()) + "-" + Math.random().toString(36).slice(2);
}

/**
 * Ids abandoned since the last call went out, waiting to ride the next one.
 *
 * ON THE SUPERSEDING REQUEST, and `calls.py:80-85` explains why that and not a
 * POST of its own: the superseding request "leaves in the same task as the
 * abort, so the mark lands before the abandoned call's record is written — a
 * separate POST measured ~19 ms later, and anything that finished inside that
 * window was recorded `ok`."
 */
let pendingSupersedes: string[] = [];

/** The call id a controller was given, so an abort can name what it cancelled. */
const callIds = new WeakMap<AbortController, string>();

function takePendingSupersedes(): string {
  if (!pendingSupersedes.length) return "";
  const out = pendingSupersedes.join(",");
  pendingSupersedes = [];
  return out;
}

/** Test-only: the queue is page-lifetime state in production. */
export function resetSupersedesForTests(): void {
  pendingSupersedes = [];
}

/** Never settles — the runtime's spelling for "a newer call owns the result". */
function hang<T>(): Promise<T> {
  return new Promise<T>(() => {});
}

/** Low-level: run any script under the template dir with the supersede rule. */
export async function runScript<T>(py: string, params: Record<string, unknown>, opts: RunOpts = {}): Promise<T> {
  const key = opts.key === undefined ? py : opts.key;
  const controller = new AbortController();
  const callId = newCallId();
  callIds.set(controller, callId);
  if (key !== null) {
    const prev = inflightByKey.get(key);
    if (prev) {
      superseded.add(prev);
      // The abandoned call names itself, so the mark can ride the request that
      // caused it (see `pendingSupersedes`).
      const was = callIds.get(prev);
      if (was) pendingSupersedes.push(was);
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
    const data = await runPy(py, params, {
      signal: controller.signal,
      attribution: {
        // `<templateDir>/template.html` — the page `--page` names, derived from
        // the script's own dir so every script under a template attributes to
        // one page rather than to itself.
        page: py.replace(/\/[^/]+$/, "") + "/template.html",
        ...(opts.target ? { target: opts.target } : {}),
        callId,
        ...(() => {
          const abandoned = takePendingSupersedes();
          return abandoned ? { supersedes: abandoned } : {};
        })(),
      },
    });
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
