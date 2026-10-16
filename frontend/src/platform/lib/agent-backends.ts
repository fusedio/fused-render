// THE AGENT CATALOG — which agents may run chats and tasks, and what each one
// offers (`GET /api/agent-backends`, docs/COPILOT_BACKEND.md D11).
//
// One read per page, cached and subscribed, the way apps/claude/feature-flag.ts
// holds the project-queue switch: every surface that names an agent, lists its
// models or hides a control by its caps reads from here rather than from a
// hand-synced list. The Claude row's models/efforts/modes used to live in
// three copies (the composer's MODELS/EFFORTS, the New task card's
// TASK_MODELS/TASK_EFFORTS, the Preferences MODEL_LABELS) kept in step by a
// comment asking people to; the server's registry is the one copy now.
//
// BEFORE THE READ LANDS the catalog is a STATIC CLAUDE ROW — the same four
// models, five efforts and four modes the composer always offered — so nothing
// paints empty or flashes on the first frame, and a server that predates the
// endpoint (a 404) simply keeps that answer. Only when the real catalog says
// otherwise does anything change.
import { useSyncExternalStore } from "react";
import {
  getAgentBackends,
  type HttpError,
  type AgentBackendRow,
  type AgentCaps,
  type AgentCatalog,
} from "./api";
import { GATE_FALLBACK_MS } from "./clock";

export type { AgentBackendRow, AgentCaps, AgentCatalog };

/** Every cap on, which is what Claude Code has (agent_backends/claude.py). */
export const ALL_CAPS: AgentCaps = {
  plan_mode: true,
  live_model_switch: true,
  live_mode_switch: true,
  snapshots: true,
  artifacts: true,
  worktrees: true,
  bg_tasks: true,
  ask_user: true,
  terminal_mirror: true,
  mid_turn_messages: true,
};

/** The Claude row as it stood before the catalog existed — the fallback for
 *  the first frame and for an older server. Lists VERBATIM from
 *  apps/claude/ui/composer-defaults (T:11823-11912). */
export const CLAUDE_FALLBACK: AgentBackendRow = {
  id: "claude",
  label: "Claude",
  enabled: true,
  models: [
    { id: "fable", label: "Fable" },
    { id: "opus", label: "Opus" },
    { id: "sonnet", label: "Sonnet" },
    { id: "haiku", label: "Haiku" },
  ],
  default_model: "sonnet",
  efforts: ["low", "medium", "high", "xhigh", "max"],
  modes: ["plan", "prompt", "acceptEdits", "auto"],
  caps: ALL_CAPS,
  health: null,
};

/** The Copilot row, so `backendOf("copilot")` answers with Copilot's caps
 *  before the read lands — a Copilot chat must never draw Claude-only
 *  controls for a frame. Off (`enabled: false`, and absent from `backends`)
 *  until the server says otherwise, which is Copilot's own default (D17).
 *  KEEP IN SYNC BY HAND with fused_render/agent_backends/copilot.py
 *  (models, default_model, efforts, modes, caps): the server row replaces it
 *  the moment the catalog answers, so a drift here costs only the first frame. */
export const COPILOT_FALLBACK: AgentBackendRow = {
  id: "copilot",
  label: "Copilot",
  enabled: false,
  models: [
    { id: "auto", label: "Auto (Copilot picks)" },
    { id: "gpt-5.4", label: "GPT-5.4" },
    { id: "gpt-5.4-mini", label: "GPT-5.4 mini" },
    { id: "claude-sonnet-4.5", label: "Claude Sonnet 4.5 (via Copilot)" },
    { id: "claude-opus-4.5", label: "Claude Opus 4.5 (via Copilot)" },
    { id: "gemini-3-pro", label: "Gemini 3 Pro (via Copilot)" },
  ],
  default_model: "auto",
  efforts: ["low", "medium", "high", "xhigh", "max"],
  modes: ["prompt", "acceptEdits", "auto"],
  caps: {
    plan_mode: false,
    live_model_switch: false,
    live_mode_switch: true,
    snapshots: false,
    artifacts: false,
    worktrees: false,
    bg_tasks: false,
    ask_user: false,
    terminal_mirror: false,
    mid_turn_messages: false,
  },
  health: null,
};

const FALLBACK: AgentCatalog = {
  // ENABLED: Claude alone, the defaults of both switches (D17).
  backends: [CLAUDE_FALLBACK],
  default: "claude",
  all: [CLAUDE_FALLBACK, COPILOT_FALLBACK],
};

export interface AgentCatalogState extends AgentCatalog {
  /** The server has answered (or refused) once; until then this is the
   *  static Claude row. */
  loaded: boolean;
}

let current: AgentCatalogState = { ...FALLBACK, loaded: false };
let reading: Promise<void> | null = null;
let generation = 0;
/** Consecutive failed reads, for the retry's backoff; 0 after an answer. */
let failures = 0;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
const RETRY_BASE_MS = 1_000;
const RETRY_MAX_MS = 30_000;
const listeners = new Set<() => void>();

function publish(next: AgentCatalogState) {
  current = next;
  for (const l of listeners) l();
}

function fromServer(c: AgentCatalog | null | undefined): AgentCatalogState {
  const all = Array.isArray(c?.all) && c!.all.length ? c!.all : FALLBACK.all;
  const backends = Array.isArray(c?.backends) && c!.backends.length
    ? c!.backends
    : all.filter((b) => b.enabled);
  const dflt =
    typeof c?.default === "string" && backends.some((b) => b.id === c!.default)
      ? c!.default
      : backends[0]?.id || "claude";
  return { backends: backends.length ? backends : FALLBACK.backends, default: dflt, all, loaded: true };
}

function withDeadline<T>(work: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("catalog read timed out")), ms);
    work.then(
      (v) => { clearTimeout(timer); resolve(v); },
      (e) => { clearTimeout(timer); reject(e); },
    );
  });
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = withDeadline(getAgentBackends(), GATE_FALLBACK_MS)
    .then((c) => {
      if (generation !== departed) return;
      failures = 0;
      publish(fromServer(c));
    })
    .catch((err) => {
      if (generation !== departed) return;
      // A 404 IS an answer: a server that predates the endpoint, which runs
      // Claude alone — exactly the static rows. Marked loaded so nothing waits
      // on a catalog that is never coming.
      if ((err as HttpError | null)?.status === 404) {
        publish({ ...current, loaded: true });
        return;
      }
      // ANYTHING ELSE (a timeout, a server mid-restart) says nothing about
      // which agents are on, so `loaded` stays false — the gates that wait on
      // a real answer (a switched-off agent's closed composer) keep waiting —
      // and the read is tried again, backing off.
      failures += 1;
      const wait = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** (failures - 1));
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = setTimeout(() => {
        retryTimer = null;
        if (!current.loaded) void read();
      }, wait);
    })
    .finally(() => {
      if (generation === departed) reading = null;
    });
  return reading;
}

/** Ask the server again — after a Preferences switch, or when the window comes
 *  back. A new generation, so the read this replaces cannot speak after it. */
export function refreshAgentBackends(): Promise<void> {
  generation += 1;
  reading = null;
  return read();
}

/** The `localStorage` key a Preferences switch announces itself on, so every
 *  OTHER tab re-reads instead of keeping the catalog it read at load. */
export const AGENTS_BROADCAST_KEY = "fused-render:agent-backends";

/** A switch was flipped on this page: re-read here, and tell the other tabs. */
export function publishAgentBackendsChanged() {
  void refreshAgentBackends();
  try {
    localStorage.setItem(AGENTS_BROADCAST_KEY, JSON.stringify({ at: Date.now() }));
  } catch {
    // Storage may be unavailable; the other tabs still re-read on focus.
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (ev) => {
    if (ev.key === AGENTS_BROADCAST_KEY && ev.newValue) void refreshAgentBackends();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && current.loaded) void refreshAgentBackends();
  });
}

/** The catalog RIGHT NOW — the plain read for a send path or a resolver that
 *  runs outside React. Triggers the one read if nothing has asked yet. */
export function agentCatalog(): AgentCatalogState {
  if (!current.loaded && !reading) void read();
  return current;
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  // Only while nothing has answered: a mount after the read has landed reads
  // the cache, and the focus / Preferences paths below re-ask on purpose.
  if (!current.loaded) void read();
  return () => {
    listeners.delete(cb);
  };
}
const snapshot = () => current;

/** Subscribe to the catalog. `backends` are the ENABLED agents, `all` every
 *  one; `default` is what a new chat or task opens on. */
export function useAgentBackends(): AgentCatalogState {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

// ---- the helpers every surface reads through ---------------------------------

/** The row for an id — enabled or not — or the Claude fallback for an id the
 *  catalog has never heard of ("" included), so a reader never gets `null`. */
export function backendOf(id: string | null | undefined): AgentBackendRow {
  const want = id || "claude";
  return current.all.find((b) => b.id === want) ?? current.backends.find((b) => b.id === want) ?? CLAUDE_FALLBACK;
}

/** "Claude" / "Copilot" — the word every surface names the agent by. */
export function agentLabel(id: string | null | undefined): string {
  return backendOf(id).label;
}

export function capsOf(id: string | null | undefined): AgentCaps {
  return backendOf(id).caps ?? ALL_CAPS;
}

/** The model ids an agent offers, in the registry's order. */
export function modelsOf(id: string | null | undefined): string[] {
  return backendOf(id).models.map((m) => m.id);
}

/** `{id → label}` for one agent's models. */
export function modelLabelsOf(id: string | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of backendOf(id).models) out[m.id] = m.label;
  return out;
}

export function effortsOf(id: string | null | undefined): string[] {
  return backendOf(id).efforts;
}

export function modesOf(id: string | null | undefined): string[] {
  return backendOf(id).modes;
}

export function defaultModelOf(id: string | null | undefined): string {
  return backendOf(id).default_model;
}

/** Is this agent switched on (D17)? An unknown id is not. */
export function isBackendEnabled(id: string | null | undefined): boolean {
  return current.backends.some((b) => b.id === (id || "claude"));
}

/** Does a surface that names NO agent today (a task row, a card, a peek, a
 *  notification) name this one? Only when there is more than one to tell
 *  apart, or the row's agent is not Claude — so an install with Claude alone
 *  draws exactly what it drew before the second agent, and a Copilot task
 *  stays named after Copilot is switched off again (D18). */
export function agentNamed(id: string | null | undefined): boolean {
  return (id || "claude") !== "claude" || current.backends.length > 1;
}

/** Where every "turn it on" link goes: the Agents section on Preferences' AI
 *  tab (D17). One string, so the chat's line, the trouble card and the New
 *  task card cannot point at three places. */
export const AGENT_PREFS_URL = "/preferences?tab=ai";

/** What a NEW chat or task opens on: the catalog's default. */
export function defaultBackend(): string {
  return current.default;
}

/** Test seam: put the module back to the static Claude row. */
export function resetAgentBackendsForTests(next?: AgentCatalog) {
  generation += 1;
  reading = null;
  failures = 0;
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
  current = next ? fromServer(next) : { ...FALLBACK, loaded: false };
  for (const l of listeners) l();
}
