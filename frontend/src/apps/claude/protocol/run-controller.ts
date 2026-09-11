// THE RUN LOOP, ported from `T`'s `sendMessage` / `pollLoop` / `sendFollowUp` /
// `stopRun` / `syncPermissions` / `answerAppState` / `loadHistory`. Pure TS: no
// React, no DOM. The UI reads `ChatState` and calls the methods on
// `ChatController` (protocol/controller-api.ts).
//
// The five rules that make this correct, each of which had its own bug first:
//
// 1. NO CURSOR. Every poll returns the WHOLE turn — segments, text,
//    permissions, skills and app_state all replayed — and the client dedupes by
//    id / renders idempotently (T:16250-16261, segments.ts). The cadence is a
//    flat `sleep(400)` with no backoff (T:16377).
//
// 2. D687 SLICING. The server cursor deliberately refuses to advance past a
//    mid-turn echo, so after a follow-up lands the replay still carries
//    everything already streamed ABOVE it. The loop freezes the PREVIOUS
//    iteration's lengths into `segBase`/`textBase` and slices back to them, so
//    the new bubble shows only the continuation (T:16269-16283).
//
// 3. SEATS. `logGen` (the visible transcript's generation, sampled by the
//    CALLER before its awaits), `sendSeq`, `loopSeq`, `activeSeat`,
//    `stoppedSeat` and `followupSeq`. Only the NEWEST loop may clear the `run`
//    param, drop the Stop chrome or post "Stopped." — equality on a run id is
//    not proof of ownership, because a reopened chat re-attaches to the SAME
//    run id (Bugbot, PR #653: T:16311-16318, 16396-16410).
//
// 4. A THROWN POLL ABORTS THE BUBBLE AND KEEPS `?run=`. The partial reply goes
//    so a failed run never leaves half a sentence behind; the param stays so the
//    next boot re-attaches to the subprocess, which is still working
//    (T:16378-16386).
//
// 5. CARDS LAND BELOW THE PROSE THEY INTERRUPT. `syncPermissions` runs AFTER
//    the segment render, every poll, and re-pins the open cards last
//    (T:16305-16311, 14665-14775).
import { scheduleMessage } from "@platform/lib/api";
import { announceTasksChanged } from "@platform/lib/tasksChanged";

import { runAgent } from "./agent";
import type {
  AdoptOptions,
  AssistantTurn,
  ChatController,
  ChatState,
  ControllerDeps,
  ErrorTurn,
  NoteTurn,
  ResumeOptions,
  RunStatus,
  SendOptions,
  Trouble,
  Turn,
  UserTurn,
  Working,
} from "./controller-api";
import { historyToTurns } from "./history";
import { CONTINUE_PROMPT, CONTINUE_TITLE, continueDue, continueNote, limitHit } from "./quota";
import { pollBody, type SegmentView } from "./segments";
import { isUnknownRun, troubleFromError, troubleFromMessage, troubleOf } from "./trouble";
import type {
  Activity,
  AppStateResponse,
  AppStateRow,
  CancelResponse,
  Decision,
  DecideResponse,
  DecisionScope,
  LandedDecision,
  HistoryResponse,
  PermissionMode,
  PermissionRow,
  Phase,
  PollResponse,
  Quota,
  RetryInfo,
  RunIdResponse,
  SendResponse,
  SkillRow,
  StartResponse,
  SwitchableMode,
} from "./types";
import { composeBlocks, composeOutgoing, isMarkerOnly, stripBlocks } from "./wire";
/** PR2: the attachment pipeline's receipt row — carried, never built here. */
import type { Receipt } from "../shots/types";


// ---- constants (all with their T line) -------------------------------------

/** T:16377 — the poll cadence. No backoff, ever. */
export const POLL_MS = 400;
/** T:11911 — `params.permission || DEFAULT_PERMISSION`. */
export const DEFAULT_PERMISSION: PermissionMode = "prompt";
/** T:16130 — a follow-up waits this long for `sendMessage`'s own `start` to
 *  publish a run id, rather than dropping the message. */
export const FOLLOWUP_WAIT_TRIES = 15;
export const FOLLOWUP_WAIT_MS = 200;
/** T:15733 — polls a request may see an empty snapshot before the page answers
 *  with the explicit "no app" sentence instead (~2 s at 400 ms). */
export const APP_STATE_NULL_POLLS = 5;
/** T:15750 — cap on the three app-state memos and the skill memo. */
export const APP_STATE_MEMO_MAX = 200;
/**
 * The permission-card map's OWN ceiling, and much higher on purpose: unlike the
 * four memos above, `permCards` is RENDERED state. The memos remember "we have
 * already answered this" — dropping the oldest entry costs one duplicate
 * notice — whereas dropping a perm card DELETES A RECEIPT OUT OF THE
 * TRANSCRIPT: the parked "✓ Allowed" / "✗ Denied" row the reader can scroll
 * back to, which `T` keeps in the DOM for the life of the log
 * (T:14728-14742). So the cap here exists only to stop a pathological session
 * growing without bound, and it is set where no real conversation reaches it
 * (a run asks single-digit permissions; 2000 is thousands of turns).
 */
export const PERM_CARD_MAX = 2000;
/** T:16229 — one artifacts read in every eight polls (~3.2 s). PR4 owns the
 *  read itself; the tick is counted here so the callback fires on T's clock. */
export const ARTIFACTS_EVERY_TICKS = 8;
/** T:17781 — `retryUnknown`'s budget for a run dir that is not visible yet. */
export const UNKNOWN_RUN_RETRIES = 5;
export const UNKNOWN_RUN_RETRY_MS = 700;
/** T:17509 — `adoptLiveRun`'s laps. ~3 s of looking at one poll interval each:
 *  one tick is the common case (the reopen itself), the tail covers the window
 *  in which a run started elsewhere has not become visible yet. */
export const ADOPT_LAPS = 8;

// ---- runEnding (pure, T:15871-15900) --------------------------------------

export interface RunEnd {
  error: string;
  note: string;
  keepText: boolean;
}

/**
 * How a finished run ends on screen. Killing claude leaves the run dead with no
 * `result` row, which `_poll` reports as an error BY DESIGN — a truncated reply
 * must never read as a clean success. But when the user asked for the stop, that
 * error IS the stop, and repeating it back reads as a crash they caused. It is
 * swallowed here, and only here.
 *
 * `data.cancelled` is the RUN's own record of having been cancelled, whoever
 * called it — so a run killed from the tasks queue card ends the same way as one
 * stopped from this page (Akshil, 2026-08-21).
 */
export function runEnding(
  data: { error?: string; cancelled?: boolean } | null | undefined,
  stopped: boolean,
): RunEnd {
  const wasStopped = stopped || !!(data && data.cancelled);
  const error = (data && data.error) || "";
  if (!wasStopped) return { error, note: "", keepText: !error };
  // The stop landed. Whatever streamed before the kill is real work and stays.
  if (error) return { error: "", note: "Stopped.", keepText: true };
  // The stop did NOT land: the reply completed between the click and the signal.
  return { error: "", note: "The turn finished before the stop landed.", keepText: true };
}

/** T:15870 `stopAllowed` — whether a Stop press should go through, given the run
 *  and turn currently live and the turn a stop was last asked for. */
export function stopAllowed(runId: string | null, seat: number, lastStoppedSeat: number): boolean {
  return !!runId && !!seat && lastStoppedSeat !== seat;
}

// ---- helpers ---------------------------------------------------------------

function trim<T>(memo: Set<T> | Map<T, unknown>): void {
  while (memo.size > APP_STATE_MEMO_MAX) {
    const first = memo.keys().next().value as T;
    memo.delete(first);
  }
}

/**
 * The same job for `permCards`, with the two differences the rendered state
 * forces (see `PERM_CARD_MAX`):
 *  * a far higher ceiling, and
 *  * SETTLED CARDS FIRST. An open card is a blocked subprocess, and its row is
 *    the only control that can unblock it — evicting one by insertion order
 *    would strand the run waiting for a click the reader can no longer make.
 *    A settled card is a receipt: losing the oldest one loses history, which is
 *    the lesser of the two. If every card is open, none goes.
 */
export function trimPermCards(cards: Map<string, { decision?: string | null }>): void {
  if (cards.size <= PERM_CARD_MAX) return;
  for (const [id, row] of cards) {
    if (cards.size <= PERM_CARD_MAX) break;
    if (row.decision) cards.delete(id);
  }
}

const nativeSleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

function emptyState(file: string | null): ChatState {
  return {
    file,
    sessionId: null,
    runId: null,
    status: "idle",
    turns: [],
    permissions: [],
    appState: [],
    skills: [],
    working: null,
    trouble: null,
    permissionMode: DEFAULT_PERMISSION,
    queued: [],
    historyLoading: false,
    adopting: false,
    transcript: null,
    ownRunEndedAt: 0,
    repaired: 0,
    transcriptGen: 0,
    rev: 0,
  };
}

// ---- the controller --------------------------------------------------------

export function createChatController(deps: ControllerDeps): ChatController {
  const sleep = deps.sleep || nativeSleep;
  const now = deps.now || Date.now;
  const dir = deps.agentDir;
  const FILE = deps.file;
  // Every `agent.py` call this controller makes carries the chat's own target
  // as `X-Fused-Target`, so `fused-render calls` can be filtered by the file a
  // conversation is about (SPEC CL-5; `protocol/agent.ts` derives the PAGE half
  // from the script's own dir). A wrapper rather than a change at each of the
  // ~17 call sites, and it leaves `deps.run` — the tests' seam — untouched.
  const run =
    deps.run ||
    ((d, action, fields, opts = {}) =>
      runAgent(d, action, fields, { ...opts, ...(FILE ? { target: FILE } : {}) })) as typeof runAgent;

  let state = emptyState(FILE);
  const listeners = new Set<() => void>();

  const emit = (patch: Partial<ChatState>) => {
    state = { ...state, ...patch, rev: state.rev + 1 };
    for (const cb of listeners) cb();
  };

  // ---- ownership seats (T:16187-16201) ------------------------------------
  let sending = false;
  let sendSeq = 0;
  let loopSeq = 0;
  let followupSeq = 0;
  let logGen = 0;
  let activeRun: string | null = null;
  let activeSeat = 0;
  let stoppedSeat = 0;
  let disposed = false;
  /**
   * THE WINDOW THAT IS ALREADY ON SCREEN, so a follow-up never re-types the
   * reply before it (owner feedback R4-3).
   *
   * `_read_current_turn`'s cursor only ever advances past a turn boundary it
   * has PROVEN — a `_starts_new_turn` echo with a `result` before it — and it
   * never trims what the poll it advances on hands back (agent.py says so
   * itself). So the first non-blank payload after an IDLE-time send is the
   * previous reply in full with the new one growing behind it, and
   * `turn_breaks` is empty for it: a genuine boundary is not an absorbed
   * fold-in, so `_absorbed_turn_breaks` reports no seam to slice at.
   *
   * A send into a live host starts a FRESH `pollLoop` (`sendMessage`'s
   * live-host road), and a fresh loop has no memory of that reply having
   * landed — so it opened a new bubble, typed the previous answer into it
   * again, and then replaced it with the new answer when the cursor finally
   * moved: "sometimes when I reply, it re-streams the previous message's
   * response, and then shows the new response".
   *
   * Recorded by the loop that SETTLED that reply — the only reader that knows
   * the payload it settled is what the transcript now holds — and by
   * `resumeAttach`'s repair of a run that finished with no frame attached, for
   * the same reason. The next loop for the SAME run drops that prefix until
   * the window no longer OPENS on that text, which is the cursor having
   * stepped over the boundary; from that poll on the payload is only the new
   * turn. The landed PROSE is what the base is anchored to rather than a pair
   * of lengths: the newer reply is not reliably shorter than the pair it
   * replaced (it commonly is not), so no length test can see the step.
   */
  let landedWindow: { runId: string; segments: number; text: string } | null = null;
  /** A live-run adoption watch is in flight — see `adoptLiveRun`. Separate
   *  from `state.adopting`, which is the RENDERER's gate: this one is the
   *  one-watch-at-a-time latch and stays set for as long as the adopted run
   *  does, while the published flag clears at its first poll. */
  let adopting = false;
  /**
   * EVERY RUN ID THIS CONTROLLER HAS TAKEN RESPONSIBILITY FOR — a send of its
   * own, a `run` param it re-attached to, a turn the standing watch adopted.
   * It is the SCHEDULE_ATTACHED question (T:16746-16765) asked of the party
   * that actually renders turns, rather than of a poller's private bookkeeping.
   *
   * It exists because PR4's two watchers read different clocks: the standing
   * watch looks every 5 s, the schedule poll every 15. A fired scheduled run is
   * therefore usually ADOPTED FIRST, and `busy()` then holds the poller at its
   * live-turn guard with the entry left deliberately unmarked — so once the
   * turn ended, the next tick `resumeRun`'d that same id with `neverShown` and
   * appended the very turn the watch had just streamed a SECOND TIME (Bugbot
   * PR #1075).
   *
   * WRITTEN ON ATTACHMENT, NEVER ON INTENT (batch review F1). The mark used to
   * go down in `adoptWatch` before `resumeRun` and again at the top of
   * `resumeAttach` before the probe — which made this loop's own stated
   * recovery unreachable: `resumeRun` bailing on the send gate is "a lap of
   * this loop away from trying again", but the next lap hit `shownRuns.has(id)`
   * and skipped the id for the life of the page. A live run passed over because
   * a send happened to be in flight, or whose first probe threw, was then never
   * adopted by the run-dir road at all — only the coarse transcript follower
   * recovered it, with no streaming chrome and whole-turn granularity, and
   * `setRunParam(id)` left a stale `?run=` on the URL. So the two early writes
   * are gone and the mark lands where the run is genuinely ours: past
   * `resumeAttach`'s `unknown run_id` check (every road on from there either
   * streams through `pollLoop` or repairs the turn from the probe payload) and
   * in `pollLoop` itself.
   *
   * CLEARED WHEN THE VISIBLE CONVERSATION IS REPLACED (Bugbot 3974975055).
   * "Never cleared" was wrong for the one gesture PR4 is built around: Back
   * mid-turn leaves the run going server-side and the session list is supposed
   * to re-attach to it — but the id was already in here from `pollLoop`, so
   * `adoptWatch` refused it and the reader got the external "Running outside
   * this app" line with no stop control and no token count. The set answers "is
   * this run's turn already ON SCREEN", and a transcript rebuilt from `history`
   * (or emptied) is precisely the event that makes the answer no. Both roads
   * that replace it — `newChat` and `openSession` — clear it, and the schedule
   * poller's baseline is re-armed by the same `transcriptGen` bump.
   */
  const shownRuns = new Set<string>();
  /**
   * AND THE SHORT-LIVED HALF: ids a `resumeAttach` is in the middle of claiming.
   *
   * The early `shownRuns` write was doing two jobs and only one of them was
   * wrong. The other is real: while this frame is probing an id, nothing else
   * may attach to the same run behind it (Bugbot PR #1075 — the schedule poller
   * asks `hasShownRun` and would otherwise append the turn a second time). A
   * claim is held for the length of the attach and RELEASED when it ends
   * without having attached, so the recovery lap finds the id free again.
   *
   * THE VALUE IS THE ATTACH'S SEAT, and both halves of that matter (Bugbot
   * 3975433059). `newChat`/`openSession` clear the claims along with
   * `shownRuns`: an attach whose transcript has been replaced is abandoned — it
   * will bail on its own generation check — and leaving its id claimed meant
   * Back during an adopted, scheduled or `?run=` attach kept that run
   * unadoptable until the probe returned, or for the rest of the page if the
   * poll hung. But a cleared claim can be RE-TAKEN by the watch's next lap
   * before the abandoned attach's `finally` runs, so the release is conditional
   * on still holding the seat it took — otherwise the old attach's exit would
   * quietly let go of the new one's claim.
   */
  const claimingRuns = new Map<string, number>();
  /** Publish the renderer's gate, and only on a real change: it is read in a
   *  layout effect, so a no-op emit is a wasted frame on every poll. */
  const setAdopting = (value: boolean) => {
    if (state.adopting !== value) emit({ adopting: value });
  };
  /** Aborted by `dispose`, so the in-flight poll and its `sleep` do not outlive
   *  the unmount by a lap (agent.ts takes the signal). */
  let life: AbortController | null = typeof AbortController === "function" ? new AbortController() : null;

  // ---- per-transcript memos (T:15721-15756) -------------------------------
  const answeredStates = new Set<string>();
  const notedStates = new Set<string>();
  const nullStatePolls = new Map<string, number>();
  const notedSkills = new Set<string>();
  /** id → row, in ARRIVAL order (Map preserves insertion) — T:13740 permCards.
   *  Capped like every other per-transcript memo: `publishPermissions` re-sorts
   *  and re-emits the whole list on each change, so an uncapped map is a list
   *  that grows for the life of the session. */
  const permCards = new Map<string, PermissionRow>();
  /** The live turn's key, or null between turns — T:15852 activeSegBody. The one
   *  thing outside the loop that has to put something INTO the streaming turn. */
  let activeTurnKey: string | null = null;
  /**
   * T:16024 — follow-ups this page handed to the live run that the MODEL has
   * not answered yet.
   *
   * The lifetime is the TURN, not the `send` round-trip, and that is the whole
   * correction here (QA round 3a, defects 2 and 5). An entry used to be dropped
   * the instant `send` came back `{sent: true}` — which only means the inbox
   * took the bytes, ~200 ms after Enter. So the "N follow-ups are queued" hint
   * existed, was styled, and was tested, and no user ever saw it: it lived for
   * one HTTP round-trip and then left, while the message it described sat in the
   * CLI's queue for the rest of the run.
   *
   * An entry now leaves only for a reason:
   *   - the send FAILED (`giveBack`) — the agent never saw it, so the bubble
   *     goes too;
   *   - the run ENDED (`clearQueued`, pollLoop's finally) — the CLI drains its
   *     queue as part of the run, so nothing is queued once the run is over;
   *   - a STOP stranded it (`stopRun`) — it goes back to the composer.
   *
   * `wire` is the outgoing form (what `still_queued` would name); `typed` is
   * what the user actually put in the box, which is what a handback owes them
   * back. `bubble` is the optimistic turn's key, so a strand can un-post it.
   *
   * `landed` is WHETHER THE INBOX CONFIRMED THE SEND — set the moment `send`
   * answers `{sent: true}`, which is also the moment `followupSeq` is bumped.
   * It is what `stopRun` needs to tell "the CLI never got this" from "the CLI
   * got it and, on the measured behaviour of the held-open stdin, echoed it
   * straight away" (feedback #11 — see `stopRun`).
   *
   * `opts` IS THE SEND'S OWN OPTIONS, kept for exactly one reason: the pictures.
   * `ClaudeChat.beginSend` parked them under `opts.attachments` before the send
   * began, and a strand is a send that did not go — so whoever strands the entry
   * owes them back through `returnSend`, not just the words through `onStranded`
   * (Bugbot, PR #1064). `handedBack` keeps that from happening twice when the
   * still-in-flight POST later settles into `giveBack`.
   */
  const queued: {
    seq: number;
    wire: string;
    typed: string;
    bubble: string;
    landed: boolean;
    opts: SendOptions;
    handedBack: boolean;
  }[] = [];
  let queuedSeq = 0;
  const publishQueued = () => emit({ queued: queued.map((q) => q.typed || q.wire) });
  /** Every entry, gone, published once. The run is over: the CLI has either
   *  answered these or died with them, and neither is "queued". */
  const clearQueued = () => {
    if (!queued.length) return;
    queued.length = 0;
    publishQueued();
  };

  // ---- params (the exact moments T writes them) ---------------------------
  //
  // THE DEFAULT IS RUNTIME.JS'S DEFAULT, which is a BARE `set`: first change on
  // a pristine entry pushes, everything after it replaces, and a write made
  // before any gesture in the document takes the replace path anyway
  // (R:1233-1254, and the store's own `sawGesture`). Making "replace" the
  // default here inverted that contract — Back stopped returning to the landing
  // from a chat the reader had just opened, because the entry it opened on
  // stayed pristine for the whole session. Only two writes are genuinely
  // "replace": `run`, which is in-flight bookkeeping (T:13036), and the plan
  // card's landing mode, which is a CONSEQUENCE of an approval sitting behind an
  // `await` (T:14576-14580).
  const setParam = (
    patch: Record<string, string | null>,
    history: "push" | "replace" = "push",
  ) => deps.params.set(patch, { history });

  /** T:16245 — a session id arrived on the poll. */
  const noteSessionId = (id: string) => {
    if (!id || state.sessionId === id) return;
    setParam({ session_id: id });
    emit({ sessionId: id });
  };

  /** T:16333 / T:17793 / T:13036 — `run` is in-flight bookkeeping and is
   *  cleared with `{history:"replace", default:""}`: absent is the spelling for
   *  "none", and clearing a real one must never buy the visit's history entry. */
  const clearRunParam = () => setParam({ run: null }, "replace");
  /** T:16678 / T:16151 / T:17437 — the same write, for the same reason. */
  const setRunParam = (runId: string) => setParam({ run: runId }, "replace");

  /** T:16435-16441 — tell every OTHER document on this origin that a turn just
   *  started or ended here. The shell's tasks store listens for it, and so does
   *  another copy of this page's live watch (D415). Best-effort. */
  const noteChatActivity = () => {
    try {
      deps.onActivity?.();
      announceTasksChanged();
    } catch {
      // The 20-30 s polls remain the fallback.
    }
  };

  const setRunningUi = (on: boolean, status: RunStatus = on ? "running" : "idle") =>
    emit({ status, runId: on ? activeRun : null });

  // ---- turns --------------------------------------------------------------

  let turnSeq = 0;
  const nextKey = (prefix: string) => `${prefix}:${++turnSeq}`;
  /** T:15196 `cardSeq` — numbers segment CONTAINERS for the life of the
   *  controller, so two turns' second thinking block cannot share one collapse
   *  override (segments.ts `cardKey`). */
  let cardSeq = 0;

  const pushTurn = (turn: Turn) => emit({ turns: [...state.turns, turn] });

  const replaceTurn = (key: string, patch: Partial<AssistantTurn>) => {
    const turns = state.turns.map((t) =>
      t.key === key && t.role === "assistant" ? { ...t, ...patch } : t,
    );
    emit({ turns });
  };

  const dropTurn = (key: string) => emit({ turns: state.turns.filter((t) => t.key !== key) });

  /**
   * PR2 — THE SENT BUBBLE LETS GO OF ITS BLOB URLS.
   *
   * `ClaudeChat.beginSend` parks a send's attachments under the very
   * `Receipt[]` it hands down here, and on the road that LANDED it re-points
   * those receipts at the copy the server now holds (`settleReceipts`) so the
   * object URLs can be released. The rows are memoized on identity, so the
   * replacement has to come through the store or the `<img>` would keep the URL
   * that is about to be revoked (Bugbot, PR #1064).
   *
   * THE ARRAY IS THE ADDRESS, exactly as it is for the hand-back: a send whose
   * bubble was dropped (a rollback, a `newChat`) finds no turn and settles
   * nothing, rather than rewriting another send's receipts.
   */
  const settleAttachments = (receipts: Receipt[], next: Receipt[]): void => {
    if (disposed) return;
    let found = false;
    const turns = state.turns.map((t) => {
      if (t.role !== "user" || t.attachments !== receipts) return t;
      found = true;
      return { ...t, attachments: next };
    });
    if (found) emit({ turns });
  };

  /** T:13446 `addUser` — the bubble goes up BEFORE anything slow on the send
   *  path: the user's words appearing instantly is worth more than a receipt and
   *  a bubble arriving together (T:16490). */
  const addUser = (
    text: string,
    raw?: string,
    attachments?: Receipt[],
    appState = false,
    /** PR3 `SendOptions.optimisticKey`: fill THAT row instead of adding one. */
    adopt?: string,
  ): UserTurn => {
    // ADOPTION IS A REPLACE IN PLACE, and it has to be: the optimistic row is
    // already the last bubble in the log, and pushing a second one would leave
    // the reader looking at their message twice while the first send is still
    // out. A key that is no longer in the log (a Back mid-capture) is not
    // adopted — the row it named is gone, so this send posts its own.
    const held = !!adopt && state.turns.some((t) => t.key === adopt && t.role === "user");
    const turn: UserTurn = {
      role: "user",
      key: held ? (adopt as string) : nextKey("u"),
      text,
      ...(raw ? { raw } : {}),
      // The receipt rides the bubble the send posted, so the row is under the
      // words from the first paint rather than appended after the start
      // round-trip (T:16560-16583 appends it to the last `.turn.user`).
      ...(attachments && attachments.length ? { attachments } : {}),
      // The push channel's own receipt legacy hangs under the bubble
      // (T:16588-16596). Set from the block actually composed into `raw`, never
      // from "is there a pane" — a pane that has told us nothing produces no
      // block and owes no receipt.
      ...(appState ? { appState: true as const } : {}),
    };
    if (held) emit({ turns: state.turns.map((t) => (t.key === turn.key ? turn : t)) });
    else pushTurn(turn);
    return turn;
  };

  /**
   * PR3 — THE OPTIMISTIC BUBBLE, and the pair that owns it.
   *
   * A caller whose send path is async before it can call `sendMessage` (the
   * annotation round photographs the pane first) posts the typed words here the
   * moment the composer clears its box, hands the key down as
   * `SendOptions.optimisticKey`, and the send's own bubble adopts this very row
   * — one bubble, however long the capture took. A send that never reached the
   * controller drops it (`dropOptimisticUser`, and `returnSend` for the roads
   * that refuse inside).
   */
  const postOptimisticUser = (text: string): string => {
    if (disposed || !text) return "";
    return addUser(text).key;
  };

  const dropOptimisticUser = (key: string): void => {
    if (!key || !state.turns.some((t) => t.key === key)) return;
    dropTurn(key);
  };

  /** T:13722 `addNote` — the ◍ / ◆ / ⏹ rows. */
  const addNote = (text: string, glyph: NoteTurn["glyph"]): NoteTurn => {
    const turn: NoteTurn = { role: "note", key: nextKey("n"), text, glyph };
    pushTurn(turn);
    return turn;
  };

  /** T:13884-13886 — the mode the RUN is in. Written at start, by every poll
   *  that reports one, and by the two decisions that move a live run's mode. */
  const setPermissionMode = (mode: PermissionMode | "" | undefined) => {
    if (!mode || state.permissionMode === mode) return;
    emit({ permissionMode: mode });
  };

  /** Clearing the slot — the one write that is not a failure. */
  const clearTrouble = () => emit({ trouble: null });

  /**
   * A failure, in BOTH halves.
   *
   * The `trouble` slot is the contract's, and it is the right home for the card
   * at the tail: it is the actionable surface, and only the newest failure is
   * worth acting on. But T APPENDS a row per failure and leaves it where it
   * happened, so a turn that failed, was retried and failed differently reads as
   * two problems in the log rather than one that changed its mind. So the row
   * goes in too, in chronological position — and the UI suppresses the row for
   * whichever failure the card is currently showing, so nothing is said twice
   * (`ui/Transcript.tsx` `lastErrorKey`).
   *
   * `emit` once, not twice: two patches would paint an intermediate transcript.
   */
  const reportTrouble = (trouble: Trouble) => {
    const row: ErrorTurn = {
      role: "error",
      key: nextKey("e"),
      text: trouble.message,
      kind: trouble.kind,
      ...(trouble.quota ? { quota: trouble.quota } : {}),
    };
    emit({ trouble, turns: [...state.turns, row] });
  };

  /** T:13698 `addError(message)` — classify, then report. */
  const addError = (message: string, fromAgent = false) =>
    reportTrouble(troubleFromMessage(message, fromAgent));

  /**
   * THE COMEBACK AFTER A USAGE LIMIT. Claude Code's own TUI waits in the open
   * session and continues the task when the plan window reopens; a headless
   * `-p` run gets no such wait — the turn ends on the limit and the process is
   * reaped. So the chat does the same thing with the infrastructure it already
   * has: one scheduled message on THIS session, due at the reset the CLI
   * reported (+ a minute), carrying the CLI's own fixed continuation prompt.
   * The schedule banner (`SchedBlock`) then shows the row with its time and
   * its cancel, exactly like any other pending message, and the fired run
   * resumes the conversation in place.
   *
   * Reported as trouble FIRST, with the window on it, so the card says when
   * the reset is even if the POST fails; the note and the card's "scheduled"
   * line land only once the server has the row. A refused POST is a NOTE
   * beside that card, never a second card: `reportTrouble` replaces the slot,
   * and the reset time is the one fact the reader must not lose (Bugbot
   * #1107). Once per run, and the guard comes BEFORE the row: `poll.done`
   * repeats on a re-attached or superseded loop, and the second pass must
   * print nothing and post nothing (Bugbot #1107).
   *
   * Returns whether it took the failure — false means the caller reports the
   * error the ordinary way (no session to schedule on, or no target).
   */
  const scheduledRuns = new Set<string>();
  const scheduleComeback = (runId: string, error: string, quota: Quota): boolean => {
    if (scheduledRuns.has(runId)) return true;
    const sessionId = state.sessionId;
    if (!FILE || !sessionId) return false;
    scheduledRuns.add(runId);
    const trouble: Trouble = { ...troubleFromMessage(error), quota };
    reportTrouble(trouble);
    const post = deps.schedule ?? scheduleMessage;
    void post({
      target: FILE,
      message: CONTINUE_PROMPT,
      due: continueDue(quota),
      session_id: sessionId,
      title: CONTINUE_TITLE,
    })
      .then(() => {
        // The card's line about the scheduled follow-up, and the CLI's own
        // wait sentence as a note in the log where the failure sits.
        if (state.trouble === trouble) emit({ trouble: { ...trouble, scheduled: true } });
        addNote(continueNote(quota), "\u25f7");
        announceTasksChanged();
      })
      .catch((err: unknown) => {
        addNote(
          "Could not schedule the follow-up: " + (err instanceof Error ? err.message : String(err)),
          "\u25f7",
        );
      });
    return true;
  };

  // ---- skills / app_state (T:15771-15837) --------------------------------

  /** T:15771 `noteSkills` — poll replays every call in the file on each tick,
   *  so the id memo is what keeps one skill to one row. */
  const noteSkills = (list: SkillRow[] | null | undefined) => {
    const fresh: SkillRow[] = [];
    const rows: NoteTurn[] = [];
    for (const call of list || []) {
      if (!call || !call.id || !call.skill || notedSkills.has(call.id)) continue;
      notedSkills.add(call.id);
      trim(notedSkills);
      fresh.push(call);
      // Skill names are model-authored text; the UI renders them as text.
      rows.push({ role: "note", key: nextKey("n"), text: "skill · " + call.skill, glyph: "◆" });
    }
    // ONE emit for the poll, not one per skill: T appends N rows in a single
    // pass (T:15771), where an `addNote` each cloned the whole state and swept
    // every listener N times for one tick.
    if (fresh.length) {
      emit({ turns: [...state.turns, ...rows], skills: [...state.skills, ...fresh] });
    }
  };

  /** T:15804 `answerAppState`'s bookkeeping half. The rows the PANE still has to
   *  answer, with how many polls each has been seen for: a request whose count
   *  has passed APP_STATE_NULL_POLLS is one the pane should answer with the
   *  explicit "no app" sentence rather than keep waiting (T:15726-15734). */
  const surfaceAppState = (list: AppStateRow[] | null | undefined, runId: string) => {
    const rows: AppStateRow[] = [];
    for (const req of list || []) {
      if (!req || !req.id || answeredStates.has(req.id)) continue;
      const seen = (nullStatePolls.get(req.id) || 0) + 1;
      nullStatePolls.set(req.id, seen);
      trim(nullStatePolls);
      // `runId` is the run that ASKED (T:16260) — see `answerAppState`.
      rows.push({ ...req, runId, pollsSeen: seen, waitedOut: seen > APP_STATE_NULL_POLLS });
    }
    // Replayed every poll, so this is a straight replace rather than a merge.
    //
    // AND RE-EMITTED EVEN WHEN THE ROWS LOOK IDENTICAL, deliberately: review
    // asked for a shallow-equality skip here, and it would hang a blocked tool
    // call. `pollsSeen` advances on every tick and `useAppStateResponder` keys
    // its answer loop on it (useAppStateResponder.ts:95-112) precisely because
    // T calls `answerAppState` from the poll itself and so retries every tick
    // (T:15758-15869); without a fresh row per poll the count never passes
    // APP_STATE_NULL_POLLS and the explicit "no app" answer is unreachable
    // (T:15726-15734). The rows ARE new information every 400 ms.
    if (rows.length || state.appState.length) emit({ appState: rows });
  };

  // ---- permissions (T:14750-14775) ---------------------------------------

  /**
   * Reconcile the poll's request list with what is on screen. Idempotent — poll
   * replays the whole list every 400 ms.
   *
   * Placement is the whole of it, and it is why this runs AFTER the segment
   * render: an OPEN card is the thing the run is blocked on, so the open set is
   * the LAST thing before the status line, whatever else got mounted after them
   * (`pinOpenCards`, T:14680). A RESOLVED card belongs where it was answered, so
   * it is parked once into the live turn at its current tail (`parkResolvedCard`,
   * T:14728) — and never dragged to the tail again.
   */
  const syncPermissions = (
    list: PermissionRow[] | null | undefined,
    runId: string,
    liveMode: PermissionMode | undefined,
    publish = true,
  ): boolean => {
    // `poll.mode` is agent.py's `_live_mode` — the authoritative read of the mode
    // the run is in, so it outranks whatever the start seeded (T:16325).
    setPermissionMode(liveMode);
    let changed = false;
    for (const p of list || []) {
      if (!p || !p.id) continue;
      const prev = permCards.get(p.id);
      // A CARD IS NEVER UN-RESOLVED. `resolveLocally` writes the verdict the
      // decide POST came back with, and the poll that started before it landed
      // still reports the request as open — T's `resolve` is called only when
      // `p.decision` is set and is idempotent, so a stale replay cannot take a
      // verdict back off a card (T:14766-14772).
      if (prev && prev.decision && !p.decision) continue;
      const next: PermissionRow = {
        ...p,
        // The CLI's own id for the asking call, in this file's camelCase — what
        // files a resolved card back against the exact tool chip it answered
        // instead of the newest chip that happens to share its tool name
        // (`Transcript.parkPlan`, feedback #18). Stamped once; agent.py never
        // rewrites the tool_use_id of a request already on disk.
        ...(p.tool_use_id || prev?.toolUseId
          ? { toolUseId: p.tool_use_id || (prev?.toolUseId as string) }
          : {}),
        // The run the card BELONGS to, stamped from the loop that delivered it
        // (T:16325 hands `run_id` to `buildPermCard`) and never re-stamped: a
        // respawn's new run did not ask this question.
        runId: prev?.runId ?? runId,
        liveMode: liveMode || prev?.liveMode,
        placement: p.decision ? "parked" : "open",
        // Filed once, from whichever of the click or the poll settles it first.
        parkedIn: p.decision ? (prev?.parkedIn ?? activeTurnKey) : null,
        // A failed decide's reason survives the replay: the card is still open,
        // the subprocess is still blocked, and the reader has to be able to read
        // why their click did not land (T:14113). Gone once it resolves.
        ...(p.decision ? {} : prev?.sendError ? { sendError: prev.sendError } : {}),
      };
      if (!prev) {
        permCards.set(p.id, next);
        trimPermCards(permCards);
        changed = true;
      } else if (!sameCard(prev, next)) {
        permCards.set(p.id, next);
        changed = true;
      }
      // else: KEEP `prev`. Nothing the card draws has moved, and replacing the
      // row would re-emit the whole list for a replay of the same request.
    }
    if (changed && publish) publishPermissions();
    return changed;
  };

  /** Whether two readings of one request say the same thing TO THE CARD. `input`
   *  is deliberately not compared: it is uncapped — a Write's whole file body, a
   *  Bash heredoc — and stringifying every row on every 400 ms poll to answer a
   *  question none of these fields depend on is exactly what inventory §M bans
   *  from the chip update key (T:15545-15554). agent.py never rewrites the input
   *  of a request already on disk. */
  const sameCard = (a: PermissionRow, b: PermissionRow): boolean =>
    a.decision === b.decision &&
    a.scope === b.scope &&
    a.mode === b.mode &&
    a.liveMode === b.liveMode &&
    a.placement === b.placement &&
    a.parkedIn === b.parkedIn &&
    a.sendError === b.sendError &&
    a.runId === b.runId &&
    JSON.stringify(a.answers ?? {}) === JSON.stringify(b.answers ?? {});

  const permissionRows = (): PermissionRow[] => {
    const rows: PermissionRow[] = [];
    for (const row of permCards.values()) rows.push(row);
    // Open cards pinned LAST, in request order, as one contiguous block: the run
    // is blocked on them (T:14680 pinOpenCards).
    rows.sort((a, b) => Number(a.placement === "open") - Number(b.placement === "open"));
    return rows;
  };
  const publishPermissions = () => {
    const rows = permissionRows();
    emit({ permissions: rows });
    rememberCards(rows);
  };
  /** THE CACHE FOLLOWS THE CARDS. A Peek opened on a tile that has been
   *  polling for a minute must open on the card the tile shows NOW, not the one
   *  the tile's history fetch saw at boot — so every published change writes
   *  the rows (with the run they belong to) back over the cached answer. */
  const rememberCards = (rows: PermissionRow[]) => {
    const cache = deps.historyCache;
    if (!cache) return;
    // Under the id the conversation was RESTORED with, not `state.sessionId`:
    // the first poll can re-point that to the id the CLI minted
    // (`--fork-session`, `noteSessionId`), while the wall's tile and the Peek
    // opened on it both still name the task's original session. Mirrored under
    // the live id as well when the two differ, so either spelling opens warm.
    const runId = activeRun || rows.find((r) => r.runId)?.runId || "";
    for (const sid of new Set([restoredSid, state.sessionId])) {
      if (!sid) continue;
      const had = cache.get(FILE || "", sid);
      if (!had) continue;
      cache.set(FILE || "", sid, { ...had, live_run: runId || had.live_run || "", permissions: rows });
    }
  };
  /** The session id `openSession` last restored — the cache key `rememberCards`
   *  writes back under (see there). */
  let restoredSid = "";

  /** The reason `sendError` is cleared HERE and not by the card: the row is the
   *  controller's, and a retry has to disable the buttons again — which the card
   *  reads off the absence of this string (PermCard's `busy`). */
  const clearSendError = (id: string) => {
    const prev = permCards.get(id);
    if (!prev || !prev.sendError) return;
    const next = { ...prev };
    delete next.sendError;
    permCards.set(id, next);
    publishPermissions();
  };

  const failSend = (id: string, message: string) => {
    const prev = permCards.get(id);
    if (!prev) return;
    permCards.set(id, { ...prev, sendError: "Could not send that: " + message });
    publishPermissions();
  };

  const resolveLocally = (
    id: string,
    decision: LandedDecision | string,
    scope: "" | DecisionScope,
    mode: "" | SwitchableMode,
    answers: Record<string, string>,
  ) => {
    const prev = permCards.get(id);
    if (!prev) return;
    permCards.set(id, {
      ...prev,
      decision: decision as PermissionRow["decision"],
      scope,
      mode,
      answers,
      placement: "parked",
      parkedIn: prev.parkedIn ?? activeTurnKey,
    });
    publishPermissions();
  };

  // ---- working line (T:14774-14926 — the VERBS are the UI's) -------------

  let workingStartedAt = 0;
  /** WHEN THIS TURN STARTED, kept across a reload (owner E2E R1, F10): the
   *  working line's "(10s)" restarted from 0 on F5 because the start was a
   *  controller-local `now()`. The frame that starts a turn stamps the run
   *  in sessionStorage; a frame that re-attaches to the same run reads the
   *  stamp back. Same tab only, which is the reload case; a new tab starts
   *  its clock at attach, as before. Cleared when the loop ends so an
   *  idle-time send into the same host does not inherit the last turn's
   *  clock. */
  const turnStartKey = (runId: string) => `fused-render:claude-turn-start:${runId}`;
  const turnStartedAt = (runId: string): number => {
    try {
      const saved = Number(sessionStorage.getItem(turnStartKey(runId)) || 0);
      if (saved > 0 && saved <= now()) return saved;
      const at = now();
      sessionStorage.setItem(turnStartKey(runId), String(at));
      return at;
    } catch {
      return now();
    }
  };
  const forgetTurnStart = (runId: string) => {
    try {
      sessionStorage.removeItem(turnStartKey(runId));
    } catch {
      /* storage refused; nothing to forget */
    }
  };

  const setStats = (
    tokens: number,
    phase: Phase,
    retry: RetryInfo | null,
    activity: Activity | null,
    external = false,
  ) => {
    const working: Working = {
      phase,
      activity,
      retry,
      tokens,
      startedAt: workingStartedAt,
      ...(external ? { external: true } : {}),
    };
    emit({ working });
  };

  // ---- the poll loop (T:16204-16428) -------------------------------------

  async function pollLoop(
    runId: string,
    gen: number,
    opts: { ownTurn?: boolean; priorReply?: { text: string } | null } = {},
  ): Promise<void> {
    const seat = ++loopSeq;
    // This run is now the one the stop button aims at. Set here, the one place a
    // run is ever in flight, which covers a re-attached run for free.
    activeRun = runId;
    // And it is a run this page has SHOWN — the schedule poller must never
    // re-attach to it once it ends (Bugbot PR #1075).
    if (runId) shownRuns.add(runId);
    activeSeat = seat;
    // WHICH TRANSCRIPT THIS LOOP IS WRITING INTO, for the `ownRunEndedAt` stamp
    // in the `finally`. `logGen` alone cannot answer it: `openSession` replaces
    // the visible conversation WITHOUT bumping the generation (it holds the
    // `sending` gate instead), so this counter is the only thing that moves.
    const tGen = state.transcriptGen;
    workingStartedAt = turnStartedAt(runId);
    setRunningUi(true);
    setStats(0, "thinking", null, null);
    noteChatActivity();

    /**
     * ONE BUBBLE PER REPLY IN THE PAYLOAD, keyed by SLOT.
     *
     * A poll payload used to be one reply, and this was a single `bubble` plus
     * a `segBase`/`textBase` pair frozen when `followupSeq` moved (D687). The
     * freeze is a GUESS at the seam — "the lengths as of the poll before the
     * send" — and it is wrong by however much of the first reply streamed
     * after the send, because agent.py blanks `text`/`segments` for the whole
     * `pending_echo` window and then hands back the FIRST reply complete on
     * the poll where the echo lands. So the first reply's remainder rendered
     * under the follow-up's own bubble, and the follow-up's real answer sliced
     * to nothing behind it: "the remaining response is cut off and it looks
     * stuck on the queued message" (QA feedback #9), while a reload looked
     * right because `_history` splits on the same echo rows with the whole
     * file to do it with.
     *
     * The seam is now REPORTED, not guessed — `poll.turn_breaks`
     * (`_absorbed_turn_breaks`) — so a payload with N seams is N+1 replies and
     * each gets its own bubble, in payload order, which is the order the
     * follow-up bubbles between them already sit in (`sendFollowUp` appends at
     * the tail). That is the reloaded transcript, live.
     *
     * The SLOT, rather than the payload index, is what survives the cursor
     * moving: `_read_current_turn` advances past a seam once a `result` closed
     * the reply before it, and from that poll on the payload is only the newer
     * reply with no seam in it at all. `chunkOffset` absorbs that (see the
     * shrink test in the loop), so slot 0 stays slot 0's bubble for life.
     */
    interface Chunk {
      /** The transcript row, or null before this reply produced anything. */
      key: string | null;
      /** The container number for `cardKey`, allocated once (see below). */
      seq: number;
      segMode: boolean;
      tailText: string | null;
      /** The legacy flat text as of the last poll that carried any — kept
       *  apart from the segment tail so a `done` poll with an empty body
       *  cannot wipe the bubble it already filled. */
      flatText: string;
      /** THE LAST VIEW THIS SLOT PRODUCED, handed back to `pollBody` so a tool
       *  row whose status/output/images have not moved keeps its `seg` OBJECT
       *  and `ToolChip`'s `memo` hits (T:15549-15554). Per slot, because the
       *  carry-over is keyed by `cardKey`, which is per container. */
      view: SegmentView | null;
    }
    const chunks = new Map<number, Chunk>();
    const chunkAt = (slot: number): Chunk => {
      let c = chunks.get(slot);
      if (!c) {
        c = { key: null, seq: 0, segMode: false, tailText: null, flatText: "", view: null };
        chunks.set(slot, c);
      }
      return c;
    };
    /** How many seams the CURSOR has already carried out of the payload. */
    let chunkOffset = 0;
    /** The previous poll's payload sizes and seam count, for the shrink test. */
    let prevSegLen = 0;
    let prevTextLen = 0;
    let prevBreaks = 0;
    /** The previous non-blank poll's whole window text, for the continuity
     *  test: a window only ever GROWS, so a payload that does not start with
     *  the last one is a window that moved. */
    let prevFullText = "";
    /** The previous non-blank poll's window start (`poll.window`), when the
     *  agent reports one: the cursor itself, and the one read that needs no
     *  inference. */
    let prevWindow: number | null = null;
    let seenFollowupSeq = followupSeq;
    /**
     * HOW MANY SEAMS ARE STILL OWED — a COUNT, not a flag (Bugbot PR #1061).
     *
     * One per follow-up that landed and whose reply this loop has not yet seen
     * the cursor step over. Only while it is above zero can a shrink be read as
     * the cursor stepping over a seam.
     *
     * It was a boolean, and a boolean is wrong for two follow-ups absorbed into
     * ONE run: the first cursor step disarmed it, so the SECOND step — which
     * commonly arrives with no `turn_breaks` at all, the newer reply alone in
     * the window — was read as an ordinary payload and rendered into the
     * PREVIOUS slot, overwriting the reply before it. Counted, the test stays
     * armed until every landed follow-up has been placed.
     *
     * Incremented by the DELTA, not by one: `followupSeq` can move by more than
     * a step between two polls (two sends inside one 400 ms lap), and each of
     * those owes a seam.
     */
    let pendingSeams = 0;
    /** The already-landed prefix of this run's window — see `landedWindow`. */
    let baseSeg = 0;
    let baseText = "";
    /**
     * WHETHER `baseSeg` IS SAFE TO SLICE `segs` WITH. `landedWindow` is always
     * trusted: both its numbers came from THIS controller's own reconstruction
     * of the very same live window. `priorReply` (below) is not — it comes
     * from `_history`, which segments a turn with NO stream deltas at all
     * (`_segments_from_rows`'s own docstring: "reply A carrying finalized text
     * only, followed by a reply B that streams, made `streamed` False for the
     * prefix and True for the window, and the offset then pointed at the wrong
     * segment entirely"). Its TEXT is still exact (byte-identical either way),
     * so `baseText` is seeded regardless — only the segment COUNT is left at 0
     * and marked untrusted, and the render loop below skips rather than slices
     * `segs` on it until either a reported seam confirms a count or the prefix
     * retires and there is nothing left to slice.
     */
    let baseSegTrusted = true;
    if (landedWindow && landedWindow.runId === runId) {
      baseSeg = landedWindow.segments;
      baseText = landedWindow.text;
    } else if (opts.priorReply && opts.priorReply.text) {
      // THE RELOAD ROAD's OWN BASE — see `priorReply`'s definition at its one
      // call site (`sendMessage`). `landedWindow` only ever covers a reply THIS
      // controller watched land; a page that restored its transcript from
      // `_history` (or a second tab open on the same conversation) has never
      // run a pollLoop to completion, so it has nothing there — but it already
      // has the reply rendered, and that rendering is exactly the prefix the
      // live window reopens on. Seeding it here means `adoptFirstSeam` below is
      // no longer the ONLY thing standing between a stale reply and a
      // duplicate bubble: it still runs, but now as a check against a base
      // already known to be right, not as the sole source of one.
      baseText = opts.priorReply.text;
      baseSegTrusted = false;
    }
    // CONSUMED EITHER WAY: one settled reply, one loop that may skip it. A base
    // left standing past the loop that could use it would hide the opening of
    // some later turn instead.
    landedWindow = null;
    /**
     * ONE TICK'S GRACE for `baseSegTrusted` to resolve on its own (a reported
     * seam confirms it, or the prefix retires because it no longer applies)
     * before this loop stops waiting for either and trusts `segs` regardless.
     *
     * NOT a guess: agent.py's cursor write is synchronous inside the very
     * `_read_current_turn` call that hands back an unconfirmed, combined
     * window (see `priorReply`'s note above) — so the NEXT poll's `segs` is
     * already clean whether or not this loop's own heuristics noticed. A
     * bug report (Bugbot, PR #1119) is why this exists as an explicit,
     * unconditionally-spent allowance rather than "keep waiting until the
     * text stops matching": a short reply that happens to share a prefix with
     * the one before it ("OK" answering into "OK, done") never makes that
     * text stop matching, and skipping every poll until it did — this loop's
     * first attempt — left `poll.done` unhandled right along with the
     * segments, `sending` stuck, and the composer dead.
     */
    let baseSegGrace = true;
    /**
     * A LOOP STARTED BY A SEND OWNS ONLY THE TURN IT SENT.
     *
     * `landedWindow` (or, on the reload road, `priorReply` above) covers the
     * case where the previous reply is already known. Either can miss: a fresh
     * controller with no prior turn at all, or a `priorReply` whose text the
     * live window's payload does not actually start with (the "already-landed
     * prefix retires" check below resets it if so — never trust it blindly).
     *
     * So the FIRST payload's own seams answer it too: a span the payload has
     * already closed off (`turn_breaks`) is a reply that ended BEFORE this send
     * — this loop was started by the send, so nothing it owns can have finished
     * yet — and the last of those seams is exactly where this turn begins. It
     * is adopted as the base (advancing it, never retreating it), and
     * everything below runs unchanged from there.
     *
     * Never set for an ADOPTED run (`resumeRun`, `adoptLiveRun`): there the
     * earlier spans are a cold read of turns this page has not rendered, and
     * skipping them would drop them.
     */
    let adoptFirstSeam = !!opts.ownTurn;
    let tick = 0;

    try {
      for (;;) {
        const data = (await run(
          dir,
          "poll",
          { run_id: runId, file: FILE || "", native: "1" },
          // The controller's own lifetime: `dispose` aborts, so an unmounted
          // chat's last poll does not run to completion on its own.
          { key: null, ...(life ? { signal: life.signal } : {}) },
        )) as PollResponse | { error: string; done: true };
        // The reader left; the run continues without this page.
        if (logGen !== gen || disposed) break;

        // agent.py's narrow early-exit: an unknown run, or one for another
        // target (agent.py:3802/3824). Not a steady-state body at all — but it
        // IS the end of the run, and T reaches the same error through `done` →
        // `runEnding` (T:16326-16374). So it takes that road too: a stop pressed
        // against a run that then reports unknown still gets its "Stopped."
        // note, and the working line still goes.
        if (isUnknownRun((data as { error?: string }).error)) {
          const message = String((data as { error?: string }).error);
          if (loopSeq === seat) clearRunParam();
          emit({ working: null });
          deps.onRunEnded?.();
          deps.onArtifactsTick?.();
          const gone = runEnding({ error: message }, stoppedSeat === seat);
          if (!gone.keepText) for (const c of chunks.values()) if (c.key) dropTurn(c.key);
          if (gone.error) reportTrouble(troubleOf("unknown-run", gone.error));
          if (loopSeq === seat && gone.note) addNote(gone.note, "⏹");
          break;
        }

        const poll = data as PollResponse;
        if (poll.session_id) noteSessionId(String(poll.session_id));
        if (tick++ % ARTIFACTS_EVERY_TICKS === 0) deps.onArtifactsTick?.();

        // usage arrives only at message end; estimate from streamed text meanwhile
        const tokens = Math.max(poll.tokens || 0, Math.round((poll.text || "").length / 4));
        setStats(tokens, poll.phase || "thinking", poll.retry ?? null, poll.activity ?? null);
        noteSkills(poll.skills);
        surfaceAppState(poll.app_state, runId);

        const segs = Array.isArray(poll.segments) ? poll.segments : [];
        const fullText = poll.text || "";
        const reported = Array.isArray(poll.turn_breaks) ? poll.turn_breaks : [];

        // A FOLLOW-UP LANDED. All this arms is the shrink test below: the seam
        // itself comes from the payload, so nothing about the transcript moves
        // until the payload says where it is (T:16269 froze the bases here and
        // guessed instead — see `Chunk`).
        if (followupSeq !== seenFollowupSeq) {
          pendingSeams += followupSeq - seenFollowupSeq;
          seenFollowupSeq = followupSeq;
        }

        // THE CURSOR STEPPED OVER THE SEAM. `_read_current_turn` advances past
        // an absorbed echo the moment a `result` closed the reply before it, so
        // the poll after that one carries ONLY the newer reply, with no
        // `turn_breaks` entry left to place it — and slot 0 would be the older
        // reply's bubble, overwritten with the answer to the follow-up.
        //
        // A payload only ever GROWS inside one window (that is the whole point
        // of the cursor rule), so a shrink is the window having moved. Read off
        // `segments` where there are any: `text` can shorten for an unrelated
        // reason on a delta-less run, where it falls back to the `result` row
        // (the LAST assistant message only) — see `_segments_from_rows`. And
        // only while a follow-up is outstanding, so no ordinary turn boundary
        // can be mistaken for one.
        // A BLANK PAYLOAD IS NOT A SHRINK. `pending_echo` makes agent.py return
        // `text: ""` / `segments: []` for the whole window between the send and
        // the echo, which is by far the commonest "smaller than last time" and
        // means the exact opposite of a cursor step: nothing has moved yet.
        const anyBody = segs.length > 0 || fullText.length > 0;
        if (pendingSeams > 0 && anyBody) {
          // A SEAM THE PAYLOAD LOST is the cursor having carried it out of the
          // window, and it is the reliable read: a window's seam count only
          // ever drops that way. It is also the only read the flat legacy text
          // path has, where the newer reply can be LONGER than the pair it
          // replaced and no length test can see the step.
          //
          // The shrink is the fallback, for a step this loop never saw the seam
          // for at all (the echo and the `result` both landed between two
          // polls, so the seam was never in a payload it read). Measured on
          // `segments` where there are any: `text` can shorten for an
          // unrelated reason on a delta-less run, where it falls back to the
          // `result` row — the LAST assistant message only.
          const lost = prevBreaks - reported.length;
          const shrank = prevSegLen
            ? segs.length < prevSegLen
            : !poll.done && fullText.length < prevTextLen;
          // THE CONTINUITY READ (owner E2E R1, F6): two short single-segment
          // replies leave the seam count AND the segment count unchanged
          // across a step the loop never saw the seam for — reply 1 `[A]`,
          // then reply 2 `[B]`, one segment each — so neither test above
          // fires, slot 0 is re-used, and reply 1 is overwritten with reply 2
          // (which then sits ABOVE its own user bubble). A window only ever
          // grows in place, so a payload whose text does not continue the
          // last one is the window having moved, whatever its size. Same
          // trick `baseText` already relies on below. A false positive costs
          // an extra bubble; a miss costs a reply.
          const moved =
            prevFullText.length > 0 && fullText.length > 0 && !fullText.startsWith(prevFullText);
          // THE CURSOR ITSELF, when agent.py says where the window starts
          // (Bugbot #1099): a follow-up reply that merely EXTENDS the one
          // before it ("OK" → "OK, done") keeps the seam count, the segment
          // count AND the prefix, so none of the three reads above can see
          // the step. The offset moving is the step, no inference needed.
          const slid =
            typeof poll.window === "number" && prevWindow !== null && poll.window !== prevWindow;
          if (lost > 0 || shrank || moved || slid) {
            // A LOST seam count is how many steps happened, so it settles that
            // many of the outstanding ones; a bare shrink is one step, and the
            // rest stay owed. Never below zero: a shrink for an unrelated
            // reason must not lend the next follow-up a step it did not make.
            const stepped = lost > 0 ? lost : 1;
            chunkOffset += stepped;
            pendingSeams = Math.max(0, pendingSeams - stepped);
          }
          // A seam APPEARING is not the answer: it is handled by the loop below
          // for as long as it stays in the payload, and the cursor may still
          // step over it later. `pendingSeams` stays armed until it does.
        }
        if (anyBody) {
          prevSegLen = segs.length;
          prevTextLen = fullText.length;
          prevBreaks = reported.length;
          prevFullText = fullText;
          if (typeof poll.window === "number") prevWindow = poll.window;
        }

        // THE SPANS THIS LOOP DID NOT SEND, taken as the base — see
        // `adoptFirstSeam`. Once only, off the first payload that carries a
        // body: after that, a seam is this turn's own and belongs to a bubble.
        if (adoptFirstSeam && anyBody) {
          adoptFirstSeam = false;
          const last = reported[reported.length - 1];
          if (last && (last.segments > baseSeg || last.text > baseText.length)) {
            baseSeg = last.segments;
            baseText = fullText.slice(0, last.text);
            // A REPORTED SEAM IS agent.py's OWN COUNT, measured against THIS
            // very payload's segmentation — unlike `priorReply`'s, it needs no
            // reconciliation.
            baseSegTrusted = true;
          }
        }

        // THE ALREADY-LANDED PREFIX RETIRES ITSELF the moment the window stops
        // OPENING on it, because that is the cursor having stepped over the
        // boundary: from that poll on the payload IS only this turn (see
        // `landedWindow`). Anchored on the prose rather than on a length,
        // because the newer reply is not reliably shorter than the pair it
        // replaced — [A, B-first-half] and [B-first-half, B-second-half] are
        // the same size and mean opposite things.
        //
        // A BLANK PAYLOAD IS NOT A STEP: the `pending_echo` window returns
        // `text: ""` for as long as the echo is outstanding and means the exact
        // opposite ("nothing has moved yet"), so the base has to survive it —
        // hence `anyBody`.
        if (
          baseText &&
          anyBody &&
          !(fullText.startsWith(baseText) && baseSeg <= segs.length)
        ) {
          baseSeg = 0;
          baseText = "";
          baseSegTrusted = true;
        }
        // `baseSegGrace` is spent here — UNCONDITIONALLY, the one poll this
        // loop first has anything to render at all with `baseSegTrusted`
        // still down — never only on the poll where suppression actually
        // fires. That is what keeps it a ONE-TIME allowance rather than a
        // wait for a condition that might not arrive (see its own note): by
        // the poll after the one it is spent on, agent.py's cursor is
        // provably already past the old turn, so `segs` needs no more
        // protecting whether or not `baseSegTrusted` itself ever flips.
        if (!baseSegTrusted && anyBody) {
          if (baseSegGrace) baseSegGrace = false;
          else baseSegTrusted = true;
        }
        // AN UNCONFIRMED TEXT BASE WITH SEGMENTS STILL IN THE PAYLOAD IS NOT
        // SAFE TO SLICE — see `baseSegTrusted`'s note. `baseText` is exact
        // either way, so `bodyText` below is unaffected: only `bodySegs` is
        // held back, for exactly the one poll `baseSegGrace` just spent, so
        // this reply still renders (as plain text, not segments, for that one
        // poll) rather than showing nothing, and every poll still reaches
        // `poll.done`/`syncPermissions` below whether or not this fires
        // (Bugbot, PR #1119 — the previous shape `continue`d past all of
        // that, and a short reply sharing a prefix with the one before it,
        // "OK" answering into "OK, done", never made the OLDER retirement
        // check below fire, so `sending` never cleared at all).
        const segSliceUnsafe = !baseSegTrusted && segs.length > 0;
        const bodySegs = segSliceUnsafe ? [] : baseSeg ? segs.slice(baseSeg) : segs;
        const bodyText = baseText ? fullText.slice(baseText.length) : fullText;
        // Rebased onto the body, and a seam that falls AT the base is the
        // boundary the base already stands for — it names no reply of ours.
        const rebased = baseText
          ? reported
              .filter((b) => b.segments > baseSeg || b.text > baseText.length)
              .map((b) => ({
                segments: Math.max(0, b.segments - baseSeg),
                text: Math.max(0, b.text - baseText.length),
              }))
          : reported;
        const breaks = rebased;

        // One pass per reply the payload holds: N seams is N+1 replies, each
        // sliced to its own span and rendered into its own slot.
        for (let j = 0; j <= breaks.length; j++) {
          const from = j === 0 ? { segments: 0, text: 0 } : breaks[j - 1]!;
          const to = j < breaks.length ? breaks[j]! : null;
          const mySegs = to
            ? bodySegs.slice(from.segments, to.segments)
            : bodySegs.slice(from.segments);
          const myText = to ? bodyText.slice(from.text, to.text) : bodyText.slice(from.text);
          const slot = chunkOffset + j;
          const chunk = chunkAt(slot);
          // The container number is allocated only once there is something to
          // number, as T does inside `renderSegments` (T:15642) — a turn whose
          // first polls are empty used to burn one per tick.
          // `chunk.view` is the same slot's previous view: this is the 2.5×/s
          // path, and the one a streaming `Write` chip's uncapped `content` was
          // being re-serialised on every tick of.
          const body = pollBody(mySegs, myText, chunk.seq || cardSeq + 1, chunk.view);
          if (body.mode === "segments") chunk.view = body.view;
          // The flat body as of THIS poll, whatever mode it came in — T keeps
          // `fullText.slice(textBase)` per poll (T:16288). Read outside the
          // `mode === "text"` branch so a reply that flipped
          // text→segments→text (agent.py replaying without segments) still has
          // a body to restore.
          const pollFlat = body.mode === "text" ? body.text : chunk.flatText;
          if (body.mode === "empty") continue;
          if (!chunk.seq) chunk.seq = ++cardSeq;
          if (!chunk.key) {
            chunk.key = nextKey("a");
            pushTurn({
              role: "assistant",
              key: chunk.key,
              text: "",
              streaming: true,
              followup: slot,
            });
          }
          // A REPLY THE PAYLOAD HAS CLOSED OFF WITH A SEAM IS FINISHED, and
          // saying so here rather than only at `poll.done` is what keeps the
          // caret off an answer that ended minutes ago: only the LAST span is
          // still growing (the seam is the `result` that ended the one before
          // it), so every earlier one settles as soon as it is placed.
          const spanLive = j === breaks.length;
          if (body.mode === "segments") {
            if (!chunk.segMode) {
              // A first poll with text but no segments yet started this reply on
              // the legacy path — take it back BEFORE the segments render, or the
              // flat text stays behind them (T:16289).
              chunk.segMode = true;
            }
            chunk.tailText = body.view.tailText;
            replaceTurn(chunk.key, {
              segments: body.view.rows.map((r) => r.seg),
              text: chunk.tailText || "",
              streaming: spanLive,
            });
          } else {
            chunk.flatText = pollFlat;
            if (!poll.done) replaceTurn(chunk.key, { text: chunk.flatText, streaming: spanLive });
          }
          // THE NEWEST reply's bubble is where a resolved card parks — a card
          // answered now belongs in the turn now streaming, never in one the
          // payload has already closed off with a seam (T:16297-16301).
          if (j === breaks.length && chunk.segMode) activeTurnKey = chunk.key;
        }

        // AFTER the reply bubble is guaranteed to exist for anything this poll
        // carried, so an open card lands BELOW the prose it interrupts
        // (T:16305-16311).
        syncPermissions(poll.permissions, runId, poll.mode);
        // …AND THE ADOPTION IS NOW SETTLED. This is the first moment
        // `state.permissions` is the truth about the adopted run rather than
        // the empty list a restore starts with, so it is the earliest point a
        // renderer can paint the transcript and its open card in ONE frame.
        // Cleared unconditionally (not only on an adopted run): a poll loop
        // running at all means there is nothing left to wait for.
        setAdopting(false);

        if (poll.done) {
          // OWNERSHIP-GUARDED: a respawn kills this run and starts a NEW loop
          // while this one is still on its way back from the poll it already
          // sent — its own `done` must not clear the param the newer loop set,
          // or a reload cannot re-attach (T:16313-16318).
          if (loopSeq === seat) clearRunParam();
          emit({ working: null });
          deps.onRunEnded?.();
          // T does one final `pollArtifacts()` at the run's end (T:16347), and
          // controller-api promises "every 8th poll AND once at the run's end".
          deps.onArtifactsTick?.();

          const end = runEnding(poll, stoppedSeat === seat);
          // WHAT IS NOW ON SCREEN, for whatever loop the next send starts — the
          // RAW window sizes, which is the shape the next payload arrives in
          // (see `landedWindow`). Only where the text stays: a dropped bubble
          // is not a landed reply, and the next loop must be free to render
          // that window again. Ownership-guarded like every other write here.
          if (loopSeq === seat) {
            landedWindow = end.keepText
              ? { runId, segments: segs.length, text: fullText }
              : null;
          }
          // EVERY bubble this loop opened settles, not just the newest: a run
          // that absorbed a follow-up has two, and leaving the older one
          // `streaming: true` parks the caret on a reply that finished minutes
          // ago.
          const live = [...chunks.values()].filter((c) => !!c.key);
          if (!end.keepText) {
            for (const c of live) dropTurn(c.key as string);
          } else if (live.length) {
            for (const c of live) {
              if (c.segMode) {
                replaceTurn(c.key as string, { streaming: false, text: c.tailText || "" });
              } else {
                // The DONE poll's own text (T:16288/16360), so a poll that
                // legitimately shortened the body can shrink the bubble and the
                // typer's clamp fires (T:15075). An empty done poll is the one
                // case that must not wipe a bubble it already filled, so it
                // keeps what the last non-empty poll left.
                replaceTurn(c.key as string, { streaming: false, text: c.flatText });
              }
            }
          } else if (poll.text || (poll.segments || []).length) {
            // A run whose text only ever arrived on the poll that ENDED it, so
            // nothing was ever streaming (T:16345-16354). Seams still apply:
            // the payload can be two replies even when none of it streamed.
            // The BASE-ADJUSTED body and its seams, not the raw payload: a
            // turn whose text only ever arrived on the poll that ended it can
            // still open on the reply already on screen (see `landedWindow`).
            const spans = breaks;
            const allSegs = bodySegs;
            const allText = bodyText;
            for (let j = 0; j <= spans.length; j++) {
              const from = j === 0 ? { segments: 0, text: 0 } : spans[j - 1]!;
              const to = j < spans.length ? spans[j]! : null;
              const view = pollBody(
                to ? allSegs.slice(from.segments, to.segments) : allSegs.slice(from.segments),
                to ? allText.slice(from.text, to.text) : allText.slice(from.text),
                ++cardSeq,
              );
              if (view.mode === "empty") continue;
              pushTurn({
                role: "assistant",
                key: nextKey("a"),
                text: view.mode === "segments" ? view.view.tailText || "" : view.text,
                ...(view.mode === "segments" ? { segments: view.view.rows.map((r) => r.seg) } : {}),
                followup: chunkOffset + j,
              });
            }
          }
          // The comeback is gated on ownership like the stop note below: a
          // superseded loop's end must not print or schedule anything.
          if (end.error && limitHit(poll.quota) && loopSeq === seat) {
            if (!scheduleComeback(runId, end.error, poll.quota)) addError(end.error);
          } else if (end.error) addError(end.error);
          // Same guard: a superseded loop's own "Stopped." must not land in the
          // log while the newer loop's turn is the one actually streaming.
          if (loopSeq === seat && end.note) addNote(end.note, "⏹");
          break;
        }
        await sleep(POLL_MS);
      }
    } catch (err) {
      // Drop the partial bubble so a failed run never leaves a half-streamed
      // reply behind. The `run` param is deliberately NOT cleared: a poll that
      // died with the page being torn down should still re-attach next boot.
      //
      // A DISPOSED controller says nothing: the poll we aborted ourselves is
      // not a failure of the run, and there is nobody left to read a card.
      if (!disposed) {
        for (const c of chunks.values()) if (c.key) dropTurn(c.key);
        reportTrouble(troubleFromError(err));
      }
    } finally {
      emit({ working: null });
      // Ownership means being the NEWEST loop, not matching the run id: a chat
      // left mid-turn and reopened re-attaches to the SAME run_id, and the
      // abandoned loop's late finally used to strip the new loop's Stop square
      // (Bugbot, PR #653).
      if (loopSeq === seat) {
        activeRun = null;
        activeSeat = 0;
        activeTurnKey = null;
        setRunningUi(false);
      }
      // T:16411 `ownRunEndedAt` — when THIS frame's own run ended, so PR4's
      // transcript follower can tell rows this page just wrote from somebody
      // else's turn arriving over the top of them (D415). TURN BOUNDARY EITHER
      // WAY, exactly like `noteChatActivity` below and for the same reason: a
      // superseded loop still wrote rows into this transcript, and a stamp
      // older than those rows lets `followDecision`'s own-echo guard read this
      // page's own turn back as somebody else's.
      //
      // GUARDED ON THE TRANSCRIPT, not on the seat. The stamp is a fact about
      // ROWS — "this frame wrote the tail of the conversation on screen" — so
      // a loop whose conversation is GONE has nothing to say about the one that
      // replaced it, and saying it anyway made the next session inherit the
      // previous turn's stamp: `newChat` cleared it and the abandoned loop's
      // late `finally` wrote "now" back over the fresh state, while
      // `openSession` never cleared it at all (Bugbot, PR #1075). Then
      // `followDecision`'s own-echo rule reads somebody else's rows in the NEW
      // chat as this page's own and suppresses the refresh they should trigger.
      if (logGen === gen && state.transcriptGen === tGen) emit({ ownRunEndedAt: now() });
      forgetTurnStart(runId);
      // Nothing is "queued for this turn" once the turn is over: the CLI drains
      // its queue as part of the run, so whatever is still listed here has
      // either been answered above or died with the process. Guarded on
      // ownership for the same reason the chrome is — an abandoned loop's late
      // finally must not wipe the hint the newer loop's follow-up just put up.
      if (loopSeq === seat) clearQueued();
      // Turn boundary either way — the tasks surfaces should hear about it even
      // when a newer loop owns the chrome (the ACTIVITY is real regardless).
      noteChatActivity();
    }
  }

  // ---- send (T:16460-16690) ----------------------------------------------

  const curModel = () => deps.model?.() || "";
  const curEffort = () => deps.effort?.() || "";
  const curPermission = () => deps.params.get("permission") || DEFAULT_PERMISSION;
  /** The same read, NARROWED — `ChatState.permissionMode` is a union and the
   *  param is arbitrary text off a URL. Deliberately separate from
   *  `curPermission`, which stays a pass-through: T sends `permission_mode`
   *  verbatim (T:16128) and agent.py is the thing entitled to reject it. */
  const curPermissionMode = (): PermissionMode => {
    const v = deps.params.get("permission") || "";
    return v === "plan" || v === "prompt" || v === "acceptEdits" || v === "auto"
      ? v
      : DEFAULT_PERMISSION;
  };
  /** `"1"` / `"0"` / `""` — see `ChatControllerDeps.hasPane`. The empty string
   *  is not a missing field: agent.py's `main` reads `has_pane == ""` as "the
   *  page has no opinion" and answers with `_has_pane(file)` itself, which is
   *  what an undecided pane must send rather than a guess that sticks for the
   *  life of the session (R2-10). No `deps.hasPane` at all is still `"0"`: a
   *  caller that does not wire a pane has decided there is none. */
  const hasPane = () => {
    if (!deps.hasPane) return "0";
    const answer = deps.hasPane();
    return answer === null ? "" : answer ? "1" : "0";
  };
  /**
   * THE PUSH CHANNEL, read once per outgoing message (T:16483 `appStatePush()`
   * at the top of `sendMessage`).
   *
   * `<live-app-state>` used to be sent by nothing at all: the watcher grew
   * `blockForSend()` and nobody ever called it, so the native chat told the
   * agent less about the app than legacy did on every single turn — and the
   * "app state attached" receipt had nothing to report, which is how QA found
   * it (feedback #30).
   *
   * Never throws: a failed offload is not a reason to lose the message, and the
   * pull channel is still there to answer a tool call that asks.
   */
  const appStateBlock = async (): Promise<string> => {
    if (!deps.appStateBlock) return "";
    try {
      return (await deps.appStateBlock()) || "";
    } catch {
      return "";
    }
  };

  /**
   * A SEND THAT NEVER HAPPENED owes the composer back what it was carrying.
   *
   * `ClaudeChat.beginSend` empties the tray and parks the pictures under the
   * very `Receipt[]` it hands down here BEFORE this function runs, so every road
   * out of a send has to say whether it went — including the two that refuse
   * before anything is attempted (already sending; disposed). Returning silently
   * from those left the tray empty and the map holding the only handle to the
   * user's pictures, which is the picture disappearing (Bugbot, PR #1064).
   */
  const returnSend = (text: string, opts: SendOptions, refused = false): void => {
    // The optimistic bubble goes with it: this send never happened, so the row
    // its caller put up ahead of the capture has nothing behind it. Dropped
    // HERE rather than by the caller, because the caller cannot tell a refusal
    // from a send that got as far as `addUser` — and adoption has already made
    // the two the same row (Bugbot, PR #1074).
    if (opts.optimisticKey) dropOptimisticUser(opts.optimisticKey);
    deps.onSendReturned?.({
      text,
      // REFUSED means the message was turned away before `addUser` ever ran:
      // there is no bubble and no queue entry holding the words, so the caller
      // has to put them back in the box or they are gone (Bugbot, PR #1074). A
      // send that got as far as a bubble and then failed is NOT this: it left
      // the failure in the transcript, which is where the reader stays to read
      // it (`sendMessage`'s catch).
      ...(refused ? { refused: true as const } : {}),
      ...(opts.attachments ? { attachments: opts.attachments } : {}),
      // WHICH send came back. A counter could not tell "this send returned"
      // from "some send returned while this one was out", and a second submit
      // inside the first one's capture window made the first roll back a turn
      // the agent had already taken (`SendOptions.sendId`).
      ...(opts.sendId ? { sendId: opts.sendId } : {}),
    });
  };

  async function sendMessage(text: string, opts: SendOptions = {}): Promise<void> {
    // DISPOSED IS A CLOSED DOOR, not a race to lose. `dispose()` is the
    // unmount, and every entry point below it emits into a store nobody reads
    // and writes params for a page that is gone — worse, `sendMessage` would
    // SPAWN a run. Callers hold the controller across awaits by construction
    // (ClaudeChat's boot walks it), so refusing here is the one place that can
    // be sure (Bugbot, PR #1061).
    if (disposed) {
      returnSend(text, opts, true);
      return;
    }
    // ONE TURN AT A TIME, and the refused one is refused OUT LOUD: its pictures
    // are already out of the tray by now, and so are its WORDS.
    if (sending) {
      returnSend(text, opts, true);
      return;
    }
    sending = true;
    const seat = ++sendSeq;
    // Sampled BEFORE anything is awaited: a Back landing mid-start outdates this
    // number, and everything below that touches the transcript or the params
    // checks it first (Bugbot, PR #653).
    const gen = logGen;
    const blocks = opts.blocks || [];
    if (!text && !blocks.length) {
      sending = false;
      returnSend(text, opts, true);
      return;
    }
    clearTrouble();
    // NO "starting" STATUS FOR THE ROUND TRIP, deliberately (D2, QA PR #1061,
    // closed the other way in PR3). `appStateBlock` + `live_host` + `send` /
    // `start` is a multi-second await with no run id yet, and QA saw the button
    // still read Send while a second Enter vanished into the `sending` gate. T
    // behaves the same way on the first half: `setRunningUi(true)` is pollLoop's
    // (T:16208), so the Stop face arrives only with the run id — a Stop with no
    // run to stop is a lie. The second half — the eaten Enter — is the
    // composer's to fix, and PR3's send door did (ClaudeChat `dispatchSend`):
    // the seat reads Send, disabled, and the words stay in the box until the
    // run is live. Emitting "starting" here would have flipped that seat to
    // Stop (Composer treats it as running) and undone the door.
    // The mode this turn is SPAWNED in, before anything is awaited: it is the
    // live mode until a poll reports one, and a card can open on the very first
    // poll (T:16128 `permission_mode`).
    setPermissionMode(opts.permission || curPermissionMode());
    // Read BEFORE the bubble so `raw` is the whole wire from the start — the
    // "what was sent" popover and the receipt describe one message, and a
    // bubble patched a tick later would briefly disagree with both. `hasPane`
    // is not consulted: the watcher answers "" for a pane it has learned
    // nothing from, which is the same non-answer a missing pane gives.
    const live = await appStateBlock();
    // THROUGH `composeBlocks`, never appended: the tray's `<pane-shot>` is
    // already in `blocks`, and `[...blocks, live]` put the state AFTER the
    // pictures — §D's reading order is state → pane-shot → annotations → text.
    // `composeBlocks` ranks `<live-app-state>` first wherever it arrives from
    // (Bugbot, PR #1064).
    const outgoing = composeOutgoing(text, composeBlocks(blocks, live ? [live] : []));
    // WHAT IS ALREADY ON SCREEN, taken BEFORE `addUser` appends this turn's own
    // bubble — see `priorReply`'s one consumer, `pollLoop`'s base seeding. A
    // controller that restored this conversation from `_history` (a reload, or
    // a second tab open on the same chat) has never run a pollLoop to
    // completion, so `landedWindow` is empty even though the reply is right
    // there, rendered. Sending into a still-live host reopens the window on
    // that same reply (agent.py's cursor cannot advance past it until a NEWER
    // turn proves it closed), and without this the only defense left is
    // `turn_breaks`, which the backend deliberately withholds whenever
    // anything — most commonly a D415 wake — sits between the old `result` and
    // this send's own echo (`_absorbed_turn_breaks`'s adjacency rule). Missing
    // that seam left the old reply duplicated into the new bubble until the
    // cursor finally stepped over it. Only the TEXT travels, never a segment
    // count: `_history` segments a turn with no stream deltas at all, which
    // can disagree with a live poll's reconstruction of the very same rows
    // once a newer turn's deltas are in the same window (`pollLoop`'s
    // `baseSegTrusted` is what makes that safe to seed anyway).
    //
    // SKIP THIS SEND'S OWN OPTIMISTIC BUBBLE FIRST. `dispatchSend`
    // (ClaudeChat.tsx) calls `postOptimisticUser` — which is `addUser` under
    // another name — SYNCHRONOUSLY, before this function's caller ever runs,
    // so by the time this line executes `state.turns` usually already ends
    // with THIS message's own user turn: `addUser` below only adopts that row
    // in place (see its own docstring — "the optimistic row is already the
    // last bubble in the log"), it does not push a second one. Reading
    // `state.turns[state.turns.length - 1]` without skipping past it read this
    // send's own words back as "the reply already on screen", found no
    // assistant turn, and seeded nothing at all — every real send through the
    // composer takes this road, since a bare `sendMessage()` call with no
    // optimistic bubble first (every one of this file's own tests) is the
    // exception, not the rule (Bugbot report + user recording, PR #1119).
    //
    // Only a SETTLED assistant turn counts once that skip lands on one: a
    // streaming one is either impossible here (`sending` is one-at-a-time) or,
    // if seen anyway, not a safe prefix to assume closed.
    const priorReply = (() => {
      let i = state.turns.length - 1;
      while (i >= 0 && state.turns[i]!.role === "user") i--;
      const last = state.turns[i];
      if (!last || last.role !== "assistant" || last.streaming) return null;
      return { text: last.text || "" };
    })();
    // The bubble shows what the user TYPED (or the markers for a wordless
    // send); the raw wire rides along for the "what was sent" popover.
    //
    // `stripBlocks(outgoing)` for a wordless send is unaffected by the block
    // above — `wire.ts` strips every `<live-app-state>` — so a send that is
    // only pictures still reads as pictures.
    const bubble = addUser(
      text || stripBlocks(outgoing),
      outgoing,
      opts.attachments,
      !!live,
      opts.optimisticKey,
    );
    let started = false;
    try {
      let runId = "";
      const sessionId = deps.params.get("session_id") || "";
      if (sessionId) {
        // A session already live for this file absorbs this message into its own
        // held-open `claude` process (Task 6) instead of `start` spawning a
        // second, parallel one against the same session_id. A brand-new chat has
        // no session_id, so the probe is skipped rather than spent on a lookup
        // that can only answer "" (T:16594-16604).
        let live: RunIdResponse | null = null;
        try {
          live = (await run(
            dir,
            "live_host",
            { file: FILE || "", session_id: sessionId },
            { key: null },
          )) as RunIdResponse;
        } catch {
          live = null; // no host reachable — fall through to start
        }
        if (live && live.run_id) {
          let sent: SendResponse | null = null;
          try {
            sent = (await run(
              dir,
              "send",
              {
                run_id: live.run_id,
                message: outgoing,
                read_dirs: JSON.stringify(opts.readDirs || []),
                model: curModel(),
                effort: curEffort(),
                permission_mode: opts.permission || curPermission(),
              },
              { key: null },
            )) as SendResponse;
          } catch {
            sent = null;
          }
          // Falsy on a network failure, `{error}` on a dead host, `{respawn}`
          // when the live session cannot honor this message as-is — every one of
          // those means "start fresh", same as no host at all (T:16644-16652).
          if (sent && "sent" in sent && sent.sent) runId = live.run_id;
        }
      }
      if (!runId) {
        const res = (await run(
          dir,
          "start",
          {
            file: FILE || "",
            message: outgoing,
            session_id: sessionId,
            model: curModel(),
            effort: curEffort(),
            permission_mode: opts.permission || curPermission(),
            // WHETHER THERE IS A PANE IS THIS PAGE'S ANSWER TO GIVE, and it is
            // sent on every turn (T:16609-16618, agent.py `_has_pane`).
            has_pane: hasPane(),
            // Granted for the SESSION, not the turn (Task 6): the process this
            // starts stays up across every follow-up it sends (T:16657-16668).
            read_dirs: JSON.stringify(opts.readDirs || []),
          },
          { key: null },
        )) as StartResponse;
        // `StartResponse` is `{run_id}` | `{error}`; agent.py answers exactly one
        // (agent.py:2452, plus main()'s own guards 5188-5191).
        const failed = (res as { error?: string }).error;
        if (failed) throw new Error(failed);
        runId = (res as { run_id: string }).run_id;
      }
      started = true;
      // A run id is in-flight bookkeeping — never a place the reader navigated
      // to — so no `run` write of any kind buys the visit's one history entry
      // (PR-3, T:16673-16678).
      if (logGen === gen) {
        setRunParam(runId);
        // `ownTurn`: this loop was started by THIS send, so anything the first
        // payload has already closed off is a turn that ended before it — see
        // `adoptFirstSeam`. `priorReply`: what was already on screen before
        // this send, in case `landedWindow` has nothing (the reload road).
        await pollLoop(runId, gen, { ownTurn: true, priorReply });
      }
      // else: the reader left during start — the run continues server-side and
      // resumeRun can re-attach; the landing gains no run param.
    } catch (err) {
      // The run never launched, so the agent never saw any of it: drop the
      // bubble so the composer's own rollback (attachments, notes) matches
      // (T:16693-16720).
      if (!started) {
        dropTurn(bubble.key);
        // ... and the attachments go back to the tray with the words, because
        // the agent never saw either (T:16693-16720).
        returnSend(text, opts);
      }
      reportTrouble(troubleFromError(err));
    } finally {
      if (sendSeq === seat) sending = false;
    }
  }

  // ---- follow-ups (T:16024-16185) ----------------------------------------

  async function sendFollowUp(text: string, opts: SendOptions = {}): Promise<void> {
    if (disposed) {
      returnSend(text, opts, true);
      return;
    }
    const gen = logGen;
    const blocks = opts.blocks || [];
    if (!text && !blocks.length) {
      returnSend(text, opts, true);
      return;
    }
    // Every send carries the app state, a follow-up included: T calls
    // `appStatePush()` from `sendFollowUp` too (T:16101), and a message typed
    // three tool calls into a turn is describing a pane that has moved since
    // the opening one.
    const live = await appStateBlock();
    // THE READER MAY HAVE LEFT DURING THAT AWAIT. `newChat` (Back) cleared the
    // transcript and the queue while the app-state block was being built, and a
    // bubble posted now would land in the LANDING — a conversation this text
    // was never typed into — with nothing downstream willing to take it back
    // (every failure road below is guarded on `logGen === gen` for exactly
    // this reason). Nothing to hand back either: Back strands the composer's
    // own text by its own rule (ClaudeChat `onBack`). Same for a dispose.
    if (logGen !== gen || disposed) return;
    // THROUGH `composeBlocks`, never appended: the tray's `<pane-shot>` is
    // already in `blocks`, and `[...blocks, live]` put the state AFTER the
    // pictures — §D's reading order is state → pane-shot → annotations → text.
    // `composeBlocks` ranks `<live-app-state>` first wherever it arrives from
    // (Bugbot, PR #1064).
    const outgoing = composeOutgoing(text, composeBlocks(blocks, live ? [live] : []));
    // The follow-up's bubble goes up immediately; the `followupSeq` bump that
    // tells a streaming pollLoop to start a NEW bubble after it happens only
    // once the INBOX has taken it. Bumping here left a failed send with a
    // counter pollLoop read as a landed follow-up, and the reply split around
    // the gap where the rolled-back row had been (Bugbot, PR #996).
    const bubble = addUser(
      text || stripBlocks(outgoing),
      outgoing,
      opts.attachments,
      !!live,
      opts.optimisticKey,
    );
    // KEYED BY A SEQ, not by the text: two identical follow-ups ("again") used
    // to collapse into one entry, and the first ack cleared both — so the second
    // one's hint left the composer while the message was still in flight.
    const entry = {
      seq: ++queuedSeq,
      wire: outgoing,
      typed: text,
      bubble: bubble.key,
      // Flipped below, when the inbox confirms — see `queued`'s own note and
      // `stopRun`'s handback rule.
      landed: false,
      // The pictures ride here so a STOP can give them back; `giveBack` reads
      // the same `opts` off its closure.
      opts,
      handedBack: false,
    };
    queued.push(entry);
    publishQueued();

    const drop = () => {
      const i = queued.indexOf(entry);
      if (i >= 0) queued.splice(i, 1);
      publishQueued();
    };
    const giveBack = () => {
      dropTurn(bubble.key);
      drop();
      // The same road sendMessage takes for a run that never launched: the
      // usual way a follow-up fails is the session having already ended, and
      // the pictures the user attached deliberately are owed back (T:16080-16093).
      //
      // ONCE, THOUGH: a Stop landing on an unconfirmed send already handed these
      // very pictures back, and this POST is only now settling behind it. A
      // second hand-back would re-add the same chips (or, for a host that keys
      // by the receipt array, be silently dropped) — either way the entry says
      // it is done.
      if (entry.handedBack) return;
      entry.handedBack = true;
      returnSend(text, opts);
    };

    // `activeRun` is set synchronously the moment pollLoop is entered, but
    // sendMessage's own `start` round-trip can still be in flight when a
    // follow-up is typed at that exact moment. Wait rather than drop: the CLI is
    // the only place text can wait now (T:16128-16137).
    let runId = activeRun;
    for (let tries = 0; !runId && tries < FOLLOWUP_WAIT_TRIES; tries++) {
      await sleep(FOLLOWUP_WAIT_MS);
      runId = activeRun;
    }
    if (!runId) {
      // GUARDED LIKE THE RESPAWN ROAD BELOW (`logGen === gen`, :1443). This road
      // has slept up to FOLLOWUP_WAIT_TRIES × FOLLOWUP_WAIT_MS, which is ample
      // room for a Back (or an `openOtherSession`) to land — and an unguarded
      // handback posts the red trouble card and re-injects the text into
      // whatever transcript is now current: the landing, or a different
      // conversation entirely. A stale failure stays quiet; `newChat` has
      // already cleared the queue and the bubble it would give back
      // (QA, PR #1061).
      if (logGen === gen) {
        giveBack();
        addError("Could not send: no run to attach this message to.");
      }
      return;
    }
    try {
      const res = (await run(
        dir,
        "send",
        {
          run_id: runId,
          message: outgoing,
          read_dirs: JSON.stringify(opts.readDirs || []),
          model: curModel(),
          effort: curEffort(),
          permission_mode: opts.permission || curPermission(),
        },
        { key: null },
      )) as SendResponse;
      if (res && "respawn" in res && res.respawn) {
        // The live session cannot honor this message as-is (a new attachment
        // directory, a changed effort) and has already ended itself.
        // sendMessage's live-host branch falls through to `start` for exactly
        // this response; do the same rather than report a generic send failure
        // for a message that was never actually rejected (T:16155-16177).
        const sessionId = deps.params.get("session_id") || "";
        const startedRes = (await run(
          dir,
          "start",
          {
            file: FILE || "",
            message: outgoing,
            session_id: sessionId,
            model: curModel(),
            effort: curEffort(),
            permission_mode: opts.permission || curPermission(),
            has_pane: hasPane(),
            read_dirs: JSON.stringify(opts.readDirs || []),
          },
          { key: null },
        )) as StartResponse;
        const failed = (startedRes as { error?: string }).error;
        if (failed) throw new Error(failed);
        const fresh = (startedRes as { run_id: string }).run_id;
        // A respawn re-sent this text as the OPENING message of a fresh run, so
        // it is not a follow-up waiting behind anything any more — it is the
        // turn now in flight. Drop the entry (the bubble stays: it is that
        // turn's own bubble).
        drop();
        if (logGen === gen) {
          setRunParam(fresh);
          void pollLoop(fresh, gen);
        }
        return;
      }
      if (!res || !("sent" in res) || !res.sent) {
        // Same guard, same reason as the `!runId` road above.
        if (logGen === gen) {
          giveBack();
          addError("Could not send: the session ended before this reached it.");
        }
        return;
      }
      // A follow-up landed as its own bubble above — bump so a pollLoop
      // streaming into an OLDER bubble knows to start a NEW one after it. HERE,
      // after the inbox took it (T:16179-16184).
      //
      // The queue entry is deliberately left standing: the inbox taking the
      // bytes is not the model reading them (see `queued`).
      //
      // MARKED LANDED in the same breath as the bump, because they are one
      // fact: the inbox has the message, so a stop from here on cannot claim
      // the CLI never received it (`stopRun`).
      entry.landed = true;
      followupSeq++;
    } catch (err) {
      // Same guard, same reason as the two roads above: a `send` that rejects
      // after the reader has left must not repaint a transcript it no longer
      // describes.
      if (logGen === gen) {
        giveBack();
        addError("Could not send: " + (err instanceof Error ? err.message : String(err)));
      }
    }
  }

  // ---- stop (T:15901-15926) ----------------------------------------------

  async function stopRun(): Promise<void> {
    if (disposed) return;
    const runId = activeRun;
    const seat: number = activeSeat;
    if (!stopAllowed(runId, seat, stoppedSeat)) return; // nothing live, or already going
    stoppedSeat = seat;
    emit({ status: "stopping" });
    try {
      const result = (await run(
        dir,
        "cancel",
        { run_id: runId as string, ...(queued.length ? { queued: "1" } : {}) },
        { key: null },
      )) as CancelResponse;
      // WHAT COMES BACK TO THE COMPOSER, and the rule is deliberately narrow
      // (feedback #11).
      //
      // Two roads only, because only two are provable:
      //
      //  1. `still_queued` — the CLI's OWN report, on the interrupt control
      //     response, of the follow-ups it dropped out of its queue unread
      //     (T:15911-15919). Whatever it names never reached the model.
      //  2. AN UNCONFIRMED SEND (`!entry.landed`) — the `send` POST was still
      //     in flight when Stop was pressed, so the inbox never acknowledged
      //     it. The interrupt is racing that write and the message may be
      //     dropped either side of it; the honest reading is "it did not
      //     land", and handing it back is recoverable where losing it is not.
      //
      // EVERYTHING ELSE STAYS A BUBBLE. A follow-up fed through the held-open
      // stdin is echoed into `out.jsonl` the moment the inbox drains, so by
      // the time an interrupt lands the CLI no longer counts it as queued and
      // answers `{"still_queued": []}` — which used to be read here as "the
      // CLI told us nothing, so assume everything was stranded" and hand the
      // whole queue back. That is the WRONG WAY ROUND on the common path:
      // Claude had already read the message (Surya's mid-response drain), and
      // yanking it out of the transcript and back into the box denied a turn
      // the reader had watched happen. An empty `still_queued` from a live
      // host is now taken at its word: nothing was queued, so nothing is
      // owed back.
      //
      // Either way the ENTRIES go: after a stop nothing is "queued for this
      // turn" any more, whether it was handed back or left standing.
      const still = (result && (result.still_queued as string[])) || [];
      // A COUNTED list, not a Set (Bugbot PR #1061). `still_queued` names WIRE
      // TEXTS, and two follow-ups can carry the same one — "again", "again",
      // both landed, both dropped unread. A Set collapsed them into a single
      // hand-back, so the second came back to nobody: its entry was spliced
      // out and its bubble dropped, leaving the text nowhere at all. Each
      // match now consumes exactly ONE name.
      const named = still.filter((t): t is string => typeof t === "string" && !!t);
      const stranded: string[] = [];
      for (const entry of queued.slice()) {
        // Matched against the WIRE form, which is what `still_queued` carries;
        // what goes BACK to the box is the TYPED form, because that is the text
        // the user owns and would edit — handing back the composed wire payload
        // would put the app-state and attachment markers into their box.
        const at = named.indexOf(entry.wire);
        // EVERY entry comes back (owner E2E R1, F7). A landed follow-up the
        // CLI did not name used to keep its bubble and leave the queue hint
        // — a message the reader apparently sent, with no reply, forever: the
        // backend ends the session tree the moment anything was queued, so
        // nothing was ever going to answer it. Claude Code's own Esc does the
        // same thing — the queued messages return to the input, editable.
        // Losing a bubble is recoverable; a bubble with no reply is not.
        stranded.push(entry.typed || entry.wire);
        // AND ITS PICTURES, but only for a send the inbox never confirmed. The
        // words go back through `onStranded`; the attachments are parked in
        // `ClaudeChat`'s `inFlight` map under this send's own `Receipt[]` and
        // come back only through `onSendReturned`, so a strand that returned
        // text alone dropped the bubble holding the only visible trace of them
        // and left the map holding the only handle — the picture disappearing
        // until the POST settled, and lost outright if it answered `sent`
        // (Bugbot, PR #1064).
        //
        // A LANDED follow-up the CLI named in `still_queued` is deliberately not
        // this: its bytes are already on disk in the agent's hands and its
        // receipts already rode a wire block, so only the words are owed back
        // (`onSendReturned`'s own note).
        if (!entry.landed && !entry.handedBack) {
          entry.handedBack = true;
          returnSend(entry.typed, entry.opts);
        }
        // The optimistic bubble goes with it. A follow-up the interrupt
        // stranded was never answered, so leaving the row posted claims the
        // agent read it — and QA saw exactly that: the text committed as a
        // permanent bubble AND absent from the box, with no way to recover or
        // edit it. T leaves the row behind (its stop only ever touches
        // `box.value`); this deliberately does not.
        dropTurn(entry.bubble);
        // Each stranded text clears ONE name, so two identical follow-ups
        // both come back and both leave the hint.
        if (at >= 0) named.splice(at, 1);
        const i = queued.indexOf(entry);
        if (i >= 0) queued.splice(i, 1);
      }
      // Anything the CLI named that this page has no entry for (a follow-up
      // from another viewer of the same session) is handed back verbatim
      // rather than lost — there is no typed form to prefer.
      for (const t of named) stranded.push(t);
      // The turn is over for every entry, handed back or not — so the hint
      // under the box goes either way, in ONE publish.
      const cleared = queued.length > 0;
      queued.length = 0;
      if (cleared || stranded.length) publishQueued();
      if (stranded.length) deps.onStranded?.(stranded);
    } catch (err) {
      // The kill never reached the backend, so the run is still going and the
      // loop is still streaming it. Take the claim back: leaving it set would
      // relabel this run's real ending as a stop the user never got.
      stoppedSeat = 0;
      // …but only for a run that is STILL the live one. `cancel` can reject
      // after pollLoop's own `finally` has already gone idle (T:16400-16406),
      // and reviving the Stop chrome for a run that is over leaves a stop
      // square on a working line that has left (T:15922's `setStopping(false)`
      // is inert on a removed row).
      if (activeRun === runId && activeSeat === seat) emit({ status: "running" });
      addError("Could not stop the run: " + (err instanceof Error ? err.message : String(err)));
    }
  }

  // ---- decide (T:13979-14584) --------------------------------------------

  const decide = async (
    fields: Record<string, string>,
    id: string,
    optimistic: Record<string, string>,
  ): Promise<DecideResponse | null> => {
    // The ONE door every card click goes through, so the disposed check lives
    // here rather than in each of the four callers.
    if (disposed) return null;
    // THE RUN THE CARD WAS BUILT WITH, not whatever is live now (T:13984 posts
    // the `run_id` closed over by `buildPermCard`). `sendFollowUp`'s respawn
    // re-points `activeRun`, and this click belongs to the process that asked.
    const runId = permCards.get(id)?.runId || activeRun;
    if (!runId) {
      // A click after the run ended. Silence here left the card at "sending…"
      // with every button disabled for good; T's catch is unconditional and
      // says so on the card (T:13996-14002).
      failSend(id, "the reply has already ended.");
      return null;
    }
    // A retry disables the buttons again, which the card reads off the ABSENCE
    // of a `sendError` — so the previous attempt's reason goes first.
    clearSendError(id);
    try {
      const res = (await run(
        dir,
        "decide",
        { run_id: runId, request_id: id, ...fields } as never,
        { key: null },
      )) as DecideResponse;
      if (res && res.error) throw new Error(res.error);
      // agent.py answers with what landed on DISK (first writer wins), so the
      // losing half of a double-click shows the answer the tool actually got.
      resolveLocally(
        id,
        (res && "decision" in res && res.decision) || fields.decision,
        ((res && "scope" in res && res.scope) || "") as "" | DecisionScope,
        ((res && "mode" in res && res.mode) || "") as "" | SwitchableMode,
        (res && "answers" in res && res.answers) || optimistic,
      );
      return res;
    } catch (err) {
      // The subprocess is still blocked, so the card has to come back.
      failSend(id, err instanceof Error ? err.message : String(err));
      return null;
    }
  };

  async function decidePermission(
    id: string,
    decision: Decision,
    scope: DecisionScope = "once",
    mode?: SwitchableMode,
  ): Promise<void> {
    const res = await decide({ decision, scope, mode: mode || "" }, id, {});
    // The picker follows what LANDED: "let Claude decide from here" changes the
    // session's permission mode (T:13988-13992). The LIVE mode moves with it —
    // the CLI is in the new mode from this decision on, and the next card must
    // not offer the escalation this one just granted.
    if (res && "mode" in res && res.mode) {
      // "replace", for the reason T:14574-14581 gives at its twin below: this
      // write is a CONSEQUENCE of a decision, not a place anyone navigated to,
      // and it lands behind an await — so the store's first-change push would
      // mint a history entry whose whole content is the mode the session has
      // already switched into, and the Back that undid it would do nothing
      // visible except put the picker back into a mode the CLI has left. The
      // picker's own dropdown still pushes: choosing a mode by hand IS a step.
      setParam({ permission: res.mode }, "replace");
      setPermissionMode(res.mode);
    }
  }

  async function answerQuestion(
    id: string,
    answers: Record<string, string[]>,
    custom: Record<string, string> = {},
  ): Promise<void> {
    // `{questionText: "label, label[, typed]"}` — one string per question, the
    // typed "Other" appended LAST (T:14059-14082, D407). `custom` always goes,
    // empty or not, so the backend never has to tell "no Other was used" from
    // "an older page".
    // `Object.fromEntries`, not `{}` + assignment: a question whose text is
    // `__proto__` would otherwise be dropped from the payload AND from the
    // chosen-labels display, which is the case D161 exists for (T:15421,
    // `summaries.ts` `leftoverInput`).
    const wire: Record<string, string> = Object.fromEntries(
      Object.entries(answers).map(([q, picked]) => [q, picked.join(", ")]),
    );
    await decide(
      {
        decision: "allow",
        scope: "once",
        answers: JSON.stringify(wire),
        custom: JSON.stringify(custom || {}),
      },
      id,
      wire,
    );
  }

  async function decidePlan(id: string, decision: Decision, mode?: SwitchableMode, note = ""): Promise<void> {
    const res = await decide({ decision, scope: "once", mode: mode || "", note }, id, {});
    // Approve always moves the picker off "plan" — to what landed, or "prompt"
    // (T:14548-14580).
    if (res && "decision" in res && res.decision === "allow") {
      const landed: PermissionMode = ("mode" in res && res.mode) || "prompt";
      // T:14580 writes this one with `{history:"replace"}` and spends six lines
      // on why: "The write is a CONSEQUENCE of approving a plan, not a place
      // anyone navigated to, and it lands behind `await runPython` — so the
      // first-change-push rule (D8/PR-3) would otherwise mint a history entry
      // whose whole content is the mode the session already switched into, and
      // the Back that undid it would do nothing visible."
      //
      // Worse than cosmetic here: Back landing on that entry put "plan" back in
      // the picker for a session that had already left plan mode, so the next
      // per-turn spawn re-entered it — the loop T's approval write exists to
      // break, re-created by the history entry.
      setParam({ permission: landed }, "replace");
      // The CLI leaves plan mode the instant it sees the plain allow, so the live
      // mode has to leave it too — otherwise every card for the rest of the run
      // still thinks it is mid-plan and withholds the escalation (T:14548-14580).
      setPermissionMode(landed);
    }
  }

  function dismissCard(id: string): void {
    // T:14118 `dismiss` is a real `deny` post that RESOLVES the card — the tool
    // call is BLOCKED until something answers it, and the card is the receipt
    // ("✗ Not answered", T:14126-14140). Filtering the row out instead lost the
    // receipt, and stranded a blocked run with nothing on screen whenever the
    // post failed.
    void decide({ decision: "deny", scope: "once" }, id, {});
  }

  // ---- app_state (T:15804-15837) -----------------------------------------

  async function answerAppState(id: string, block: string): Promise<void> {
    if (disposed) return;
    // The run that ASKED (T:16260 passes the loop's own `run_id`): a respawn in
    // between must not have this snapshot answered at it for a request it never
    // made.
    const runId = state.appState.find((r) => r.id === id)?.runId || activeRun;
    if (!runId || answeredStates.has(id)) return;
    answeredStates.add(id); // claimed before the await: polls overlap
    trim(answeredStates);
    // NO LINE OF THIS PAGE'S OWN (owner E2E R1, 2026-09-10). T appended a
    // "read app state" note at the end of the log when it answered — and the
    // reply kept streaming ABOVE it, so the note trailed the finished answer
    // like a stuck status, and a reload lost it. agent.py now emits the read
    // as a notice segment where it happened (`native=1` on poll/history →
    // `app_reads`), so it streams and restores in place.
    emit({ appState: state.appState.filter((r) => r.id !== id) });
    let res: AppStateResponse | null = null;
    try {
      res = (await run(
        dir,
        "app_state",
        // A JSON string, not a nested object: params cross into python
        // string-shaped, and never the bare `null` a snapshot can be — agent.py
        // reads a non-dict as a permanent failure (T:15819-15825).
        { run_id: runId, request_id: id, state: block },
        { key: null },
      )) as AppStateResponse;
    } catch {
      // The tool call is still blocked, so this has to be retried — un-claim the
      // id and let the next poll (400 ms) have another go.
      answeredStates.delete(id);
      return;
    }
    // A RESOLVED error is the same news as a throw WHEN IT SAYS SO: agent.py
    // flags which of its errors another go could ever help (T:15829-15836).
    if (res && "error" in res && res.error && res.retry) answeredStates.delete(id);
  }

  // ---- history / sessions -------------------------------------------------

  /**
   * ONE EMIT for a restored conversation: turns, and — when the answer names a
   * live run (`_history_live`) — its cards and the mode it is running in, with
   * the adoption gate DOWN in the same frame. The gate exists so the transcript
   * never paints once without its card (see `openSession`); with the card in
   * hand there is nothing left to hold it for. An answer without `live_run`
   * (older server, tests) keeps the gate up and the adopt watch discovers as
   * before. The first poll of the adopted run replays the same rows and
   * `syncPermissions` dedupes them by id.
   *
   * `fromCache` keeps `historyLoading` up: the fetch is still out.
   */
  function landHistory(res: HistoryResponse, fromCache: boolean): void {
    const live = typeof res.live_run === "string";
    if (live) {
      // THE FETCH REPLACES THE WARM PAINT, cards included: `syncPermissions`
      // only adds and updates by id, so a cache paint's card would survive an
      // answer that says the run is over (`live_run: ""`, no rows) and sit
      // there answerable, posting `decide` to a run that has ended (Bugbot, PR
      // #1112). Only the UNDECIDED cards the answer no longer names go: the
      // warm paint's gate is down, so a click can land while the fetch is out,
      // and a verdict that already reached the server must not come back as an
      // open card because this answer was built a moment before it (Bugbot,
      // round 2). `syncPermissions` keeps a landed decision over an incoming
      // row without one, so the decided card survives either way.
      if (!fromCache) {
        const named = new Set((res.permissions || []).map((p) => p && p.id));
        for (const [id, row] of permCards) {
          if (!named.has(id) && !row.decision) permCards.delete(id);
        }
      }
      syncPermissions(
        res.permissions,
        res.live_run || "",
        (res.mode || undefined) as PermissionMode | undefined,
        false,
      );
    }
    emit({
      turns: historyToTurns(res),
      transcript: res.transcript ?? null,
      ...(fromCache ? {} : { historyLoading: false }),
      ...(live ? { permissions: permissionRows(), adopting: false } : {}),
    });
  }

  async function openSession(sessionId: string): Promise<void> {
    if (disposed) return;
    // Reuse the `sending` gate: a message sent during the await would be
    // appended first and the older history turns dumped after it (T:17994).
    if (sending) return;
    sending = true;
    const seat = ++sendSeq;
    const gen = logGen;
    permCards.clear();
    notedSkills.clear();
    answeredStates.clear();
    notedStates.clear();
    nullStatePolls.clear();
    // THE OTHER ROAD THAT REPLACES THE VISIBLE CONVERSATION (Bugbot 3974975055
    // — see `shownRuns`). The rows about to arrive come from `history`, which
    // knows nothing of this page's run ids, so every recorded "already shown"
    // is about a transcript that is being thrown away. Cleared here rather than
    // relying on the `transcriptGen` bump, because the id an entering reader
    // most needs re-adopted is the one this frame streamed a moment ago.
    shownRuns.clear();
    claimingRuns.clear();
    // Published, not just emptied: the hint under the box belongs to the
    // conversation that is leaving.
    clearQueued();
    setParam({ session_id: sessionId });
    emit({
      sessionId,
      historyLoading: true,
      // THE VISIBLE CONVERSATION IS BEING REPLACED, and this counter is how the
      // React side hears about it — T calls `scheduleResetForNewTranscript()`
      // here, in `loadHistory`'s non-refresh branch and nowhere else (T:18000).
      // A counter rather than the session id: the id also changes when the
      // FIRST poll of a brand-new chat reports one (`noteSessionId`), mid-run,
      // and a reset there re-arms the schedule baseline so the next tick
      // silently writes off a scheduled run that fired in the window.
      transcriptGen: state.transcriptGen + 1,
      // Set HERE rather than in `adoptLiveRun`, which this function only
      // reaches after the history round-trip: the gate has to be up before the
      // first frame, or the transcript paints once without the card and the
      // flag arrives too late to have prevented it.
      adopting: true,
      turns: [],
      permissions: [],
      appState: [],
      skills: [],
      trouble: null,
      // A restored conversation has no live run, so the picker's param is the
      // only honest answer until the next poll reports one.
      permissionMode: curPermissionMode(),
      // AND NO RUN OF OURS HAS ENDED IN IT. The stamp belongs to the
      // conversation that is leaving; carried across, it tells the live-watch
      // follower that this page wrote the tail of a transcript it has never
      // written a row into, and the own-echo guard then swallows the first
      // outside turn to arrive (Bugbot, PR #1075). `newChat` clears it through
      // `emptyState`; this is the other way a transcript is replaced.
      ownRunEndedAt: 0,
    });
    // WHAT THIS PAGE ALREADY KNOWS ABOUT THE CONVERSATION paints before the
    // fetch is even sent: the Tasks wall loaded this chat into a tile, and the
    // Peek opened on that tile is a second controller with nothing of its own.
    // Transcript and cards together, gate down — the fetch below replaces it.
    restoredSid = sessionId;
    const cached = deps.historyCache?.get(FILE || "", sessionId);
    if (cached) landHistory(cached, true);
    try {
      const res = await fetchHistoryVia(sessionId);
      if (logGen !== gen || disposed) return;
      if (res.error) throw new Error(res.error);
      deps.historyCache?.set(FILE || "", sessionId, res);
      landHistory(res, false);
    } catch (err) {
      // A failed restore is not a trouble card in T either — it warns and leaves
      // an empty log (T:18057-18059).
      emit({ historyLoading: false });
      if (typeof console !== "undefined") {
        console.warn("history restore failed:", err instanceof Error ? err.message : err);
      }
    } finally {
      if (sendSeq === seat) sending = false;
    }
    // AND THEN ASK WHETHER THE SESSION IS BUSY. Not awaited: the adoption
    // watch is up to ~3 s of laps and the caller's next step is painting the
    // restored transcript and telling its host it is ready — neither of which
    // should wait on the answer. A `run` already on the URL is `resumeRun`'s
    // job and the `activeRun` guard inside makes the overlap harmless.
    if (!deps.params.get("run")) void adoptLiveRun(sessionId);
  }

  /**
   * IS ANYTHING STILL RUNNING FOR THIS CHAT? (T:17506-17663 `adoptLiveRun`.)
   *
   * A run id lives on the URL, and only there. Opening a conversation from the
   * session list, a folder listing, the cards wall or Peek hands over a
   * `session_id` and nothing else — so a turn that was mid-flight rendered as
   * a finished transcript with no working line, and, the way QA hit it
   * (feedback #25), a tool call blocked on a permission showed "waiting for
   * approval" with NO CARD to answer it, because cards only ever arrive on a
   * poll and nothing was polling.
   *
   * So the session is asked directly. `live_run` matches on the target first
   * and on either of the two ids that can name one chat (the session the run
   * resumed and the one the CLI minted for it — `--fork-session` makes those
   * different), and answers `""` when there is nothing going.
   *
   * PR1's SUBSET, deliberately: this is the WINDOW around opening a chat. The
   * standing watch that adopts a turn started later in the life of the page
   * (T:17553-17663, D415) is PR4.
   *
   * Every guard here is one of T's, and each is a way this can adopt into the
   * wrong transcript:
   *   * `logGen` — the reader went home; nothing on this page is ours.
   *   * `activeRun` — somebody attached already (this call, or the user's own
   *     send). The watch is over, not merely skipped.
   *   * `sending` — the gate is held THIS tick (history still restoring, a send
   *     in flight); look again next lap rather than give up.
   * A failed lookup ends the watch and leaves the transcript exactly as it
   * rendered: a missing working line is a smaller lie than a run adopted onto
   * a conversation this is no longer showing.
   */
  async function adoptLiveRun(sessionId: string, opts: AdoptOptions = {}): Promise<void> {
    if (disposed) return;
    // ONE WATCH AT A TIME. `openSession` starts one for every restore and the
    // boot starts one for the path where `openSession` bailed on the gate, so
    // the ordinary reopen asks twice — two lookups every 400 ms, and two
    // callers that could each reach `resumeRun` for the same id.
    if (adopting) return;
    adopting = true;
    const gen = logGen;
    try {
      await adoptWatch(sessionId, gen, opts);
    } finally {
      adopting = false;
      // THE BACKSTOP, not the ordinary road. Every early exit lands here — a
      // thrown lookup, a disposed controller, a generation bump, the laps
      // running out — so the renderer's gate can never be left up by a watch
      // that simply stopped. The ordinary clears happen earlier and sooner:
      // the first answer with no run in it, and the adopted run's first poll.
      setAdopting(false);
    }
  }

  async function adoptWatch(
    sessionId: string,
    gen: number,
    opts: AdoptOptions,
  ): Promise<void> {
    // ONE LAP is the STANDING WATCH's posture (T:17615): it is re-armed every
    // 5 s and by three events of its own, so laps here would only duplicate a
    // timer that already exists. The open-a-chat window keeps all eight.
    const laps = Math.max(1, opts.laps ?? ADOPT_LAPS);
    for (let tries = 0; tries < laps; tries++) {
      if (tries) await sleep(POLL_MS);
      if (logGen !== gen || disposed) return;
      if (activeRun) return;
      if (sending) continue;
      let live: RunIdResponse | null = null;
      try {
        live = (await run(
          dir,
          "live_run",
          { file: FILE || "", session_id: sessionId || "" },
          { key: null },
        )) as RunIdResponse;
      } catch {
        return;
      }
      // Both can have changed across the await.
      if (logGen !== gen || disposed || activeRun) return;
      const id = live && live.run_id;
      if (!id) {
        // NOTHING IS LIVE, AND THAT IS AN ANSWER — the transcript can paint.
        // The remaining laps exist to catch a run that starts a moment later
        // (a hand-off, another viewer's send), which is not something a first
        // paint should be held for: waiting them out would hide a restored
        // conversation for ~3 s every time it is reopened idle.
        setAdopting(false);
        continue;
      }
      if (sending) continue;
      /**
       * A RUN THIS FRAME ALREADY STREAMED IS NOT ADOPTED AGAIN (Bugbot, this
       * batch). `shownRuns` was being WRITTEN here and never read, which left
       * the half of its own contract undone — "a streamed run is attached,
       * never re-resumed".
       *
       * The window is real and now reliably reachable: `noteChatActivity`
       * announces at both turn boundaries, and at the END one the `busy()` gate
       * is already down, so the tile that ran the turn hears its own poke,
       * `live_run` still answers the id for a few seconds, and this frame
       * re-adopts the reply it just streamed — the done branch strips and
       * rebuilds the turn, bumps `repaired` (a forced scroll) and takes the
       * caret back. The 5 s interval could already hit the same window; the
       * in-document poke (P4-06) only made it certain.
       *
       * `continue`, not `return`: the remaining laps of the open-a-chat window
       * should go on looking for a DIFFERENT id, and the transcript follower
       * below is the coarser fallback for any tail this skips — which is exactly
       * the division of labour `tick` is built on ("run dirs first, always …
       * the transcript is the blinder fallback and only speaks for the turns no
       * run dir can account for").
       */
      // `claimingRuns` beside it for the OTHER half: an attach already in
      // flight for this id (the boot's `?run=`, the schedule poller) owns it
      // until it either attaches or lets go — see the declaration.
      if (shownRuns.has(id) || claimingRuns.has(id)) {
        setAdopting(false);
        continue;
      }
      setRunParam(id);
      // `quiet` rides through to the reconciliation: the watch may be adopting a
      // turn whose user line this transcript is already showing (the woken run
      // whose first turn this frame streamed) or one it has never shown (a send
      // made in another tab). Only `resumeRun` can tell those apart.
      await resumeRun(id, { quiet: !!opts.quiet });
      // `resumeRun` resolves at the END of the turn, so a completed call means
      // the run was handled — its own done branch cleared the param. The one
      // call that resolves with the param still reading `id` is a bail on a
      // gate grabbed between these two lines, and only that earns another lap.
      if (deps.params.get("run") !== id) return;
    }
  }

  /**
   * Re-attach to a run id — a `run` param at boot, a run the standing watch
   * found, a scheduled message that just fired. It retries a run dir that is
   * not visible yet, writes off a stale param, and either repairs a finished
   * turn from the probe payload or streams a live one.
   *
   * PR4 completes it (T:17798-17862): `matches` / partial-row stripping,
   * `neverShown`, and the follower's `quiet` / `onScreen` test — all of which
   * exist to reconcile a run this frame never started against turns history
   * already restored.
   */
  async function resumeRun(runId: string, opts: ResumeOptions = {}): Promise<void> {
    if (disposed) return;
    try {
      await resumeAttach(runId, opts);
    } finally {
      // THE ADOPTION GATE COMES DOWN ON EVERY ROAD OUT OF HERE (Bugbot PR
      // #1061). `openSession` raises `adopting` before the first frame, and on
      // the URL-`run` road NOTHING else lowers it: `adoptLiveRun` is skipped
      // (that is `resumeRun`'s job) and only a live `pollLoop`'s first poll
      // clears the flag. So every exit that never reaches the loop — a stale
      // param, a run that finished while the frame was away, a bail on the
      // send gate, a thrown probe — used to return with the gate still up,
      // and the transcript kept `is-settling` (`visibility: hidden`) for the
      // life of the page: a restored conversation, rendered and invisible.
      //
      // Idempotent and cheap: `setAdopting` only emits on a real change, so
      // the ordinary live road (where the first poll already cleared it) pays
      // nothing here.
      setAdopting(false);
      // AND THE CARET GOES BACK IN THE BOX, on every road out (T:17866 —
      // `focusBox(box)` in T's own `finally`). Boot's `?run=` was covered
      // incidentally by the composer's `autoFocus`; an adoption mid-session and
      // a scheduled attach were not, and a reader who was handed a streaming
      // reply then had to click to answer it (P4-17).
      if (!disposed) deps.focusComposer?.();
    }
  }

  async function resumeAttach(runId: string, opts: ResumeOptions): Promise<void> {
    if (sending) return;
    const neverShown = !!opts.neverShown;
    const quiet = !!opts.quiet;
    /**
     * OPT-IN, AND ONLY BOOT OPTS IN (T:17749, 17776-17786). PR1 retried
     * unconditionally, which is right for the case the retry exists for — "a
     * frame handed a run id by its EMBEDDER can boot before the freshly created
     * run dir is visible to the agent" — and wrong for the two roads that
     * arrive here with an id nobody typed: the standing watch and the schedule
     * poller. Those ids come from `live_run`, so a "not visible yet" answer is
     * not a race with a spawn, it is a run that has since been PRUNED — and
     * waiting it out cost 5 × 700 ms inside the `sending` gate, during which
     * the composer refuses a send and the watch cannot lap.
     *
     * Keyed on the flags those roads already carry (`quiet` from `adoptWatch`,
     * `neverShown` from the scheduled attach), so nothing new has to be
     * threaded and an explicit `retryUnknown` still wins either way. Boot's
     * `?run=` carries neither flag and keeps the retry (P4-18).
     */
    const retryUnknown = opts.retryUnknown ?? !(quiet || neverShown);
    sending = true;
    const seat = ++sendSeq;
    const gen = logGen;
    /** WHICH TRANSCRIPT THIS ATTACH IS WRITING INTO, for the `ownRunEndedAt`
     *  stamp below — the same guard `pollLoop` takes, for the same reason: the
     *  stamp is a fact about ROWS, so an attach whose conversation is gone has
     *  nothing to say about the one that replaced it. */
    const tGen = state.transcriptGen;
    // Past the gate this run is ours to TRY, which is not the same as ours to
    // have shown (batch review F1). The claim keeps every other road off the id
    // for the length of the attempt (Bugbot PR #1075) and is released in the
    // `finally` below, so an attempt that ends without attaching — a thrown
    // probe, a stale id — leaves the run adoptable on the watch's next lap.
    // Tagged with this attach's seat so only this attach can let it go.
    if (runId) claimingRuns.set(runId, seat);
    try {
      let probe = (await run(dir, "poll", { run_id: runId, file: FILE || "", native: "1" }, { key: null })) as
        | PollResponse
        | { error: string; done: true };
      if (logGen !== gen || disposed) return;
      // A frame handed a run id by its EMBEDDER can boot before the freshly
      // created run dir is visible to the agent — a race, not a stale bookmark.
      // A few short retries tell the two apart (T:17777-17787).
      for (
        let i = 0;
        retryUnknown &&
        i < UNKNOWN_RUN_RETRIES &&
        (probe as { error?: string }).error === "unknown run_id";
        i++
      ) {
        await sleep(UNKNOWN_RUN_RETRY_MS);
        probe = (await run(dir, "poll", { run_id: runId, file: FILE || "", native: "1" }, { key: null })) as
          | PollResponse
          | { error: string; done: true };
        if (logGen !== gen || disposed) return;
      }
      if (isUnknownRun((probe as { error?: string }).error)) {
        // Stale param — nothing to attach to (T:17788-17796). NOT `onRunEnded`:
        // no run of this frame's ended here, so the checkpoint chain is exactly
        // as fresh as it was. T's own branch calls `annResolveSent()` and
        // nothing else (T:17792 vs 17812-17814); the annotations still have to
        // be handed back, because a bookmarked mid-run URL is the one road on
        // which they would otherwise be stranded `sent` forever.
        clearRunParam();
        deps.onRunAbandoned?.();
        // A CARD ONLY FOR AN ID THE CALLER SUPPLIED (T:17787-17795, P4-05). The
        // card is right for a bookmarked mid-run URL — the reader put that id
        // there and is owed an answer about it. The same road is taken by the
        // standing watch and the schedule poller, whose ids come from
        // `live_run`: the run ends and its dir is pruned before the probe
        // lands, and a reader who touched NOTHING got "That turn is no longer
        // running" over a healthy transcript. T recovered from those in
        // silence, and the flags those roads already carry are the difference.
        if (!quiet && !neverShown) {
          reportTrouble(troubleOf("unknown-run", String((probe as { error?: string }).error)));
        }
        return;
      }
      // AND HERE THE RUN IS GENUINELY OURS (batch review F1). Past the stale-id
      // branch every road on either streams the turn through `pollLoop` or
      // repairs it from this payload, so the id is one this transcript shows
      // and the schedule poller must never re-attach to it. Marked at this one
      // point rather than in each of the branches below, so no reconciliation
      // road can be added later that forgets to.
      if (runId) shownRuns.add(runId);
      const poll = probe as PollResponse;
      if (poll.session_id) noteSessionId(String(poll.session_id));
      const probeMsg = stripBlocks(poll.message || "");
      const users = state.turns.filter((t): t is UserTurn => t.role === "user");
      const lastUser = users.length ? users[users.length - 1] : null;
      /**
       * IS THE LAST BUBBLE THIS RUN'S OWN LINE? (T:17809-17810.)
       *
       * A marker-only message never counts as a match: every such send collapses
       * to the same text, so it cannot identify a specific turn — and a false
       * match trims another turn's assistant rows. Worst case for refusing is a
       * duplicated marker row, which is harmless by comparison.
       *
       * `neverShown` first: with it, identical text is a COINCIDENCE (the same
       * prompt sent twice), never this run's own line, so no match is legitimate.
       */
      const matches =
        !neverShown && !!lastUser && !isMarkerOnly(probeMsg) && lastUser.text === probeMsg;
      /**
       * `matches` asks the question of the LAST bubble only, because it also
       * decides whether to STRIP rows. `onScreen` only decides whether to PRINT,
       * so it may look at the whole transcript — and a message the user sent
       * twice reads as already-shown and prints nothing, which is the safe
       * direction and the same coincidence `matches` refuses to resolve
       * (T:17765-17766).
       */
      const onScreen = (msg: string) => !!msg && users.some((u) => u.text === msg);
      /**
       * IS THIS RUN'S TURN NEWS TO THIS TRANSCRIPT? (Bugbot PR #1075, second
       * pass.)
       *
       * `neverShown` is the CALLER'S BELIEF, not a fact about the log: the
       * schedule poller sets it for any id missing from `shownRuns`, and the
       * standing watch's own `refreshHistory` can have pulled that very turn in
       * from the transcript without ever touching `shownRuns` — `history` rows
       * carry no run id (`HistoryUserTurn` is text + uuid), so there is nothing
       * for a refresh to record. A short scheduled run therefore lands on
       * screen at the 5 s refresh and is attached again at the 15 s tick.
       *
       * So `neverShown` takes the SAME `onScreen` test the `quiet` follower
       * takes: append only what the transcript is not already showing. The two
       * flags differ in what they claim (a turn this frame never rendered vs. a
       * turn made in another tab), never in that question.
       */
      const unseen = (neverShown || quiet) && !!probeMsg && !onScreen(probeMsg);
      /** Already on screen and the caller is one of the two that must not
       *  double it up: print no USER LINE at all (the failure below is a
       *  separate question — see `errorShown`). */
      const shownAlready = (neverShown || quiet) && onScreen(probeMsg);
      /**
       * IS THIS FAILURE ALREADY IN THE TRANSCRIPT? (Bugbot PR #1075, third
       * pass.)
       *
       * The prompt and the failure arrive on screen by DIFFERENT roads, so
       * "the turn is already shown" cannot answer for both: `refreshHistory`
       * renders whatever rows the transcript file holds, while `poll.error` is
       * the RUN DIR's verdict — a CLI that died before it could write a row
       * leaves the prompt on screen and no failure anywhere. Asking the log
       * for the error text itself is therefore the only test that tells the
       * two apart, and it is exact: `historyToTurns` maps a transcript
       * `error` row to `text` verbatim, the same string `addError` would
       * classify.
       */
      const errorShown = (msg: string) =>
        !!msg && state.turns.some((t) => t.role === "error" && t.text === msg);
      /** Drop the partial assistant rows under a matched user line: `pollLoop`
       *  re-streams the whole turn, and the done branch re-renders it from the
       *  probe payload (T:17831 / 17857). */
      const stripAfterLastUser = () => {
        if (!lastUser) return;
        const cut = state.turns.findIndex((t) => t.key === lastUser.key);
        if (cut >= 0 && cut < state.turns.length - 1) {
          emit({ turns: state.turns.slice(0, cut + 1) });
        }
      };
      /** The assistant rows the probe payload carries. A `poll` response holds
       *  the turn's SEGMENTS too, so a run that finished while this frame was
       *  away keeps its whole tool timeline rather than losing it until the next
       *  history restore (T:17827-17835). */
      const probeSpans = Array.isArray(poll.turn_breaks) ? poll.turn_breaks : [];
      const probeSegs = Array.isArray(poll.segments) ? poll.segments : [];
      const probeText = poll.text || "";
      const addAssistantFromProbe = () => {
        // SEAMS APPLY TO A REPAIR TOO. A run that absorbed a follow-up and then
        // finished with no frame attached is TWO replies in one window
        // (`turn_breaks`), and one `pollBody` over the lot merged them into a
        // single bubble — the same defect `pollLoop`'s own done branch already
        // slices for. Sliced HERE rather than at the call sites so every repair
        // road (matched, appended, watched) gets it the once.
        for (let j = 0; j <= probeSpans.length; j++) {
          const from = j === 0 ? { segments: 0, text: 0 } : probeSpans[j - 1]!;
          const to = j < probeSpans.length ? probeSpans[j]! : null;
          const view = pollBody(
            to ? probeSegs.slice(from.segments, to.segments) : probeSegs.slice(from.segments),
            to ? probeText.slice(from.text, to.text) : probeText.slice(from.text),
            ++cardSeq,
          );
          if (view.mode === "empty") continue;
          pushTurn({
            role: "assistant",
            key: nextKey("a"),
            text: view.mode === "segments" ? view.view.tailText || "" : view.text,
            ...(view.mode === "segments" ? { segments: view.view.rows.map((r) => r.seg) } : {}),
            ...(j > 0 ? { followup: j } : {}),
          });
        }
      };
      if (poll.done) {
        // Run finished with no frame attached: repair only what the restored
        // transcript can provably be missing (T:17812-17855).
        clearRunParam();
        /**
         * AND THE REPAIR IS A TURN BOUNDARY THIS FRAME OWNS (Bugbot 3975677791).
         *
         * `pollLoop`'s `finally` stamps `ownRunEndedAt` and this road never did
         * — so a run reconciled here left the watermark at whatever it was
         * before (0 on a fresh mount), and `followDecision`'s own-echo guard
         * (`probe.mtime > ownEnd`) then read the rows THIS repair had just
         * accounted for as somebody else's turn and raised the external working
         * line over them. Same guard as `pollLoop`'s, and stamped on every exit
         * from this branch: the error roads append rows too, and a repair that
         * reconciled to "already on screen" has still just established that the
         * run is over.
         */
        const stampOwnEnd = () => {
          if (logGen === gen && state.transcriptGen === tGen) emit({ ownRunEndedAt: now() });
        };
        // The run this frame missed still handled its notes, and may have edited
        // the file while we were away (`annResolveSent` / `snapInvalidate`).
        deps.onRunEnded?.();
        /**
         * DID THIS REPAIR ACTUALLY PUT ROWS ON SCREEN? (Bugbot 3974939169 and
         * 3974975062, batch review F9.)
         *
         * The nonce below is an UNCONDITIONAL scroll-to-bottom in the renderer,
         * and it was bumped on one road only — the success one, whether or not
         * that road appended anything. Both halves of that were wrong:
         *
         *  * the error branches append a user line and a failure in one commit
         *    and then returned WITHOUT bumping, so a reader who had scrolled up
         *    never saw a failed scheduled or adopted turn land at all;
         *  * the success branch bumped even when `matches` and `unseen` were
         *    both false and nothing was added — the `shownAlready` case, or a
         *    probe with no `probeMsg`. Native reaches that on roads T does not
         *    (the quiet standing watch after a 5 s `refreshHistory` has already
         *    drawn the turn), so a reader who had scrolled up was yanked to the
         *    bottom for no new content.
         *
         * So the flag, not the road: every branch that appends says so, and the
         * nonce is bumped exactly when there is something to scroll to. T's own
         * call is equally unconditional (T:17851) but cannot reach the no-op
         * case, so this is parity with its behaviour rather than its spelling.
         */
        let appended = false;
        // A LIMIT HIT REPAIRED OFF-FRAME IS STILL A LIMIT HIT (Bugbot #1107):
        // the same card with the reset time, the same scheduled comeback. The
        // three branches below only decide whether a user line is needed
        // first; the error itself goes through here.
        const probeQuota = poll.quota;
        const reportProbeError = (message: string) => {
          if (limitHit(probeQuota) && scheduleComeback(runId, message, probeQuota)) return;
          addError(message);
        };
        if (poll.error) {
          if (matches || !users.length) {
            reportProbeError(poll.error);
            appended = true;
          } else if (unseen) {
            // The turn is not on screen and never was, so the failure needs its
            // own user line to hang under — otherwise the error reads as
            // belonging to whatever the reader last said.
            //
            // `quiet && !onScreen` IS THE SAME CASE AS THE SUCCESS PATH BELOW
            // (Bugbot PR #1075): the standing watch adopts `quiet: true`, so a
            // turn made in another tab that FAILED took neither this branch nor
            // that one and was discarded outright — while its succeeding twin
            // was appended. A failed turn is news in exactly the same way.
            //
            // Gated on `probeMsg` (inside `unseen`), mirroring the success
            // branch: the message is the whole of the evidence about what this
            // transcript is already showing, so with none there is no turn to
            // append and neither flag has anything to be quiet about.
            addUser(probeMsg);
            reportProbeError(poll.error);
            appended = true;
          } else if (shownAlready && !errorShown(poll.error)) {
            // THE PROMPT IS UP AND THE FAILURE IS NOT (Bugbot PR #1075, third
            // pass). `unseen` above asks whether the TURN is news, and it is
            // the wrong question for a failed run: the two-clock race this
            // code already guards for successful turns — the 5 s
            // `refreshHistory` pulls the prompt in from the transcript, the
            // 15 s watch then attaches the same id — leaves a scheduled or
            // adopted run's error with nowhere to go, because the turn no
            // longer counts as unseen. So the failure is printed on its own,
            // under the line that is already there, and only `errorShown`
            // stops it: if the transcript happened to carry the failure too,
            // the row is there and there is nothing to add.
            reportProbeError(poll.error);
            appended = true;
          }
          // A FAILED TURN IS NEWS TOO (Bugbot 3974939169): same nonce, same
          // reason — one commit, no `running` edge to hang a settle-scroll off.
          if (appended) emit({ repaired: state.repaired + 1 });
          stampOwnEnd();
          return;
        }
        if (matches) {
          stripAfterLastUser();
          addAssistantFromProbe();
          appended = true;
        } else if ((!users.length && probeMsg) || unseen) {
          // Appended, never matched: the log is empty, or the turn is genuinely
          // `unseen` — a scheduled send that fired and finished between polls,
          // or a turn made in another tab this transcript has never shown. A
          // SHORT turn is over before the watch's first look, and dropping it
          // silently was the whole of the second tab's remaining complaint
          // (D415); appending one the refresh had already pulled in was the
          // duplicate on the other side of it (Bugbot PR #1075).
          addUser(probeMsg);
          addAssistantFromProbe();
          appended = true;
        }
        // AND THE WINDOW IS NOW ON SCREEN, so the next send's loop does not
        // type it a second time (see `landedWindow`). This is the reload road
        // into exactly the R4-3 shape: a finished run, repaired here, then a
        // follow-up into the host that is still holding it open.
        landedWindow = { runId, segments: probeSegs.length, text: probeText };
        // A REPAIR IS NEWS THAT HAS TO BE SCROLLED TO (T:17851, P4-10). T calls
        // `scrollBottom()` here unconditionally, and the reason it must be
        // unconditional is that a repair has no `running` → `idle` edge for the
        // settle-scroll to hang off: it appends a whole turn in one commit. So
        // a reader who had scrolled up — which is exactly the reader who came
        // back to a run that finished while the frame was away — saw nothing
        // appear at all. The renderer reads this nonce beside its own
        // follow-tail effect.
        //
        // Gated on `appended` (see above): a repair that reconciled to "already
        // on screen" has nothing to scroll TO, and the scroll would only be a
        // reader losing their place.
        if (appended) emit({ repaired: state.repaired + 1 });
        stampOwnEnd();
        return;
      }
      if (matches) {
        // In flight and this turn's user line is on screen: keep it, drop the
        // partial assistant rows.
        stripAfterLastUser();
      } else if (probeMsg && !shownAlready) {
        addUser(probeMsg);
      }
      sending = false; // pollLoop is not gated on it, and follow-ups need it free
      await pollLoop(runId, gen);
    } catch (err) {
      // A THROWN PROBE IS A TROUBLE CARD, not an unhandled rejection. Every
      // road in here is reached as a bare `void` (the boot's, `adoptWatch`'s,
      // the schedule watcher's), so without this a re-attach that failed — the
      // server gone between mount and the probe — died silently and the reader
      // was left looking at a restored transcript with no run and no
      // explanation. T warns in exactly this place (T:17862-17865); a card is
      // the native surface for it, and it supersedes the bare warn.
      //
      // ON THE ROADS NOBODY ASKED FOR, IT STAYS A WARN (P4-05, batch review
      // F2). Same predicate as the `unknown run_id` branch above, and for the
      // same reason: `quiet` (the standing watch) and `neverShown` (the
      // schedule poller) are ids that came from `live_run`, not from a reader.
      // A dropped socket, a server restart, a sleep/wake — any of which make
      // one `poll` reject — painted a trouble card over a healthy transcript
      // for somebody who touched nothing, which is the exact symptom P4-05
      // exists to remove. T only warns here (T:17862-17865), so a warn is also
      // the parity answer for those two roads.
      if (!disposed && logGen === gen) {
        if (!quiet && !neverShown) reportTrouble(troubleFromError(err));
        else console.warn("re-attach failed:", err instanceof Error ? err.message : err);
      }
    } finally {
      if (sendSeq === seat) sending = false;
      // THE CLAIM IS LET GO ON EVERY ROAD OUT (batch review F1). By here the id
      // is either in `shownRuns` (attached and reconciled) or genuinely free
      // again — a stale id, a thrown probe, a generation bump — and the watch's
      // next lap is entitled to try it.
      //
      // ...but only if it is still OURS (Bugbot 3975433059): a Back in the
      // middle of this attach cleared the claims, and the lap that followed may
      // already have taken a fresh claim on the same id.
      if (runId && claimingRuns.get(runId) === seat) claimingRuns.delete(runId);
    }
  }

  // ---- the transcript follower's two half-methods (PR4, D415) -------------

  /**
   * REFRESH MODE (T:17972-17987): re-ask what the conversation IS. No skeleton,
   * no scroll reset, no card wipe, no `session_id` write — the page does not
   * decide what is new, and `history` is the one party that knows where the turn
   * boundaries are.
   *
   * Gated on the same two facts every follower read is: a live run owns the
   * transcript, and a send in flight is about to add to it.
   */
  /** The transcript restore's transport: the host's in-process road when it
   *  gave one (`deps.history`, owner E2E R1, F5), else agent.py through
   *  `/api/run` — the tests' fake agent, and the pre-F5 behaviour. */
  const fetchHistoryVia = (sessionId: string): Promise<HistoryResponse & { error?: string }> =>
    deps.history
      ? deps.history(FILE || "", sessionId)
      : (run(
          dir,
          "history",
          { file: FILE || "", session_id: sessionId, native: "1" },
          { key: null },
        ) as Promise<HistoryResponse & { error?: string }>);

  async function refreshHistory(sessionId: string): Promise<void> {
    if (disposed || !sessionId) return;
    if (activeRun || sending) return;
    const gen = logGen;
    // WHAT THE LOG LOOKED LIKE WHEN THIS WAS ASKED (Bugbot 3977975835). A
    // scheduled or adopted repair that starts AND finishes inside the round
    // trip leaves `activeRun`/`sending` clear again — but it has drawn rows
    // this payload predates, and stamped `ownRunEndedAt` doing so. Either
    // stamp moving means the answer is about an older conversation than the
    // one on screen: drop it, the watch will ask again.
    const endBefore = state.ownRunEndedAt;
    const tGen = state.transcriptGen;
    try {
      const res = await fetchHistoryVia(sessionId);
      if (logGen !== gen || disposed) return;
      // A run that attached across the await owns the log now; its stream is
      // fresher than this answer.
      if (activeRun || sending) return;
      if (state.ownRunEndedAt !== endBefore || state.transcriptGen !== tGen) return;
      if (res.error) throw new Error(res.error);
      deps.historyCache?.set(FILE || "", sessionId, res);
      // NOTHING IS RECORDED IN `shownRuns` HERE: a `history` row is text plus a
      // transcript `uuid` (`HistoryUserTurn`) and carries no run id, so a
      // refresh cannot say WHICH runs it just rendered. The duplicate-attach
      // guard therefore lives on the other side, in `resumeAttach`'s `unseen`
      // test, which asks the rendered transcript itself (Bugbot PR #1075).
      //
      // THE WATERMARK IS WRITTEN ONLY WHEN THE ANSWER CARRIES ONE
      // (T:17663-17665 `noteTranscript`): a history answer with no stat leaves
      // the mark exactly as it rendered. Publish `null` instead and
      // `followTranscript` bails at "no render to compare against" for the rest
      // of the session — the standing watch goes deaf on the very refresh it
      // just performed.
      emit({
        turns: historyToTurns(res),
        ...(res.transcript && res.transcript.path ? { transcript: res.transcript } : {}),
      });
    } catch (err) {
      // The transcript stays exactly as it rendered. The watermark is NOT
      // advanced, so the next lap tries again.
      if (typeof console !== "undefined") {
        console.warn("history refresh failed:", err instanceof Error ? err.message : err);
      }
    }
  }

  /** T:17715-17732 `setExternalWorking`. Idempotent, and it never speaks over a
   *  run this frame owns: that line is the real one, with a stop button and a
   *  token count this one cannot honestly offer. */
  function setExternalWorking(on: boolean): void {
    if (disposed) return;
    if (on) {
      if (activeRun || sending) return;
      if (state.working && state.working.external) return;
      workingStartedAt = now();
      setStats(0, "external", null, null, true);
      return;
    }
    if (state.working && state.working.external) emit({ working: null });
  }

  // ---- back to home (T:12972-13077) --------------------------------------

  function newChat(): void {
    // The back button bumps the generation: a streaming pollLoop that wakes to a
    // bumped number bails before touching the transcript or the URL params. The
    // run itself keeps going server-side (Akshil, 2026-08-19 — Back is live
    // mid-turn) and re-attaches from the session list.
    logGen++;
    sending = false; // the button releases the gate
    activeRun = null;
    activeSeat = 0;
    activeTurnKey = null;
    permCards.clear();
    restoredSid = ""; // the cache key leaves with the transcript (Bugbot, PR #1112)
    notedSkills.clear();
    answeredStates.clear();
    notedStates.clear();
    nullStatePolls.clear();
    // THE TRANSCRIPT IS GONE, SO NOTHING IS ON SCREEN ANY MORE (Bugbot
    // 3974975055 — see `shownRuns`). Back mid-turn is the gesture this whole
    // feature is built around: the run keeps going server-side and the session
    // list is meant to re-attach to it, which `adoptWatch` refused while the id
    // was still recorded here from `pollLoop`.
    shownRuns.clear();
    // And the CLAIMS with them (Bugbot 3975433059): an attach whose transcript
    // has been replaced is abandoned, and a claim it never gets to release
    // would keep its run unadoptable for the rest of the page.
    claimingRuns.clear();
    clearQueued();
    // Neither of these is a true default worth stamping — a session id is an
    // identifier and `run` is in-flight bookkeeping — so absent stays the
    // spelling for "none" (T:13031-13036). The DIFFERENCE between the two is in
    // the history knob, and it is expressed in code rather than only in prose:
    // disowning a session id is the reader navigating home (T:13031's bare set),
    // where clearing a REAL run id must not buy the entry (T:13036).
    setParam({ session_id: null });
    setParam({ run: null }, "replace");
    // `rev` is documented monotonic and offered as a cheap memo key
    // (controller-api.ts:161), so a fresh transcript carries the old number
    // forward rather than jumping back to 1.
    const carried = state.rev;
    state = { ...emptyState(FILE), rev: carried };
    emit({});
  }

  return {
    getState: () => state,
    subscribe(cb) {
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
      };
    },
    sendMessage,
    sendFollowUp,
    postOptimisticUser,
    dropOptimisticUser,
    stopRun,
    decidePermission,
    answerQuestion,
    decidePlan,
    dismissCard,
    answerAppState,
    openSession,
    resumeRun,
    adoptLiveRun,
    refreshHistory,
    setExternalWorking,
    addNote: (text: string, glyph: NoteTurn["glyph"] = "\u25f7") => {
      addNote(text, glyph);
    },
    isBusy: () => !!activeRun || sending,
    hasActiveRun: () => !!activeRun,
    /** Shown OR being claimed: the schedule poller's question is "may I attach
     *  to this?", and an attach already in flight is as good an answer as a
     *  turn already on screen (see `claimingRuns`). */
    hasShownRun: (runId: string) => shownRuns.has(runId) || claimingRuns.has(runId),
    newChat,
    settleAttachments,
    dispose() {
      disposed = true;
      logGen++;
      // The in-flight poll and its `sleep(400)` would otherwise outlive the
      // unmount by up to one lap; `agent.ts` takes the signal.
      life?.abort();
      life = null;
      listeners.clear();
    },
  };
}
