// The contract between the chat's protocol layer (run-controller.ts) and its
// React UI. Pure types — no React, no DOM. The controller implements
// `ChatController`; the UI reads `ChatState` through useSyncExternalStore and
// calls the methods. Behaviour cites: .claude-design/inventory/04-core-chat.md.
//
// Rule for implementers: extend, never rename. UI agents build against this
// file while the controller is written in parallel.

import type {
  Activity,
  AppStateRow,
  Decision,
  DecisionScope,
  HistoryTurn,
  PermissionMode,
  PermissionRow,
  Phase,
  RetryInfo,
  Segment,
  SkillRow,
  SwitchableMode,
  TranscriptStat,
} from "./types";

/** One transcript row. History turns and live turns share this shape. */
export interface UserTurn {
  role: "user";
  /** Stable key: history uuid, or `u:<sendSeq>` for a live send. */
  key: string;
  /** Display text — wire blocks (<live-app-state>, <pane-shot>, <annotations>) already stripped (T:10404-10608). */
  text: string;
  /** Raw outgoing text incl. blocks, for the "what was sent" popover (T:10970). */
  raw?: string;
  uuid?: string;
}

export interface AssistantTurn {
  role: "assistant";
  key: string;
  /** Finalised markdown text; while streaming this is the text so far. */
  text: string;
  /** Present when the turn had tool/thinking/notice segments (T:15591). */
  segments?: Segment[];
  /** True only on the last history turn when the run was stopped (agent.py:5049). */
  stopped?: boolean;
  /** True while this turn is the live bubble being polled. */
  streaming?: boolean;
  /** Which followup slice this bubble belongs to (D687, T: followupSeq). */
  followup?: number;
}

/**
 * A ⏹ / ◍ / ◆ note row (T:13718-13734 A12, `addNote`). ADDED to the union: these
 * are transcript rows in `T`, appended at chronological position and kept there
 * after the thing they announce is settled — "Stopped." / "The turn finished
 * before the stop landed." (T:16374), "read app state — <reason>" (T:15797) and
 * "skill · <name>" (T:15779). `ChatState.appState` / `.skills` are the LIVE
 * lists (what the pane must still answer, what the run has reached for); these
 * rows are the log's own record and outlive them.
 *
 * A renderer that only knows `user` and `assistant` must branch on
 * `role === "note"` before its assistant fallback.
 */
export interface NoteTurn {
  role: "note";
  key: string;
  text: string;
  /** ⏹ a stop, ◍ an app-state read, ◆ a skill (T:13722). */
  glyph: "\u23f9" | "\u25cd" | "\u25c6";
}

/**
 * A failure that landed IN the transcript (T:13698 `addError`). `T` appends one
 * row per failure and leaves it where it happened; the controller's
 * `ChatState.trouble` slot holds only the LATEST one, which is right for the
 * actionable card at the tail but loses the record of every failure before it —
 * so a turn that failed, was retried and failed differently read as one problem.
 *
 * ADDED to the union for that record. `text` is the message verbatim (a text
 * node, never markdown), and `kind` is the classification the slot would carry,
 * so a renderer can dress the row the way the card would.
 *
 * A renderer that only knows `user` and `assistant` must branch on
 * `role === "error"` before its assistant fallback, exactly as for `note`.
 */
export interface ErrorTurn {
  role: "error";
  key: string;
  text: string;
  kind: TroubleKind;
}

export type Turn = UserTurn | AssistantTurn | NoteTurn | ErrorTurn;

/** Kinds the trouble card knows (T:13531-13602 TROUBLE_*). Reuse platform/lib/trouble.ts ids where identical. */
export type TroubleKind =
  | "cli-missing"
  | "cli-broken"
  | "login"
  /** ADDED (protocol/trouble.ts): the plan's usage limit — the third failure the
   *  template draws real copy for (T:13548 TROUBLE_SAID.limit). Renders through
   *  `platform/ui/TroubleCard.tsx` like `login` / `cli-missing`. */
  | "limit"
  | "needs-install"
  | "engine"
  | "unknown-run"
  | "network"
  | "generic";

export interface Trouble {
  kind: TroubleKind;
  message: string;
  /** Verbatim traceback / stderr when present. */
  detail?: string;
}

/** Working-line input (T:14782-14943 activityVerb/activityDetail/retryVerb). */
export interface Working {
  phase: Phase;
  activity: Activity | null;
  retry: RetryInfo | null;
  tokens: number;
  /** ms since the run started, drives the elapsed clock (1 s tick, T:14903). */
  startedAt: number;
  /** "Claude is …" line for a run owned by another tab/host (T:17506 setExternalWorking). */
  external?: boolean;
}

export type RunStatus = "idle" | "starting" | "running" | "stopping";

export interface ChatState {
  file: string | null;
  sessionId: string | null;
  runId: string | null;
  status: RunStatus;
  /** Ordered transcript. The last assistant turn is the live bubble while running. */
  turns: Turn[];
  /** Open + parked permission/question/plan cards, in arrival order (T:14659-14781). */
  permissions: PermissionRow[];
  /** Unanswered app-state requests the pane must answer (T:15758-15869). */
  appState: AppStateRow[];
  skills: SkillRow[];
  working: Working | null;
  trouble: Trouble | null;
  /**
   * The permission mode the RUN is actually in — never the picker's param, which
   * applies to the next spawn (T:13884-13886). Seeded from the mode the run was
   * started with, replaced by `poll`'s own `mode`, and moved forward by the two
   * things that change a live run's mode: "Allow, and let Claude decide from
   * here" (T:13988-13992) and approving a plan (T:14548-14580).
   *
   * It is what gates a perm card's escalation button: without it `permChoices`
   * falls back to `DEFAULT_PERMISSION` and offers "let Claude decide from here"
   * to a run already in `auto`, or mid-plan, where T offers neither.
   */
  permissionMode: PermissionMode;
  /** Follow-ups typed while a run is live, not yet acknowledged (T:16024 sendFollowUp). */
  queued: string[];
  historyLoading: boolean;
  transcript: TranscriptStat | null;
  /** T:16411 `ownRunEndedAt` — the clock reading at which a run THIS frame was
   *  streaming last ended, or 0 if none has. D415's transcript follower (PR4)
   *  compares against it so rows this page just wrote are not read back as
   *  somebody else's turn arriving over the top of them. */
  ownRunEndedAt: number;
  /** Monotonic; bumps on every state change so cheap memo keys work. */
  rev: number;
}

export interface SendOptions {
  model?: string;
  effort?: string;
  permission?: PermissionMode;
  /** Pre-composed wire blocks appended by attachments/annotations/app-state (PR2/PR3).
   *  Order is the reading order T composes in: app-state, pictures, annotations. */
  blocks?: string[];
  /** ADDED: the directories THIS message's attachments live in, beyond the
   *  shots dir the spawn line always allows — one Read rule each, granted for
   *  the SESSION (Task 6, T:16657-16668). JSON-encoded onto the wire. */
  readDirs?: string[];
}

/**
 * ESCAPE IS NOT THE PROTOCOL'S, and there is deliberately no method here for it.
 *
 * T's `escapeAction` (T:15948-15953) answers close-viewer / close-composer /
 * exit-annotate and nothing else — three UI states, all of them PR2/PR3, each
 * owned by the component that is open and each stopping the event itself. It has
 * NO branch that touches a run: Escape used to kill the turn whenever nothing
 * else wanted the key, and a reader reaching for it out of habit lost the whole
 * turn to a keystroke never aimed at it (Akshil, 2026-09-03).
 *
 * So the precedence rule lives where it can be honoured — Esc closes the
 * innermost thing open, DOM order decides which that is — and a controller
 * method here would only be a stub with an inert guard around it, overstating a
 * contract the run loop has no part in. A future claimant that needs a
 * protocol-level answer adds the method together with the state it reads.
 */

export interface ChatController {
  getState(): ChatState;
  subscribe(cb: () => void): () => void;

  /** Start a run (T:16460 sendMessage). Resolves when the run is started or refused. */
  sendMessage(text: string, opts?: SendOptions): Promise<void>;
  /** Queue/send a follow-up into the live run (T:16024). `opts` ADDED: notes
   *  and pictures fold into a follow-up exactly as into a fresh turn. */
  sendFollowUp(text: string, opts?: SendOptions): Promise<void>;
  /** Stop the live run (T:15870-16023 stopRun). No-op when idle. */
  stopRun(): Promise<void>;

  /** Permission card (T:13899 buildPermCard → decide). */
  decidePermission(id: string, decision: Decision, scope?: DecisionScope, mode?: SwitchableMode): Promise<void>;
  /** Question card (T:14056 buildQuestionCard → decide with answers/custom). */
  answerQuestion(id: string, answers: Record<string, string[]>, custom?: Record<string, string>): Promise<void>;
  /** Plan card (T:14476 buildPlanCard). `note` ADDED: the free text beside
   *  "Keep planning", <= PLAN_NOTE_LIMIT (2000) chars (T:14601, D146). */
  decidePlan(id: string, decision: Decision, mode?: SwitchableMode, note?: string): Promise<void>;
  /** Dismiss without deciding (T:14118 dismiss). */
  dismissCard(id: string): void;

  /** Answer an app-state request with a snapshot block (T:15804 answerAppState). */
  answerAppState(id: string, block: string): Promise<void>;

  /** Load a session's history and make it current (T:17984 loadHistory). */
  openSession(sessionId: string): Promise<void>;
  /** Re-attach to a run id from the URL (T:17792 resumeRun). */
  resumeRun(runId: string): Promise<void>;
  /** Back to home: clear transcript, drop session_id/run params (T:13031 enterChat/back). */
  newChat(): void;

  /** Release timers/aborts. */
  dispose(): void;
}

/** Deps the controller needs from its host; keeps it testable without DOM. */
export interface ControllerDeps {
  file: string | null;
  agentDir: string;
  params: import("../params/store").ParamsStore;
  /** For the working-line clock and tests. */
  now?: () => number;
  /** Fired after a run starts/ends so hosts can poke task lists (T:16435 chat-activity). */
  onActivity?: () => void;
  /** ADDED: the transport, injectable so bun tests drive the loop with a fake
   *  agent.py. Defaults to `protocol/agent.ts`'s `runAgent`. */
  run?: typeof import("./agent").runAgent;
  /** ADDED: `sleep` for the 400 ms poll cadence and the follow-up wait —
   *  injectable so a test runs the loop without real time (T:16377). */
  sleep?: (ms: number) => Promise<void>;
  /** ADDED: the permission mode the pickers currently show, and the model /
   *  effort they show, read at SEND time (T:16621-16623 curModel/curEffort).
   *  Falls back to the `permission` param and "" when absent. */
  model?: () => string;
  effort?: () => string;
  /** ADDED: `has_pane` is the PAGE's answer, sent on every turn (T:16609). */
  hasPane?: () => boolean;
  /** ADDED: follow-ups the CLI never delivered, handed back to the composer on
   *  a stop (`still_queued`, T:15911). */
  onStranded?: (texts: string[]) => void;
  /** ADDED: every 8th poll (~3.2 s) and once at the run's end — where PR4 hangs
   *  its artifacts read (T:16229, 16330). */
  onArtifactsTick?: () => void;
  /** ADDED: the run ended — PR2/PR3/PR4 hang `annResolveSent` / `snapInvalidate`
   *  here (T:16321-16330). */
  onRunEnded?: () => void;
}

export type { HistoryTurn };
