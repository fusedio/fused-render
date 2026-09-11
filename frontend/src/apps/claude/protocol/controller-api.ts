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
/** PR2: the attachment pipeline's own contract (`shots/types.ts`). Pure types —
 *  the protocol layer never calls into it. */
import type { Receipt } from "../shots/types";

/** One transcript row. History turns and live turns share this shape. */
export interface UserTurn {
  role: "user";
  /** Stable key: history uuid, or `u:<sendSeq>` for a live send. */
  key: string;
  /** Display text — wire blocks (<live-app-state>, <pane-shot>, <annotations>) already stripped (T:10404-10608). */
  text: string;
  /** Raw outgoing text incl. blocks, for the "what was sent" popover (T:10970). */
  raw?: string;
  /**
   * This message carried a `<live-app-state>` description of the app the pane
   * was showing. The UI draws the receipt legacy draws — "app state attached",
   * a faint caption under the bubble (T:16588-16596, `.user .attach` at
   * T:1802) — because the push channel is otherwise invisible and the reader
   * has no way to know the agent was told what they were looking at.
   */
  appState?: true;
  uuid?: string;
  /**
   * ADDED (PR2): the receipt rows this turn wears — "screenshot attached",
   * "file attached: notes.csv", or the refusal and its reason (T:10815
   * `shotReceipt`).
   *
   * Set only on a turn THIS page sent, because only this page holds the blob
   * URLs its thumbnails were drawn from. A turn read back off disk has none and
   * its receipts are rebuilt from `raw`'s own `<pane-shot>` block instead
   * (T:10903 `shotRestoreReceipt`, `ui/attachApi.restoreReceipts`) — so the two
   * roads meet, and a picture visible while the session lasts is still there
   * when the session is reopened.
   */
  attachments?: Receipt[];
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
  /** ⏹ a stop, ◍ an app-state read, ◆ a skill (T:13722) — and ◷ a scheduled
   *  message, which is PR4's own row: "Your scheduled message is running now."
   *  and its foreign-session twin (T:17427-17434). Added to the union rather
   *  than spelled at the call site so a renderer's glyph switch stays total. */
  glyph: "\u23f9" | "\u25cd" | "\u25c6" | "\u25f7";
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
  /** The plan window the failure carried (kind `limit` only). */
  quota?: import("./types").Quota;
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
  /** ADDED (P3R1-8): the chat could not BOOT — no target, or the folder's
   *  template never resolved (a stalled `/api/fs/stat`, the 8 s backstop). Its
   *  own kind because the copy is the only one the reader can act on without
   *  knowing anything about the app's insides: `ui/TroubleView`'s `SAID` gives
   *  it two plain sentences and no verbatim block. */
  | "boot"
  | "generic";

export interface Trouble {
  kind: TroubleKind;
  message: string;
  /** Verbatim traceback / stderr when present. */
  detail?: string;
  /** For `limit`: the window that refused the turn, so the card can say WHEN
   *  it reopens (and that the comeback is already scheduled) instead of
   *  pointing the reader at the CLI's own sentence for the time. */
  quota?: import("./types").Quota;
  /** For `limit`: the comeback is on the schedule (server confirmed). */
  scheduled?: boolean;
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
  /**
   * A RESTORE IS NOT FINISHED UNTIL ADOPTION HAS SPOKEN. True from the first
   * frame of a session restore until either nothing turns out to be live or
   * the adopted run's first poll has landed (cards included).
   *
   * `historyLoading` alone goes false the instant the transcript arrives, and
   * a task parked on an AskUserQuestion then painted twice: the prose first,
   * scrolled to the bottom, and the card a poll later on top of it with a
   * second scroll — the "double flash" on the cards wall, where the tile is
   * small enough that the jump is the whole tile (feedback R2-11/R2-13).
   * A renderer holds its enter animation (`is-settling`) while this is true so
   * the transcript and the card paint in ONE frame.
   */
  adopting: boolean;
  transcript: TranscriptStat | null;
  /** T:16411 `ownRunEndedAt` — the clock reading at which a run THIS frame was
   *  streaming last ended, or 0 if none has. D415's transcript follower (PR4)
   *  compares against it so rows this page just wrote are not read back as
   *  somebody else's turn arriving over the top of them. */
  ownRunEndedAt: number;
  /**
   * ADDED (PR4, P4-10): how many turns have been REPAIRED into this transcript
   * from a probe payload — a run that finished while no frame was attached.
   *
   * T scrolls to a repaired turn UNCONDITIONALLY (T:17851 `scrollBottom()` at
   * the end of the done branch), and it has to: the repair appends a whole turn
   * with no `running` → `idle` edge for the settle-scroll to hang off, so a
   * reader who had scrolled up saw nothing appear at all. A nonce rather than a
   * boolean, because two repairs in a row are two scrolls and a flag that is
   * already `true` emits nothing.
   */
  repaired: number;
  /**
   * ADDED (PR4): how many times the VISIBLE CONVERSATION has been replaced —
   * bumped by `openSession` and by nothing else, which is where T calls
   * `scheduleResetForNewTranscript()` (T:18000, `loadHistory` non-refresh).
   *
   * A counter rather than the session id, because the id is not the same fact:
   * it also changes when the first poll of a brand-new chat reports one
   * (`noteSessionId`), mid-run, where a reset re-arms the schedule baseline and
   * the next tick then writes off a scheduled run that fired in that window.
   * Starts at 0, so a reader can skip the mount.
   */
  transcriptGen: number;
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
  /** ADDED (PR2): the receipts the turn this send posts should wear. The
   *  controller only carries them onto the bubble — the pipeline builds them,
   *  and the same list goes back to the tray if the send never lands. */
  attachments?: Receipt[];
  /**
   * ADDED (PR3): an opaque id for THIS send, minted by the caller and echoed
   * back verbatim in `onSendReturned`.
   *
   * The hand-back used to be attributed by COUNTING: the caller sampled a
   * "sends returned so far" counter before its send and compared it after. A
   * second submit inside the first send's window bumped that counter — the
   * controller refuses a second send out loud, which is a hand-back — and the
   * FIRST send, already delivered, read the bump as its own failure and rolled
   * its notes and its overview back (Bugbot, PR #1074). An id per send asks the
   * question of the right send.
   */
  sendId?: string;
  /**
   * ADDED (PR3): a user bubble ALREADY on screen that this send's bubble should
   * ADOPT rather than duplicate.
   *
   * A send carrying annotations photographs the pane before it can compose the
   * wire, and the composer's box is empty from the keystroke: for the width of
   * that capture the transcript held nothing and the message read as dropped.
   * So the caller posts the typed words optimistically (`postOptimisticUser`)
   * and hands the key down here; `addUser` fills that very row in place, so
   * there is exactly one bubble however slow the capture was. A refused send
   * drops it (`returnSend`).
   */
  optimisticKey?: string;
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

/** T:17509 — `adoptLiveRun`'s two knobs. The standing watch takes ONE lap
 *  (it is re-armed every 5 s and by three events, so laps of its own would only
 *  duplicate the timer) and asks quietly. */
export interface AdoptOptions {
  laps?: number;
  quiet?: boolean;
}

/** T:17747-17765 — `resumeRun`'s three postures.
 *
 *  `neverShown`: this frame has never had THIS run's turn on screen, which is
 *  something only the caller can know (a scheduled send that fired after the
 *  render). It turns OFF the `matches` heuristic: with it, identical text is a
 *  COINCIDENCE — the same prompt sent twice — never this run's own line.
 *
 *  `quiet`: print the run's message UNLESS this transcript is already showing
 *  it. The standing watch adopts turns nobody on this page started, and those
 *  split two ways — a run whose message IS on screen (re-printing it would be
 *  the page inventing a turn) and one whose message is NOT (a send made in
 *  another tab, whose words belong here as much as the reply does).
 *
 *  `retryUnknown`: a frame handed a run id by its EMBEDDER can boot before the
 *  freshly created run dir is visible to the agent — a race, not a stale
 *  bookmark. OPT-IN, and only boot opts in (T:17749): unset, it follows
 *  `!(quiet || neverShown)`, because an id that came from `live_run` rather than
 *  from a caller is not racing a spawn — it has been pruned, and the wait is 5 ×
 *  700 ms spent inside the `sending` gate for nothing (P4-18). */
export interface ResumeOptions {
  neverShown?: boolean;
  quiet?: boolean;
  retryUnknown?: boolean;
}

export interface ChatController {
  getState(): ChatState;
  subscribe(cb: () => void): () => void;

  /** Start a run (T:16460 sendMessage). Resolves when the run is started or refused. */
  sendMessage(text: string, opts?: SendOptions): Promise<void>;
  /**
   * PR3: post the typed words as a user bubble NOW — before a caller's own
   * async send window (an annotation round's pane capture) — and answer the key
   * to hand back as `SendOptions.optimisticKey`, which is what makes the real
   * bubble adopt this row instead of adding a second. Empty text posts nothing
   * and answers "".
   */
  postOptimisticUser(text: string): string;
  /** Drop an optimistic bubble whose send never reached `sendMessage` at all. */
  dropOptimisticUser(key: string): void;
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
  /** Re-attach to a run id from the URL (T:17792 resumeRun). `opts` ADDED in
   *  PR4 — the three postures the standing watch and the scheduled-run poller
   *  need (see `ResumeOptions`). */
  resumeRun(runId: string, opts?: ResumeOptions): Promise<void>;
  /**
   * Ask the SESSION whether a turn is in flight and attach to it if so
   * (T:17506 `adoptLiveRun`) — the answer for every host that opens a chat by
   * `session_id` alone: the sidebar, the content mode, the cards wall and
   * Peek. Without it a live run only ever showed when a `run` param happened
   * to be on the URL, so an open permission card had nothing polling to
   * deliver it (feedback #25).
   *
   * `openSession` already calls this itself when no `run` param is set, so a
   * host that restores a conversation gets it for free; it is exported for the
   * boot paths that attach without going through a history load.
   *
   * Resolves when the watch ends — either something was adopted and its turn
   * finished, or ~3 s of laps found nothing.
   */
  adoptLiveRun(sessionId: string, opts?: AdoptOptions): Promise<void>;

  /**
   * RE-ASK WHAT THE CONVERSATION IS (T:17972-17987 `loadHistory(id,{refresh:1})`).
   * ADDED in PR4 for the transcript follower: a turn driven from outside this
   * app writes no run dir, so the only honest repair is the one the reader was
   * doing by hand — reload the transcript.
   *
   * REFRESH MODE, and the difference from `openSession` is the whole point: no
   * skeleton, no scroll reset, no card/memo wipe, no `session_id` write. The
   * turns and the watermark are replaced; everything else about the visible
   * conversation stays exactly as it was.
   */
  refreshHistory(sessionId: string): Promise<void>;

  /**
   * The working line for a turn happening SOMEWHERE ELSE (T:17715-17732
   * `setExternalWorking`). Reuses the one the poll loop uses so a reader does
   * not have to learn a second shape of "busy", minus the two things this page
   * cannot honestly offer: a stop button (the process is not ours) and a token
   * count (we are reading a file, not a stream).
   *
   * A no-op while a run this frame owns is live: that line is the real one.
   */
  setExternalWorking(on: boolean): void;

  /** A ◷ / ⏹ / ◍ / ◆ row in the transcript (T:13722 `addNote`). ADDED so the
   *  scheduled-run poller can say what it just attached to. */
  addNote(text: string, glyph?: NoteTurn["glyph"]): void;

  /** `activeRun || sending` — the one question both PR4 watchers ask before
   *  touching the transcript (T:17429, 17604). Read live, never memoised: it is
   *  checked adjacent to the call it guards. */
  isBusy(): boolean;
  /** `!!activeRun` alone — narrower than `isBusy`, which also counts a send in
   *  flight. PR4's transcript follower needs the narrow one for the external
   *  working line's OFF edge (T:17709 gates on `!activeRun`). */
  hasActiveRun(): boolean;
  /**
   * HAS THIS PAGE ALREADY TAKEN THIS RUN? True for a run this controller sent,
   * re-attached to from the URL, or adopted through the standing watch.
   *
   * ADDED for the schedule poller, which reads a listing rather than the
   * transcript and so cannot otherwise tell a run that fired from one it has
   * already shown. The two watchers poll at different rates (5 s vs 15 s), so
   * the watch normally adopts a fired scheduled run first; without this the
   * poller re-attached to it after the turn ended and appended the turn twice
   * (Bugbot PR #1075). Same SCHEDULE_ATTACHED semantics as the poller's own
   * baseline (T:16746-16765) — an attached run is never resumable.
   */
  hasShownRun(runId: string): boolean;
  /** Back to home: clear transcript, drop session_id/run params (T:13031 enterChat/back). */
  newChat(): void;

  /**
   * ADDED (PR2): swap the receipts under the user turn that was sent with
   * `receipts` for `next` — the rows re-pointed at the copy on disk once the
   * send landed, so the blob URLs they were drawn with can be revoked
   * (`shots/attach.settleReceipts`). The ARRAY IS THE ADDRESS, like the
   * hand-back's; a send whose bubble is gone settles nothing.
   */
  settleAttachments(receipts: Receipt[], next: Receipt[]): void;

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
  /** ADDED (owner E2E R1, F5): the transcript restore, as its own road. The
   *  host passes `protocol/history.ts`'s `fetchHistory` — the in-process
   *  `/api/claude-sessions/history` route, with `/api/run` behind it — so a
   *  chat opens without waiting on a Python subprocess. Absent (tests), the
   *  loop asks `run("history")` as before. */
  history?: (
    file: string,
    sessionId: string,
  ) => Promise<import("./types").HistoryResponse & { error?: string }>;
  /** ADDED: the comeback after a usage limit. Injectable so bun tests see the
   *  body without a server; defaults to `@platform/lib/api`'s `scheduleMessage`
   *  (POST /api/schedule). */
  schedule?: (body: {
    target: string;
    message: string;
    due: string;
    session_id: string;
    title: string;
  }) => Promise<unknown>;
  /** ADDED: `sleep` for the 400 ms poll cadence and the follow-up wait —
   *  injectable so a test runs the loop without real time (T:16377). */
  sleep?: (ms: number) => Promise<void>;
  /** ADDED: the permission mode the pickers currently show, and the model /
   *  effort they show, read at SEND time (T:16621-16623 curModel/curEffort).
   *  Falls back to the `permission` param and "" when absent. */
  model?: () => string;
  effort?: () => string;
  /** ADDED: `has_pane` is the PAGE's answer, sent on every turn (T:16609). */
  /**
   * The page's answer to `has_pane`, and it is TRI-STATE: `true` a pane is
   * there, `false` there is provably none, `null` NOT DECIDED YET.
   *
   * `null` is the one that matters. `has_pane` is what decides the spawned
   * session's MCP roster — agent.py builds `mcp.json` and `--allowed-tools`
   * off it once, at spawn, and `_send` cannot repair a live host afterwards
   * (`host.json` does not even record the pane) — so a `false` sent while the
   * pane's stat was still in flight took the `app_state` tool away for the
   * whole session, and the CLI reported it as unreachable (feedback R2-10).
   * `null` sends the field EMPTY, which is agent.py's own "you decide": it
   * falls back to `_has_pane(file)`, the authority that does not race.
   */
  hasPane?: () => boolean | null;
  /** ADDED: follow-ups the CLI never delivered, handed back to the composer on
   *  a stop (`still_queued`, T:15911). */
  onStranded?: (texts: string[]) => void;
  /** ADDED: every 8th poll (~3.2 s) and once at the run's end — where PR4 hangs
   *  its artifacts read (T:16229, 16330). */
  onArtifactsTick?: () => void;
  /** ADDED: the run ended — PR2/PR3/PR4 hang `annResolveSent` / `snapInvalidate`
   *  here (T:16321-16330). */
  onRunEnded?: () => void;
  /** ADDED: the `run` param pointed at a run that does not exist — a bookmarked
   *  mid-run URL, a pruned tmp. NOT a run ending, so it is deliberately not
   *  `onRunEnded`: nothing was streamed and nothing on disk changed, so the
   *  snapshot chain is as fresh as it was. What DOES have to happen is the
   *  annotation hand-back, which is all T's own branch does (T:17792). */
  onRunAbandoned?: () => void;
  /**
   * ADDED (PR4, P4-17): put the caret back in the composer at the end of a
   * re-attach — T's `focusBox(box)` in `resumeRun`'s own `finally` (T:17866),
   * which runs on every road: a boot `?run=`, an adoption by the standing
   * watch, a scheduled message's attach. Native focused only from `autoFocus`
   * at mount, so boot-with-`run` was covered incidentally and a mid-session
   * adoption was not.
   *
   * A dep rather than a `ChatState` field because it is an ACTION with no
   * lasting truth behind it: a nonce would have to be spent, and a spent nonce
   * is a second piece of state for a gesture that is over.
   */
  focusComposer?: () => void;
  /**
   * ADDED: the `<live-app-state>` block for THIS send, or `""` when there is
   * nothing to say (no pane, or a pane that has told us nothing).
   *
   * Read at SEND time, once per outgoing message, exactly where T reads it
   * (`appStatePush()` at the top of `sendMessage`, T:16483) — the state the
   * user is describing is the state at the moment they typed, not whatever the
   * pane has drifted to by the time the run starts. Async because the DOM
   * outline is offloaded to a file first when it is large enough
   * (`AppStateWatcher.blockForSend`, T:5177-5218).
   *
   * The PULL channel (`answerAppState`) is the other half and is unrelated:
   * this is what goes out unasked with every message.
   */
  appStateBlock?: () => Promise<string>;
  /**
   * ADDED (PR2): a send whose BUBBLE WAS DROPPED — the run never launched, or a
   * follow-up never reached a live one — so the agent saw none of it and the
   * composer takes its attachments back (T:16091-16093, 16693-16720).
   *
   * The pictures are NOT revoked on this road and must not be: they are handed
   * back as pending chips showing those very thumbnails. The user attached them
   * deliberately — a capture of a moment that has passed, or a file they went
   * and found — and a failed send is not a reason to make them do it again, nor
   * could they, if what they photographed is gone.
   *
   * A DELIVERED follow-up stranded by a stop is not this: `onStranded` hands
   * those words back, and their pictures are already in the agent's hands.
   */
  onSendReturned?: (info: {
    text: string;
    attachments?: Receipt[];
    /** `SendOptions.sendId`, echoed: WHICH send came back (see its own note). */
    sendId?: string;
    /**
     * The send was turned away BEFORE any bubble went up (disposed, the
     * `sending` gate, nothing to send) — so `text` is not in the transcript,
     * not in the follow-up queue, and not in the composer's box, which cleared
     * on the keystroke. The caller owes those words back to the box (Bugbot,
     * PR #1074). Absent for a send that reached a bubble and then failed: that
     * one left the failure in the chat, where the reader is.
     */
    refused?: boolean;
  }) => void;
}

export type { HistoryTurn };
