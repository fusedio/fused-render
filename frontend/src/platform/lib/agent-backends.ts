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
 *  the moment the catalog answers, so a drift here costs only the first frame.
 *
 *  THE MODELS HERE ARE THE STATIC SHORTLIST. Once the server has asked the
 *  CLI which models the signed-in ACCOUNT may run (its plan's list, carried
 *  in the Copilot health snapshot), the catalog row serves that list instead,
 *  with the effort levels each model takes. `reasoning: false` on `auto`
 *  and the Claude ids is the one per-model fact the first frame already
 *  knows (the CLI takes no effort for them), so their effort pill is hidden
 *  from the first paint rather than flashing. */
export const COPILOT_FALLBACK: AgentBackendRow = {
  id: "copilot",
  label: "Copilot",
  enabled: false,
  models: [
    { id: "auto", label: "Auto (Copilot picks)", reasoning: false },
    { id: "gpt-5.4", label: "GPT-5.4" },
    { id: "gpt-5.4-mini", label: "GPT-5.4 mini" },
    { id: "claude-sonnet-4.5", label: "Claude Sonnet 4.5 (via Copilot)", reasoning: false },
    { id: "claude-opus-4.5", label: "Claude Opus 4.5 (via Copilot)", reasoning: false },
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

/** The Codex row (docs/CODEX_BACKEND.md §8), Copilot's twin for the same
 *  reason: a Codex chat answers with Codex's caps from the first frame. Off
 *  until the server says otherwise (D17). Unlike Copilot, model and mode are
 *  per turn on Codex's wire, so both live switches are on (§7).
 *  KEEP IN SYNC BY HAND with fused_render/agent_backends/codex.py — pinned
 *  field for field by tests/test_agent_backend_pins.py. */
export const CODEX_FALLBACK: AgentBackendRow = {
  id: "codex",
  label: "Codex",
  enabled: false,
  models: [
    { id: "gpt-6-luna", label: "GPT-6 Luna" },
    { id: "gpt-6.1-sol", label: "GPT-6.1 Sol" },
    { id: "gpt-5.6-terra", label: "GPT-5.6 Terra" },
    { id: "gpt-5.6-luna", label: "GPT-5.6 Luna" },
  ],
  default_model: "gpt-6-luna",
  efforts: ["low", "medium", "high", "xhigh", "max"],
  modes: ["prompt", "acceptEdits", "auto"],
  caps: {
    plan_mode: false,
    live_model_switch: true,
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
  // ENABLED: Claude alone, the defaults of every switch (D17).
  backends: [CLAUDE_FALLBACK],
  default: "claude",
  all: [CLAUDE_FALLBACK, COPILOT_FALLBACK, CODEX_FALLBACK],
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
      scheduleWarmupReread();
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

// THE FIRST CATALOG READ CAN LAND BEFORE THE SERVER HAS MEASURED an agent:
// the health probes warm in the background at startup, and a non-Claude
// agent's row carries its discovered model list (Copilot's plan list) only
// once its snapshot exists. Such a row answers `health: null`. Rather than
// show the static shortlist until the next focus, the catalog is re-read a
// few times, spaced out, while an ENABLED agent other than Claude still has
// no snapshot. Bounded: a machine whose probe never answers stops asking.
const WARMUP_REREAD_MS = [3_000, 6_000, 12_000, 24_000];
let warmupRereads = 0;
let warmupTimer: ReturnType<typeof setTimeout> | null = null;

function awaitingMeasure(): boolean {
  return current.backends.some((b) => b.id !== "claude" && b.health === null);
}

function scheduleWarmupReread() {
  if (warmupTimer) return;
  if (!awaitingMeasure() || warmupRereads >= WARMUP_REREAD_MS.length) return;
  const wait = WARMUP_REREAD_MS[warmupRereads];
  warmupRereads += 1;
  warmupTimer = setTimeout(() => {
    warmupTimer = null;
    if (awaitingMeasure()) void refreshAgentBackends();
  }, wait);
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

/** "Claude" / "Copilot" / "Codex" — the word every surface names the agent by. */
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

/** One model's catalog row, or undefined for an id the agent does not list. */
export function modelOf(id: string | null | undefined, model: string | null | undefined) {
  if (!model) return undefined;
  return backendOf(id).models.find((m) => m.id === model);
}

/** THE EFFORTS THIS MODEL TAKES — the list every effort picker offers and
 *  every send validates against. Per model where the server knows it per
 *  model (Copilot's `models.list` names the levels each id accepts, and names
 *  none for `auto` and its Claude ids); an empty list means the model takes
 *  no effort at all, and the pill is hidden rather than offering a value the
 *  spawn would drop. A model the agent does not list (a pinned or newer id)
 *  gets the agent's own list: the spawn passes an uncataloged id through
 *  with its effort, so the picker offers one. */
export function effortsForModel(id: string | null | undefined, model: string | null | undefined): string[] {
  const row = modelOf(id, model);
  if (!row) return effortsOf(id);
  if (Array.isArray(row.efforts)) return row.efforts;
  return row.reasoning === false ? [] : effortsOf(id);
}

/** The effort a new pick on this model opens on: the CLI's own default for
 *  the model when the catalog carries it, else "medium" when the model takes
 *  it, else the first level it takes, else "" (no effort). */
export function defaultEffortFor(id: string | null | undefined, model: string | null | undefined): string {
  const list = effortsForModel(id, model);
  if (!list.length) return "";
  const own = modelOf(id, model)?.default_effort;
  if (own && list.includes(own)) return own;
  return list.includes("medium") ? "medium" : list[0];
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

/** Has the server answered (a real catalog, or a 404 from an older one)?
 *  The plain read, which never starts one — for pure code that must not act
 *  on the static rows as if they were the server's answer. */
export function agentCatalogLoaded(): boolean {
  return current.loaded;
}

/** A model id ANOTHER agent lists and this one does not: a pick carried across
 *  an agent change ("sonnet" on a Copilot task, "auto" on a Claude one), which
 *  would spawn one agent with the other's model. An id NO agent lists is not
 *  foreign — it is a pinned or newer id, carried through as typed. */
export function isForeignModel(model: string | null | undefined, id: string | null | undefined): boolean {
  if (!model) return false;
  const own = id || "claude";
  if (backendOf(own).models.some((m) => m.id === model)) return false;
  return current.all.some((b) => b.id !== own && b.models.some((m) => m.id === model));
}

/** An effort this agent — or, with `model`, this model on this agent — does
 *  not offer. Efforts are a closed set per agent and per model, so anything
 *  off the list is dropped ("no effort", the spawn's default). */
export function isUnlistedEffort(
  effort: string | null | undefined,
  id: string | null | undefined,
  model?: string | null,
): boolean {
  if (!effort) return false;
  return !(model ? effortsForModel(id, model) : backendOf(id).efforts).includes(effort);
}

/** Is this agent SWITCHED OFF, on the server's word? False until the catalog
 *  has answered — the static rows are not a verdict — so a door to an agent
 *  that is on never blinks out on the first frame (D17). */
export function isBackendOff(id: string | null | undefined): boolean {
  return current.loaded && !isBackendEnabled(id);
}

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
  if (warmupTimer) clearTimeout(warmupTimer);
  warmupTimer = null;
  warmupRereads = 0;
  current = next ? fromServer(next) : { ...FALLBACK, loaded: false };
  for (const l of listeners) l();
}
