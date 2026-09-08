// Wire types for `templates/claude/agent.py` as reached through POST /api/run
// (`{py: <tplDir>/agent.py, params: {action, ...fields}}`). Every request field
// crosses into Python STRING-shaped (the param binder, agent.py:5179-5186);
// nested data (`read_dirs`, `answers`, `custom`, `state`) is JSON.stringify'd
// by the caller. Response shapes are read off each handler's `return` — the
// `agent.py:line` cites point at them. Inventory: 04-core-chat.md §B/§C.

// ---- actions ---------------------------------------------------------------

/** Every `action` main() dispatches (agent.py:5187-5292). Note: there is no
 *  `once` action — "once" is a `decide` SCOPE literal (T:14098, 14548). */
export type Action =
  | "start"
  | "poll"
  | "decide"
  | "app_state"
  | "sessions"
  | "live_run"
  | "defaults"
  | "history"
  | "snapshots"
  | "snapshot_plan"
  | "snapshot_revert"
  | "shots_dir"
  | "image_to_png"
  | "terminal_command"
  | "cancel"
  | "live_host"
  | "send";

/** A permission mode as the CLI spells it (PERMISSION_MODES, agent.py). */
export type PermissionMode = "plan" | "prompt" | "acceptEdits" | "auto";
/** The two modes a card may switch INTO (SWITCHABLE_MODES, T:13756). */
export type SwitchableMode = "acceptEdits" | "auto";
export type Decision = "allow" | "deny";
export type DecisionScope = "once" | "session";
/** What lands on disk for a permission row: "" = still open. */
export type LandedDecision = "" | "allow" | "deny" | "expired";

// ---- requests --------------------------------------------------------------

export interface StartRequest {
  file: string;
  message: string;
  /** "" = a new session (T:16605-16623). */
  session_id: string;
  model: string;
  effort: string;
  /** `params.permission || "prompt"` (DEFAULT_PERMISSION, T:11911). */
  permission_mode: string;
  /** "0" | "1" — from `noPane` (T:16108-16118). */
  has_pane: "0" | "1";
  /** JSON array of folders granted for reading (T:16617). */
  read_dirs: string;
}

export interface PollRequest {
  run_id: string;
  /** Rides along so a poll can refuse another target's run (agent.py:5199-5203). */
  file: string;
}

/** `decide` for a permission card (T:13979-13996). */
export interface DecidePermissionRequest {
  run_id: string;
  request_id: string;
  decision: Decision;
  scope: DecisionScope;
  /** "" | "auto" — "let Claude decide from here" (T:13895-13899). */
  mode: "" | SwitchableMode;
}

/** `decide` for an AskUserQuestion card (T:14059-14082). */
export interface DecideQuestionRequest {
  run_id: string;
  request_id: string;
  decision: Decision;
  scope: "once";
  /** JSON `{questionText: "label, label[, typed]"}`; omitted on deny. */
  answers?: string;
  /** JSON `{questionText: typedText}`; always sent alongside `answers`. */
  custom?: string;
}

/** `decide` for an ExitPlanMode card (T:14539-14584). */
export interface DecidePlanRequest {
  run_id: string;
  request_id: string;
  decision: Decision;
  scope: "once";
  /** "" | picker mode ∈ SWITCHABLE_MODES (landing mode on approve). */
  mode: "" | SwitchableMode;
  /** ≤ PLAN_NOTE_LIMIT (2000) chars, sent with "Keep planning". */
  note: string;
}

export type DecideRequest = DecidePermissionRequest | DecideQuestionRequest | DecidePlanRequest;

export interface AppStateRequest {
  run_id: string;
  request_id: string;
  /** JSON snapshot; never a bare null (T:15810-15836). */
  state: string;
}

export interface SendRequest {
  run_id: string;
  /** The composed outgoing wire text (T:16094-16102). */
  message: string;
  read_dirs: string;
  model: string;
  effort: string;
  permission_mode: string;
}

export interface FileRequest {
  file: string;
}
export interface FileSessionRequest {
  file: string;
  /** Optional for `live_run` (target as a whole), required for the rest. */
  session_id: string;
}
export interface RunIdRequest {
  run_id: string;
}
export interface SnapshotsRequest {
  file: string;
  /** "" | "0" = don't enrich (agent.py:5239-5250). */
  enrich?: string;
  /** "0" | "false" = skip difflib; absent = yes. */
  deltas?: string;
}
export interface SnapshotPlanRequest {
  file: string;
  version_id: string;
}
export interface SnapshotRevertRequest {
  file: string;
  version_id: string;
  /** Only a positive string ("1") counts (agent.py:5253-5258). */
  confirm_unique: string;
}
export interface ImageToPngRequest {
  /** The file the page just uploaded under SHOTS (agent.py:5263-5269). */
  path: string;
}

export interface AgentRequests {
  start: StartRequest;
  poll: PollRequest;
  decide: DecideRequest;
  app_state: AppStateRequest;
  sessions: FileRequest;
  live_run: FileSessionRequest;
  defaults: FileRequest;
  history: FileSessionRequest;
  snapshots: SnapshotsRequest;
  snapshot_plan: SnapshotPlanRequest;
  snapshot_revert: SnapshotRevertRequest;
  shots_dir: Record<string, never>;
  image_to_png: ImageToPngRequest;
  terminal_command: FileSessionRequest;
  cancel: RunIdRequest;
  live_host: FileSessionRequest;
  send: SendRequest;
}

// ---- segments (agent.py:3253 _segments_from_rows; T:15591-15635) -----------

export interface TextSegment {
  kind: "text";
  text: string;
}
export interface ThinkingSegment {
  kind: "thinking";
  /** Never all-whitespace — those are dropped (agent.py:3486-3487). */
  text: string;
}
export type ToolStatus = "running" | "ok" | "error";
export interface ToolImage {
  media_type: string;
  /** base64 (agent.py:3207-3208). */
  data: string;
}
export interface ToolSegment {
  kind: "tool";
  /** tool_use id, "" when the block had none. */
  id: string;
  name: string;
  /** The raw tool input; `{}` when it was not a dict. */
  input: Record<string, unknown>;
  status: ToolStatus;
  /** null while running; capped at SEGMENT_OUTPUT_CAP=4000 chars once settled. */
  output: string | null;
  images: ToolImage[];
}
export interface NoticeSegment {
  kind: "notice";
  text: string;
  /** From the row, may be "". */
  status: string;
}
/** Unknown kinds render as text (T:15630-15635). */
export type Segment = TextSegment | ThinkingSegment | ToolSegment | NoticeSegment;

// ---- permission / question / plan requests (agent.py:1403-1437) ------------

export interface QuestionOption {
  label: string;
  description?: string;
}
export interface Question {
  question: string;
  header?: string;
  options: QuestionOption[];
  multiSelect?: boolean;
}
/** `input` of an AskUserQuestion request (ANSWERABLE_TOOL, T:13767). */
export interface QuestionInput {
  questions: Question[];
  [extra: string]: unknown;
}
/** `input` of an ExitPlanMode request (PLAN_TOOL, T:13761). */
export interface PlanInput {
  /** Markdown. */
  plan: string;
  [extra: string]: unknown;
}

export interface PermissionRow {
  id: string;
  /** Tool name: "Bash", "Edit", …, "AskUserQuestion", "ExitPlanMode". */
  tool: string;
  input: Record<string, unknown> | QuestionInput | PlanInput;
  /** 0 when absent. */
  created_at: number;
  decision: LandedDecision;
  scope: "" | DecisionScope;
  mode: "" | SwitchableMode;
  /** `{questionText: chosenLabels}` once a question was answered, else `{}`. */
  answers: Record<string, string>;

  // -- CLIENT-ONLY annotations (run-controller.ts). Never sent to agent.py. --
  /** Where the card sits: `open` cards are pinned last, as one contiguous block
   *  above the status line (T:14680 pinOpenCards); a `parked` one was answered
   *  and belongs where it was answered (T:14728 parkResolvedCard). */
  placement?: "open" | "parked";
  /** The assistant turn a parked card was filed into, or null when there was no
   *  live turn to file it in (T:14732 — then it keeps its place). */
  parkedIn?: string | null;
  /** The live permission mode as of the poll that delivered this row — decides
   *  whether "let Claude decide from here" is offered (T:13895). */
  liveMode?: PermissionMode;
  /** "Could not send that: …" — the decide failed, so the controls come back
   *  (T:14113, 13999). */
  sendError?: string;
  /** THE RUN THE CARD WAS BUILT WITH (T:16325 hands `run_id` to
   *  `buildPermCard`, which closes over it). A decide posts THIS, not whatever
   *  run happens to be live at click time: a follow-up's respawn re-points the
   *  live run, and the same click would then land on a process that never asked
   *  the question. */
  runId?: string;
}

/** An UNANSWERED app-state request (agent.py:1970-1996). */
export interface AppStateRow {
  id: string;
  reason: string;
  created_at: number;

  // -- CLIENT-ONLY annotations (run-controller.ts). --
  /** How many polls this request has been seen unanswered for (T:15729). */
  pollsSeen?: number;
  /** `pollsSeen` passed APP_STATE_NULL_POLLS (5, ~2 s): this is not a pane
   *  mid-reload, it is a project with no app to read, so the pane should answer
   *  with the explicit sentence rather than keep waiting (T:15726-15740). */
  waitedOut?: boolean;
  /** The run that ASKED (T:16260 passes the loop's own `run_id`). The snapshot
   *  is posted back to this run, not to whatever is live when the pane answers
   *  — a respawn in between would otherwise answer a request the new run never
   *  made. */
  runId?: string;
}

export interface SkillRow {
  id: string;
  skill: string;
}

// ---- poll (agent.py:3799; full return 4288-4316) -------------------------

export type Phase = "thinking" | "composing" | "tooling" | "requesting" | "retrying" | "awaiting";

export interface RetryInfo {
  attempt: number;
  max_retries: number;
  delay_ms: number;
  status: number;
  error: string;
}

export interface ActivityTool {
  id: string;
  name: string;
  detail: string;
}
export interface ActivityTask {
  id: string;
  description: string;
}
/** agent.py:4307-4315. */
export interface Activity {
  tool: ActivityTool | null;
  tools_open: number;
  tool_input_bytes: number;
  thinking_tokens: number;
  hook: string;
  tasks: ActivityTask[];
  agent_rows: number;
}

/** The steady-state poll body. */
export interface PollResponse {
  text: string;
  done: boolean;
  session_id: string;
  /** "" normally; non-empty does NOT narrow the shape. */
  error: string;
  tokens: number;
  phase: Phase;
  /** The run's original first user message. */
  message: string;
  permissions: PermissionRow[];
  /** [] once done. */
  app_state: AppStateRow[];
  /** Live permission mode (`_live_mode`). */
  mode: PermissionMode;
  skills: SkillRow[];
  retry: RetryInfo | null;
  retry_total: number;
  retry_status: number;
  cancelled: boolean;
  tasks_pending: boolean;
  activity: Activity;
  segments: Segment[];
}

/** The narrow early-exit body: unknown run_id / another target (agent.py:3802,3824). */
export interface PollRefused {
  text: "";
  done: true;
  session_id: "";
  error: string;
  permissions: [];
  app_state: [];
  skills: [];
  retry: null;
  retry_total: 0;
  retry_status: 0;
  segments: [];
}

// ---- other responses ---------------------------------------------------------

/** Handlers that answer `{error}` and nothing else on failure. */
export interface ErrorOnly {
  error: string;
}

/** agent.py:2452 / 2350 (+ main()'s own guards 5188-5191). */
export type StartResponse = { run_id: string; error?: undefined } | ErrorOnly;

/** agent.py:2971-3038 — exactly one of the three. */
export type SendResponse = { sent: true } | { respawn: true } | ErrorOnly;

/** agent.py:1601-1607; error branches carry only `error`. */
export type DecideResponse =
  | {
      decided: string;
      decision: "allow" | "deny" | "expired";
      scope: "" | DecisionScope;
      mode: "" | SwitchableMode;
      answers: Record<string, string>;
      error?: undefined;
    }
  | ErrorOnly;

/** agent.py:2015-2038. `retry: true` = un-claim and retry next poll (T:15810-15836). */
export type AppStateResponse = { answered: string } | { error: string; retry: boolean };

/** agent.py:5075-5176. `still_queued` only when the live host answered the
 *  interrupt in time; its element shape is session_host's (opaque here). */
export interface CancelResponse {
  cancelled: string;
  still_queued?: unknown[];
}

/** agent.py:2759-2802 / 2902-2927 — "" when none. */
export interface RunIdResponse {
  run_id: string;
}

/** agent.py:4396. */
export interface DefaultsResponse {
  model: string;
  effort: string;
  source: "" | "session" | "settings";
}

/** agent.py:4625-4627 (`_cli_sessions`), newest first. */
export interface SessionRow {
  id: string;
  /** First human message, ≤80 chars. */
  preview: string;
  created_at: number;
  last_used: number;
  cwd: string;
  /** File the pane was opened on, "" if none. */
  pane: string;
  running: boolean;
}
export interface SessionsResponse {
  sessions: SessionRow[];
}

/** agent.py:5024-5025 / 4990-5050. */
export interface HistoryUserTurn {
  role: "user";
  /** App-state block stripped. */
  text: string;
  uuid: string;
}
export interface HistoryAssistantTurn {
  role: "assistant";
  text: string;
  /** Absent on a text-only turn (agent.py:5041). */
  segments?: Segment[];
  /** Only on the LAST turn and only when true (agent.py:5049-5050). */
  stopped?: true;
}
export type HistoryTurn = HistoryUserTurn | HistoryAssistantTurn;
export interface TranscriptStat {
  path: string;
  mtime: number;
  size: number;
}
export interface HistoryResponse {
  turns: HistoryTurn[];
  transcript: TranscriptStat;
}

/** agent.py:904 / 868,883. */
export type TerminalCommandResponse = { command: string; cwd: string; error?: undefined } | ErrorOnly;

/** agent.py:1136. */
export type ShotsDirResponse = { dir: string; error?: undefined } | ErrorOnly;

/** agent.py:1344. */
export type ImageToPngResponse =
  | {
      path: string;
      width: number;
      height: number;
      bytes: number;
      source_w: number;
      source_h: number;
      error?: undefined;
    }
  | ErrorOnly;

// ---- snapshots (shared/file_history.py via agent.py:4650-4830) --------------

export interface SnapshotCurrent {
  exists: boolean;
  size: number;
  lines: number | null;
}
export interface SnapshotVersion {
  id: string;
  session: string;
  version: number;
  existed: boolean;
  path: string | null;
  mtime: number;
  size: number;
  lines: number | null;
  differs: boolean;
  added: number;
  removed: number;
  exact: boolean;
}
export interface SnapshotBlocker {
  session: string;
  version: number | null;
  mtime: number | null;
  reason: string;
}
/** file_history.timeline() (857-892). */
export interface SnapshotsTimeline {
  file: string;
  hash: string;
  available: boolean;
  writable: boolean;
  writable_reason: string;
  current: SnapshotCurrent;
  versions: SnapshotVersion[];
  position: string | null;
  revert: string | null;
  offer: boolean;
  offer_reason: string;
  at_earliest: boolean;
  unconfirmed: boolean;
  blocking: SnapshotBlocker[];
  enriched: boolean;
  unique_current: boolean;
  skipped: SnapshotBlocker[];
  note: string;
}
export type SnapshotsResponse = SnapshotsTimeline | ErrorOnly;

export interface SnapshotDiff {
  lines: string[];
  changed: number;
  truncated: boolean;
  reason: string;
}
export interface SnapshotPlanOk {
  ok: true;
  id: string;
  session: string;
  version: number;
  action: "restore" | "delete";
  added: number;
  removed: number;
  exact: boolean;
  diff: SnapshotDiff;
  mtime: number;
  current: SnapshotCurrent;
  position: string | null;
  target: { size: number; lines: number | null; existed: boolean };
  unique_current: boolean;
  at_earliest: false;
  unconfirmed: false;
  blocking: [];
  skipped: SnapshotBlocker[];
  writable: true;
  writable_reason: "";
}
export interface SnapshotPlanRefused {
  ok: false;
  at_earliest: boolean;
  unconfirmed: boolean;
  blocking: SnapshotBlocker[];
  skipped: SnapshotBlocker[];
  current: SnapshotCurrent;
  error: string;
}
export type SnapshotPlanResponse = SnapshotPlanOk | SnapshotPlanRefused | ErrorOnly;

/** agent.py:4786-4827. */
export type SnapshotRevertResponse =
  | {
      ok: true;
      action: "restore" | "delete";
      id: string;
      bytes: number;
      /** Absent when the post-revert timeline re-read failed. */
      timeline?: SnapshotsTimeline;
    }
  | SnapshotPlanRefused
  | { error: string; plan?: SnapshotPlanOk };

export interface AgentResponses {
  start: StartResponse;
  poll: PollResponse | PollRefused;
  decide: DecideResponse;
  app_state: AppStateResponse;
  sessions: SessionsResponse | ErrorOnly;
  live_run: RunIdResponse | ErrorOnly;
  defaults: DefaultsResponse | ErrorOnly;
  history: HistoryResponse | ErrorOnly;
  snapshots: SnapshotsResponse;
  snapshot_plan: SnapshotPlanResponse;
  snapshot_revert: SnapshotRevertResponse;
  shots_dir: ShotsDirResponse;
  image_to_png: ImageToPngResponse;
  terminal_command: TerminalCommandResponse;
  cancel: CancelResponse;
  live_host: RunIdResponse | ErrorOnly;
  send: SendResponse;
}

// ---- sibling scripts ---------------------------------------------------------

/** `./app.py {dir}` (T:5417): the folder's entry html, "" / absent when none. */
export interface AppEntryResponse {
  entry?: string;
  error?: string;
}

/** `./artifacts.py` (T:18482-18553): `{action:"list", file}` and friends. */
export interface ArtifactsListResponse {
  artifacts?: unknown[];
  error?: string;
}
