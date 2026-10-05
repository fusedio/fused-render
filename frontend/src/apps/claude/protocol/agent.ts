// The chat's transport to the in-process agent (`fused_render/claude_agent/`):
// POST /api/claude/agent with `{action, ...fields}`, every field string-shaped
// exactly as the handlers take them. Pure TS, no React.
//
// No folder, no script path: the server owns the one agent module, so a call
// names only the action. The two siblings — the folder's app entry and the
// artifacts reader — have routes of their own beside it.
import { postJson, runHeaders, type HttpError } from "@platform/lib/api";
import type {
  Action,
  AgentRequests,
  AgentResponses,
  AppEntryResponse,
  ArtifactsListResponse,
} from "./types";

/** A handler that raised, or ran out of its budget, on the server: the route's
 *  non-2xx `{error: {type, message, traceback}}` (504 is `type: "Timeout"`).
 *  A handler's own `{error: "..."}` is a 200 and comes back as the result. */
export class AgentError extends Error {
  readonly type: string | undefined;
  readonly traceback: string | undefined;
  readonly stdout: string | undefined;
  constructor(err: { type?: string; message?: string; traceback?: string } | undefined, stdout?: string) {
    super(err?.message || "the chat agent failed");
    this.name = "AgentError";
    this.type = err?.type;
    this.traceback = err?.traceback;
    this.stdout = stdout;
  }
}

export interface RunOpts {
  signal?: AbortSignal;
  /** What the chat is open ON (`_file`) — `X-Fused-Target`. */
  target?: string | null;
}

// ---- call-log attribution (SPEC CL-5, `fused_render/calls.py`) --------------
//
// A native page has no URL of its own for `runtime.js`'s header builder to
// read, so the chat says who it is: one constant page id for every chat call
// (`fused_render.claude_agent.CLAUDE_PAGE_ID`, which `calls.is_first_party`
// recognises), plus the target and a per-call correlation id. Observability
// only; no behaviour depends on it, which is exactly why it is easy to lose.

/** `X-Fused-Page` for every chat call. */
export const CLAUDE_PAGE_ID = "fused-render://claude";

/** The correlation id, `runtime.js`'s shape (R:1442's `newCallId`). */
function newCallId(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : String(Date.now()) + "-" + Math.random().toString(36).slice(2);
}

/** POST one of the chat routes. A 2xx body IS the result. A non-2xx whose
 *  `error` is the structured `{type, message, traceback}` becomes an
 *  `AgentError`; a string `error` (400 bad action/params, 403) stays the
 *  platform's `HttpError`; an abort rethrows untouched. */
async function post<T>(url: string, body: Record<string, unknown>, opts: RunOpts = {}): Promise<T> {
  try {
    return await postJson<T>(url, body, {
      ...(opts.signal ? { signal: opts.signal } : {}),
      headers: runHeaders({
        page: CLAUDE_PAGE_ID,
        ...(opts.target ? { target: opts.target } : {}),
        callId: newCallId(),
      }),
    });
  } catch (err) {
    const said = (err as HttpError | null)?.body as { error?: unknown } | null | undefined;
    const e = said?.error;
    if (e && typeof e === "object") {
      throw new AgentError(e as { type?: string; message?: string; traceback?: string });
    }
    throw err;
  }
}

/** One agent action, typed by name (`04-core-chat.md §B`). */
export function runAgent<K extends Action>(
  action: K,
  fields: AgentRequests[K],
  opts: RunOpts = {},
): Promise<AgentResponses[K]> {
  return post<AgentResponses[K]>("/api/claude/agent", { action, ...fields }, opts);
}

/** The agent's `terminal_command` action, which already returns a full
 *  `cd '<dir>' && claude ...` line — the one fetch every "continue this
 *  session in a terminal" caller shares (`apps/claude/ui/Kebab.tsx`'s
 *  "Continue in terminal", `shell/TaskPeek.tsx`'s own), so neither re-derives
 *  it and there is exactly one `cd` in the string either ends up sending. */
export async function fetchTerminalCommand(file: string, sessionId: string): Promise<string> {
  const out = await runAgent("terminal_command", { file, session_id: sessionId });
  if ("error" in out && out.error) throw new Error(out.error);
  if (!("command" in out)) throw new Error("the chat agent returned no command");
  return out.command;
}

/** The folder's entry html, if it is an app (`{entry: null}` when not). */
export function runAppEntry(target: string, opts: RunOpts = {}): Promise<AppEntryResponse> {
  return post<AppEntryResponse>("/api/claude/app-entry", { dir: target }, opts);
}

/** The artifacts reader: `{action: "list", file}` or `{action: "live",
 *  session_id, file}`. */
export function runArtifacts(fields: Record<string, string>, opts: RunOpts = {}): Promise<ArtifactsListResponse> {
  return post<ArtifactsListResponse>("/api/claude/artifacts", fields, opts);
}
