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
  /** `""` is "the page has no opinion, decide it yourself" — agent.py `main`
   *  maps an empty string to `has_pane=None` and falls back to
   *  `_has_pane(file)`. Sent while the pane is still resolving, because the
   *  value is what the session's MCP roster is built off and a guess sticks
   *  for the whole session (R2-10). */
  has_pane: "0" | "1" | "";
  /** JSON array of folders granted for reading (T:16617). */
  read_dirs: string;
}

export interface PollRequest {
  run_id: string;
  /** Rides along so a poll can refuse another target's run (agent.py:5199-5203). */
  file: string;
  /** `"1"` from the React page: app-state reads come back as in-stream
   *  notice segments instead of being stripped (agent.py `app_reads`). */
  native?: string;
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
  /** `"1"` from the React page (history): app-state reads come back as
   *  in-stream notice segments (agent.py `app_reads`). */
  native?: string;
}
export interface RunIdRequest {
  run_id: string;
}
/** `cancel` — `queued: "1"` when this page had a follow-up in flight for the
 *  turn, so agent.py ends the session tree after the interrupt rather than
 *  letting the CLI answer the queue with nobody watching. */
export interface CancelRequest extends RunIdRequest {
  queued?: string;
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
  cancel: CancelRequest;
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
  /**
   * The CLI's own `tool_use_id` for the call that asked — the SAME id a `tool`
   * segment carries, which is what lets a resolved card be filed back against
   * the exact chip it answered rather than matched by tool name and arrival
   * order (`Transcript.parkPlan`, feedback #18).
   *
   * Optional and possibly `""`: `permission_server.py` records whatever the CLI
   * sent, so a request raised without one — or written by an older build — has
   * nothing here and the name-and-order fallback still applies.
   */
  tool_use_id?: string;
  input: Record<string, unknown> | QuestionInput | PlanInput;
  /** 0 when absent. */
  created_at: number;
  decision: LandedDecision;
  scope: "" | DecisionScope;
  mode: "" | SwitchableMode;
  /** `{questionText: chosenLabels}` once a question was answered, else `{}`. */
  answers: Record<string, string>;

  // -- CLIENT-ONLY annotations (run-controller.ts). Never sent to agent.py. --
  /** `tool_use_id` in this file's own camelCase, stamped once by
   *  `syncPermissions`. The wire field is snake_case because agent.py's is; the
   *  transcript reads this one because every other annotation here is
   *  camelCase, and a card-placement rule reading two spellings of one id is
   *  how the wrong one gets used. */
  toolUseId?: string;
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
  /**
   * THE ANSWER IS HELD, not delivered (the project queue, `/api/tasks/queue/
   * decide`). The reader decided; the folder was busy with another task, so the
   * server stored the decision and will write it the moment the folder frees —
   * at the head of the line, ahead of every message, because an answer somebody
   * is already waiting on outranks new work.
   *
   * The value is the holder's task id ("TASK-041"), or "" when the server could
   * not name it. Present at all is what makes the card say so; the card is
   * `resolved` either way, because from the reader's side the decision is MADE —
   * it is latched, first-writer-wins, and a second click is ignored exactly as
   * it is on a delivered one.
   *
   * CLIENT-ONLY, like every annotation above it: agent.py has never heard of it,
   * and the poll that eventually reports the delivered decision simply replaces
   * the row.
   */
  queuedAhead?: string;
  /**
   * THE SAME FACT, FROM THE SERVER — and a second field rather than the one
   * above for what the one above cannot do: `queuedAhead` is stamped by the
   * click that made the decision, so it lives exactly as long as this document
   * does. Reload, or open the conversation in another tab, and the card came
   * back UNANSWERED with live buttons over a decision that is already stored and
   * waiting its turn — buttons a second reader would then press.
   *
   * `held` rides on the poll's own permission row (the server writes it for a
   * request that has an entry in `held_answers.json`), so it survives the
   * reload the annotation cannot. True means to the card exactly what
   * `queuedAhead` means: latched, no buttons, "◷ Answer queued".
   *
   * IT CARRIES NO NAME. The store is keyed by folder, not by whatever happens to
   * be holding it when a page is rebuilt, so a restored card says which folder
   * it is waiting on rather than which task — and `queuedAhead` is what names
   * the holder for as long as the page that clicked is still open.
   */
  held?: boolean;
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

/** `"external"` is not agent.py's — it is the page's own, written by
 *  `setExternalWorking` for a turn running in someone else's process
 *  (T:17721). The working line branches on `Working.external` for its verb, so
 *  this only has to be a phase the union admits. */
export type Phase =
  | "thinking"
  | "composing"
  | "tooling"
  | "requesting"
  | "retrying"
  | "awaiting"
  | "external";

export interface RetryInfo {
  attempt: number;
  max_retries: number;
  delay_ms: number;
  status: number;
  error: string;
}

/**
 * The plan window as of the last API response (agent.py `_quota_info`, off the
 * CLI's own `rate_limit_event` row — one per response). `status` is the CLI's
 * word: `allowed`, `allowed_warning` (it crossed its own threshold; `utilization`
 * says how far), `rejected` (the turn just died on the limit; `resets_at` is the
 * epoch second the window reopens, and the comeback is scheduled on it).
 * `windows` is every window at once, keyed `five_hour` / `seven_day`.
 */
export interface Quota {
  status: "allowed" | "allowed_warning" | "rejected" | string;
  type: "five_hour" | "seven_day" | string;
  resets_at: number;
  utilization: number | null;
  windows: Record<string, { utilization: number; resets_at: number }>;
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
  /** Absent on an older agent.py. */
  quota?: Quota | null;
  cancelled: boolean;
  tasks_pending: boolean;
  activity: Activity;
  segments: Segment[];
  /** Byte offset in out.jsonl where this payload's window starts — the poll
   *  cursor. A change while a follow-up is outstanding IS the cursor stepping
   *  past its seam (agent.py `_poll`, Bugbot #1099). Absent on an older
   *  agent.py. */
  window?: number;
  /**
   * Where a mid-stream follow-up was ABSORBED into the reply already streaming
   * (agent.py `_absorbed_turn_breaks`). One entry per seam, in file order,
   * each the `segments` count and the `text` length of everything BEFORE that
   * seam — so `[]` (every ordinary poll) means the payload is one turn, and
   * `[{segments: 5, text: 900}]` means `segments.slice(0, 5)` / `text.slice(0,
   * 900)` answered the previous user message and everything after it answers
   * the follow-up.
   *
   * Optional because an older agent.py does not send it, in which case the
   * loop falls back to treating the payload as one turn.
   */
  turn_breaks?: TurnBreak[];
  /**
   * FOLLOW-UPS THE LIVE RUN HAS TAKEN AND THE MODEL HAS NOT ANSWERED YET —
   * the CLI's undrained inbox, in the order they were typed (agent.py `_poll`).
   *
   * THE GAP IT CLOSES. A line typed into a running chat is absorbed by the live
   * host: it goes into the CLI's queue and is answered when the current turn
   * ends. Until then it exists in exactly two places — the CLI's stdin queue,
   * and this page's own optimistic bubble — and the second of those is client
   * memory. So a reload, or the standing watch's `refreshHistory`, replaced the
   * transcript with the JSONL, which does not have the message either (nothing
   * has consumed it), and the reader's own words simply vanished until the
   * model got to them (Akshil, 2026-09-12).
   *
   * The run has the list, so the run reports it. Absent on an older agent.py,
   * which is the same as an empty one: the optimistic bubbles are all there is.
   */
  inbox?: InboxMessage[];
}

/**
 * One undrained follow-up (`PollResponse.inbox`).
 *
 * `id` is the send's own identity, stable across polls, so a bubble drawn for it
 * is the SAME bubble on the next lap rather than a new one in the same place.
 * `text` is what the reader typed. `at` is when the host took it — an ISO stamp
 * or an epoch, whichever the server sends, and this page only ever ORDERS by it.
 */
export interface InboxMessage {
  id: string;
  text: string;
  at?: string | number;
}

/** One seam in a poll payload — see `PollResponse.turn_breaks`. */
export interface TurnBreak {
  /** Number of `segments` entries before the seam. */
  segments: number;
  /** Number of `text` characters before the seam. */
  text: number;
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
  // ── the three below are NOT the agent's. ────────────────────────────────
  //
  // A chat whose first message was QUEUED has no transcript — nothing of it has
  // run — so `sessions` cannot list it and the landing had no row for it at all
  // (`sched/waiting-chats`). Its row is built from the `/api/tasks` listing and
  // folded into this same list, because a waiting chat is not a different kind
  // of thing from one that ran: it is the same conversation, earlier.
  //
  // Absent on every row the agent produced, which is what tells the two apart.
  /** The leader entry this conversation is waiting AS — its only name until the
   *  scheduler gives it a session (`platform/lib/queue.QUEUED_PARAM`). */
  queuedEntry?: string;
  /** Its number, for the row: the task exists the moment the entry does. */
  taskId?: string;
  /** Where the row opens — the queued chat URL, built once where the task's own
   *  target is in hand rather than re-derived by the component drawing it. */
  href?: string;
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
/** A turn that FAILED — Claude Code's own `isApiErrorMessage` record (no
 *  network, a 429, an exhausted usage limit), which agent.py `_history` lifts
 *  out of the assistant stream so a restored conversation shows the failure in
 *  red exactly as the live run did (agent.py `_is_api_error_row`). */
export interface HistoryErrorTurn {
  role: "error";
  text: string;
  /** The transcript's `quotaLimits` on a failed row — only a limit hit has it. */
  quota?: Quota;
}
export type HistoryTurn = HistoryUserTurn | HistoryAssistantTurn | HistoryErrorTurn;
export interface TranscriptStat {
  path: string;
  mtime: number;
  size: number;
}
export interface HistoryResponse {
  turns: HistoryTurn[];
  transcript: TranscriptStat;
  /** agent.py `_history_live`: the run still going for this chat, with its
   *  cards, so transcript and card paint in one frame. `""` = nothing live (an
   *  answer too); absent = an older server, and the page discovers as before. */
  live_run?: string;
  permissions?: PermissionRow[];
  mode?: PermissionMode | "";
  /**
   * THE LIVE RUN'S UNDRAINED FOLLOW-UPS — the same list `PollResponse.inbox`
   * carries, on the read a RELOADING chat makes first (agent.py `_history_live`).
   *
   * This is the half that actually fixes the reload. The poll's copy keeps the
   * bubbles up while a page stays open; a page that comes BACK reads `history`
   * before it has a run to poll, and without the list here the reader's held
   * follow-ups would be missing for the whole of that window — which is exactly
   * the moment they are looking for them. Only meaningful beside `live_run`:
   * nothing is held when nothing is running.
   */
  inbox?: InboxMessage[];
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
