// THE ROOT of the native chat: the token-scoped `.chat-root` box, the layout
// variants, the boot branch, and the wiring between `protocol/run-controller`
// and everything in `ui/` and `pane/`.
//
// Sources: T's boot IIFE (T:19178-19296) for the branch and its 1.5 s ask race,
// T:16336 for the end-of-run `typer.finish` → `attachCodeCopy` ordering,
// T:15947-15978 for Escape, T:14652-14663 for the narrow hard block, and
// inventories 00 §1e / 01 §A+§G / 04 §F / 05 §G. Nothing here talks to the
// shell: hosts hand facts down as props and get hops back through `onNavigate`.
//
// WHAT THE IFRAME USED TO BUY, and where each of those now lives (00 §1e):
//   * param isolation   → `params/store.ts` (a memory store per mount)
//   * its own scroller  → `.chat-root` is a full-height flex column and
//                         `Transcript`'s `.chat-logwrap` is the one scroller
//                         (the host cards are `overflow: hidden`)
//   * focus             → `autoFocus`, threaded to the composer's textarea
//   * CSS cuts          → `styles/chat.css`'s `.chat-compact` / `.chat-only` /
//                         `.chat-peek` blocks
//   * the ready signal  → `onReady`, which replaces `dataset.chatReady`
//   * the activity poke → `onActivity`: the controller announces on THIS
//                         document, `stampChatActivity` tells every other one
//                         (T:16435)
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type MutableRefObject,
} from "react";

import { currentUrl, navigateUrl } from "@platform/lib/router";

import { createUrlParamsStore, type ParamsStore } from "./params/store";
import { useChatParam } from "./params/useChatParams";
import { resolveAgentDir } from "./protocol/agent";
import { fetchHistory, sharedHistoryCache } from "./protocol/history";
import type { SendOptions, UserTurn } from "./protocol/controller-api";
import { watchStreamTeardown, watchTopOrigin } from "./shots";
import type { Attachment, Receipt } from "./shots/types";
import { enhanceCodeBlocks } from "./protocol/markdown";
import { createChatController } from "./protocol/run-controller";
import { finishedTailText, parseTailKey, streamingTailOf } from "./protocol/segments";
import { createTyper, type Typer } from "./protocol/typer";
import { createFrameClock } from "./ui/frameClock";
import {
  AnnBar,
  AnnChips,
  barFit,
  AnnPins,
  AnnPopover,
  createRecorder,
  isSendableNow,
  NAV_LOCKED_REASON,
  pathOf,
  recClockText,
  RecControls,
  transcribe,
  useAnnotations,
  walkthroughOwns,
  warmTranscriber,
  type AnnAnchor,
  type AnnBarHandlers,
  type AnnotationsApi,
  type AnnRecorder,
  type Annotation,
  type RecAnchor,
  type RecAnnotation,
  type Recorder,
} from "./ann";
import { captureAudio, captureSources } from "@platform/lib/capture-audio";
import { composeOutgoing, formatAnnotations, type AnnotationWire } from "./protocol/wire";
import { getStream, isNativeOff, noteSourcesProbe, shotsDir } from "./shots";
import {
  CHAT_FRAME_FALLBACK_MS,
  ChatFramePlaceholder,
} from "@platform/ui/ChatFrame";
import {
  AppPane,
  createAppStateWatcher,
  footnoteFor,
  homePlaceholderFor,
  LeftModePicker,
  pickerHost,
  SplitDivider,
  useAppStateResponder,
  useNarrowView,
  usePaneState,
  useSplit,
  ViewToggle,
  type PaneNoun,
  type PaneSrcFlags,
} from "./pane";
import {
  AnnStrip,
  ArtStrip,
  AttachTray,
  Kebab,
  CardPolicyProvider,
  Composer,
  createCardPolicy,
  Home,
  openCardIds,
  resetCardPolicy,
  liveViewable,
  RecapFold,
  SchedBlock,
  SentPop,
  settleReceipts,
  ShotViewer,
  Topbar,
  Transcript,
  NO_TARGET_SAID,
  TroubleView,
  ATTACH_API,
  mergeSendOptions,
  sendBlocks,
  useArtStrip,
  useAttachments,
  useAwayRecap,
  useFitStrip,
  useComposerDefaults,
  useRecentSessions,
  useRepairScroll,
  useTaskId,
  type TranscriptTail,
  type Viewable,
} from "./ui";
import { recapAnchor } from "./protocol/recap";
import { queueEnabled, useChatRecapEnabled, useProjectQueueEnabled } from "./feature-flag";
import { WaitingCard, WaitingRow } from "./ui/Waiting";
import { copyToTaskShots } from "./ui/SchedButton";
import type { SchedAttachment } from "./ui/sched-draft";
import { troubleFromError } from "./protocol/trouble";
import { useSchedule } from "./sched/useSchedule";
import type { QueueFacts } from "@platform/lib/queue";
import { PENDING_KEY_PREFIX, QUEUED_PARAM } from "@platform/lib/queue";
import { usageLimitStatusWord } from "@platform/lib/usage-limit";
import {
  NO_DROPPED,
  pruneDropped,
  useLiveSeeds,
  headerTaskId,
  waitingFacts,
  waitingRows,
} from "./sched/waiting";
import type { WaitingSeed } from "./sched/waiting";
import { leaderSession, useQueuedLeader } from "./sched/queue-leader";
import { mergeWaitingChats, useWaitingChats } from "./sched/waiting-chats";
import { inboxBubbles } from "./protocol/inbox";
import { createLiveWatch } from "./live/watch";
import {
  admitQueueSend,
  cancelScheduledMessage,
  getClaudeSessionLiveness,
  skipQueue,
} from "@platform/lib/api";
import "./styles/ann.css";
import "./styles/chat.css";
import "./styles/hljs.css";

/** The "Fix with AI" prompt text a host hands over (explorer claude-ask.ts). */
export type ClaudeAsk = string;

/** "url" = the shell URL (sidebar / content / canvas); a store = in-memory
 *  (cards / peek / panel / tab). design.md §2. */
export type ChatParamsSource = "url" | ParamsStore;

/** T:19176 `ASK_DETECTION_TIMEOUT_MS` — the bound on the ask branch's wait for
 *  model/effort detection. Comfortably longer than the warm-venv case this wait
 *  exists for, short enough that a cold one still sends before the reader
 *  wonders whether anything happened. */
export const ASK_DETECTION_TIMEOUT_MS = 1500;

/** T:16435 — the cross-document poke. `storage` fires only on a CHANGED value
 *  and never in the writing document, which is exactly the shape needed; the
 *  clock plus a nonce means two panes stamping in the same millisecond cannot
 *  swallow each other's poke. */
export const CHAT_ACTIVITY_KEY = "fused-render:chat-activity";

/** How often the HOST's app-state frame is looked up again (`annotateTarget`).
 *  The mark moves with the host's own mode switcher and goes when it shows a
 *  listing, so this is a live fact and not a boot one — T re-reads it on the
 *  same kind of tick (`annPollTarget`). Fast enough that the mark is in hand
 *  long before the reader's first message, cheap enough to be a `querySelector`
 *  twice a second. */
export const HOST_PANE_POLL_MS = 500;

export interface ClaudeChatProps {
  /** `_file`: folder | file | null. */
  file: string | null;
  /** `chat_only=1` → no left pane. */
  chatOnly: boolean;
  /** `compact=1` (cards wall). */
  compact: boolean;
  /** `peek=1` (TaskPeek modal). */
  peek: boolean;
  params: ChatParamsSource;
  initialSessionId?: string;
  initialRunId?: string;
  initialAsk?: ClaudeAsk;
  /** `!IS_PREVIEW && !_nofocus`. */
  autoFocus: boolean;
  /**
   * Replaces the `parent.document` lookup for the annotate target (T:6117).
   *
   * It is the APP-STATE frame as well as the annotation one, and in the hosted
   * `?_side=claude` layout it is the ONLY one — see `hostFrame` in the body.
   * PR3 hangs the notes off the same getter.
   */
  annotateTarget?: () => HTMLIFrameElement | null;
  /** `_remote=1`. */
  remote?: boolean;
  /** `_preview=1` — this mount is a thumbnail, so the pane's own render is too. */
  preview?: boolean;
  /** `_noopen=1` — this mount must not record the app it frames as OPENED
   *  (D622, the listing pane). Unlike `preview` it is not display-only: the
   *  pane is fully interactive, so it carries the no-open stamp WITHOUT the
   *  thumbnail one that would disable `fused.daemon.*` for the framed app. */
  noOpen?: boolean;
  /** Replaces `window.top.location` hops (T:12029, 17054, 18222). */
  onNavigate?: (url: string) => void;
  /** Replaces `dataset.chatReady`: fired ONCE, when the view the boot branch
   *  chose has something on screen (T:4520 `markChatReady`). */
  onReady?: () => void;
  /**
   * Escape with nothing of the chat's own open. TaskPeek's close, which used to
   * be a listener the host attached INSIDE the frame's document
   * (TaskCards.tsx:711-733). Never a stop: Escape has no claim on a run
   * (T:15947-15978, and `escapeAction`'s own header).
   */
  onEscape?: () => void;
  /** The composer's textarea, for a host modal's `initialFocus` (TaskPeek's
   *  `Modal initialFocus`, which used to be the iframe element). */
  focusRef?: MutableRefObject<HTMLTextAreaElement | null>;
  /**
   * "WHILE YOU WERE AWAY", OPT-IN — and default OFF is the whole point.
   *
   * A recap is a ~12s model call fired by window `focus`, which every mounted
   * chat on the page hears. The tasks wall mounts one chat PER CARD, the
   * canvases workspace mounts its editor, the explorer mounts the one the
   * reader actually opened: a single return spent seven model calls, six of
   * them for folds inside cards nobody was looking at.
   *
   * So the recap belongs to the PRIMARY chat — the full one the reader opened —
   * and the host has to say so. Off by default means a new embed site cannot
   * inherit the cost by forgetting to think about it, which is the opposite of
   * how this bug arrived. (.claude-design/session-recap.md, "Trigger".)
   */
  recap?: boolean;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** T:16435 `noteChatActivity` — best-effort: a blocked store (private mode, a
 *  locked-down webview) costs the poke, never the turn. */
function stampChatActivity(): void {
  try {
    localStorage.setItem(
      CHAT_ACTIVITY_KEY,
      Date.now() + ":" + Math.random().toString(36).slice(2),
    );
  } catch {
    // The shell's 20-30 s task polls remain the fallback.
  }
}

function variantOf(p: Pick<ClaudeChatProps, "compact" | "peek" | "chatOnly">): string {
  return p.compact ? "compact" : p.peek ? "peek" : p.chatOnly ? "chat-only" : "split";
}

/** The host cuts are CLASSES and not a teardown, exactly as in T (T:1405-1438):
 *  compact / chat-only / peek are host FACTS, decided when the mount was built. */
function rootClass(
  p: Pick<ClaudeChatProps, "compact" | "peek" | "chatOnly">,
  extra?: string,
): string {
  return [
    "chat-root",
    p.compact ? "chat-compact" : "",
    p.chatOnly ? "chat-only" : "",
    p.peek ? "chat-peek" : "",
    extra ?? "",
  ]
    .filter(Boolean)
    .join(" ");
}

/**
 * TEST-ONLY WINDOW ONTO THE SEND BOOKKEEPING (`inFlight` below).
 *
 * The delete-on-success is invisible from outside: nothing on screen changes
 * either way, the map merely keeps every `Receipt[]` and every `Attachment` the
 * page has ever sent — with their blob URLs — alive for as long as the chat is
 * open. A leak with no symptom needs a seam or it has no test, so the most
 * recently mounted chat parks its map here from an effect (`resetAgentDirCacheForTests`
 * in protocol/agent.ts is the same idiom).
 */
let inFlightForTests: Map<Receipt[], Attachment[]> | null = null;

/** How many sends are still holding their pictures. Tests only. */
export function inFlightSizeForTests(): number {
  return inFlightForTests ? inFlightForTests.size : 0;
}

/**
 * TEST-ONLY WINDOW ONTO THE ANNOTATION SUBSYSTEM (`ann`, below), the same idiom
 * `inFlightForTests` is and for the same kind of reason.
 *
 * A note is MADE by a click inside the framed app — six listeners in a document
 * `react-test-renderer` does not have — so the seams this file owns (the strip's
 * arm, the chip row, the send's `<annotations>` block and its badged overview,
 * Escape's discard, `enterNoPane`) had no way to be driven from a test at all.
 * They are integration wiring: the pieces each have their own suite, and what is
 * left untested is precisely how this file joins them.
 *
 * `ann/*`'s own suites cover the making of a note; this hands the coordinator
 * over so a test can make one and then assert on what THIS file did with it.
 */
let annForTests: AnnotationsApi | null = null;

export function annotationsForTests(): AnnotationsApi | null {
  return annForTests;
}

/**
 * Resolve the two things every hook below needs — the claude template's folder
 * and the param store — before the chat itself mounts, so the body's hook list
 * never has to branch. A null `agentDir` is the one hard stop: with no `agent.py`
 * there is nothing to talk to, and saying so is better than an inert composer
 * (T:4653-4656 replaced the body for the same reason).
 */
export function ClaudeChat(props: ClaudeChatProps) {
  const { file } = props;
  const [agentDir, setAgentDir] = useState<string | null | undefined>(undefined);

  useEffect(() => {
    if (!file) {
      setAgentDir(null);
      return;
    }
    let live = true;
    setAgentDir(undefined);
    // AND A BACKSTOP, which legacy had as `CHAT_FRAME_FALLBACK_MS` (8 s) on the
    // frame's own cover: a `statPath` that never settles — a stalled server, a
    // request the browser never answers — left the box blank FOR EVER, with no
    // road to the `TroubleView` branch below that exists to explain exactly
    // this. Losing the race resolves to `null`, which is that branch.
    //
    // The same 8 s, and the same constant, so the two waits cannot drift apart.
    const backstop = setTimeout(() => {
      if (live) setAgentDir(null);
    }, CHAT_FRAME_FALLBACK_MS);
    void resolveAgentDir(file).then((dir) => {
      if (live) {
        clearTimeout(backstop);
        setAgentDir(dir);
      }
    });
    return () => {
      live = false;
      clearTimeout(backstop);
    };
  }, [file]);

  // ONE store per mount. A card's params must not survive its remount —
  // `key={cardKey(task)}` is the host's remount rule (TaskCards.tsx:339) — and
  // the URL store is a singleton per mount for the same reason: it installs
  // window listeners and holds a coalescing timer that has to go with it.
  // A LAZY STATE INITIALIZER, not the render body: the URL store installs
  // window listeners and holds a coalescing timer, and constructing it where a
  // render React discards (StrictMode, a concurrent interruption, a Suspense
  // retry) can throw the instance away leaks those listeners for good — the
  // `[]`-dep cleanup only ever sees the last one. And the constructor binds
  // NOTHING — the store binds on demand (its first subscriber, or the `attach()`
  // the effect below makes) — so a store this initializer builds and React
  // throws away costs nothing at all. The effect owns attach/detach, and
  // `attach()` is idempotent so a StrictMode remount re-arms a store it has
  // already disposed rather than going deaf to `fused:urlchange`/`popstate`.
  const [urlStore] = useState(() =>
    props.params === "url" ? createUrlParamsStore() : null,
  );
  useEffect(() => {
    if (!urlStore) return;
    urlStore.attach();
    return () => urlStore.dispose();
  }, [urlStore]);
  const params: ParamsStore = props.params === "url" ? urlStore! : props.params;

  if (agentDir === undefined) {
    // THE TEMPLATE LOOKUP IS IN FLIGHT, and this branch used to be an EMPTY BOX
    // on the argument that "the host is still holding its own cover over this
    // box (ChatFrame's skeleton)". That is true flag-OFF, where the host really
    // does frame a booting document — but flag-on there is no frame and no
    // cover: `ChatMount`'s `Suspense` fallback covers only the CHUNK LOAD, and
    // it has already resolved by the time this component is running its own
    // stat. So a first mount for a folder drew a bare `.chat-root` on the host
    // background for the length of one `/api/fs/stat`, and a cold cards wall of
    // six drew six empty tiles where legacy drew six skeletons.
    //
    // `placeholderFor`'s node — the SAME `ChatFramePlaceholder` that
    // `Suspense` shows and that `ChatFrame` holds over a booting frame — so the
    // two waits look like one wait, which is what 00 §1e's "one wait, one look"
    // actually asks for. The reader sees the chunk's skeleton become the stat's
    // skeleton with no flash of an empty box between them.
    return <ChatFramePlaceholder className={rootClass(props)} />;
  }
  if (agentDir === null) {
    return (
      <div className={rootClass(props)} data-variant={variantOf(props)}>
        <div className="chat-logwrap">
          <div className="chat-log">
            {/* TWO PLAIN SENTENCES AND NOTHING ELSE (P3R1-8, owner
                2026-09-10). This branch is reached two ways — no target at
                all, and a folder whose template never resolved, the 8 s
                backstop above included — and it used to say "Something went
                wrong" over a monospace box reading "There is no claude
                template for this folder.": a title that says nothing, a
                sentence naming an internal thing the reader cannot have an
                opinion about, and a claim that is simply false when what
                actually happened is that `/api/fs/stat` never answered. The
                copy lives in `ui/TroubleView`'s table with every other trouble
                sentence; `message: ""` is what takes the verbatim box away,
                because there are no machine words behind this failure to
                quote — and the Copy buttons no longer invent any either
                (R1-3): `troubleReport` used to print `Error:` over
                "(no message)", so this card's clipboard handed on the exact
                string the screen had just stopped saying. It now carries what
                it actually knows — what the app was doing, and where to read
                more.

                AND THE ACTION THE COPY NAMES IS A BUTTON (R1-4). The sentence
                asks the reader to reload the page; without `onRetry` the card
                drew no button at all, so it named an action it did not offer
                (`main.tsx`'s boot card, the other one, has always passed one).
                Only when there IS a target: "there's nothing to open a chat
                on" is answered by opening a file, and a Reload button under
                that sentence would be a door back to the same empty room. */}
            <TroubleView
              trouble={{ kind: "boot", message: "" }}
              {...(file
                ? { onRetry: () => location.reload(), retryLabel: "Reload the page" }
                : { said: NO_TARGET_SAID })}
              what={file ? "opening the chat on " + file : "opening the chat"}
            />
          </div>
        </div>
      </div>
    );
  }
  return <ChatBody {...props} agentDir={agentDir} params={params} />;
}

interface ChatBodyProps extends ClaudeChatProps {
  agentDir: string;
  params: ParamsStore;
}

function ChatBody(props: ChatBodyProps) {
  const { agentDir, params, file, chatOnly, compact, peek } = props;
  const rootRef = useRef<HTMLDivElement | null>(null);
  const columnRef = useRef<HTMLDivElement | null>(null);
  const ownBox = useRef<HTMLTextAreaElement | null>(null);
  const boxRef = props.focusRef ?? ownBox;
  const onNavigate = props.onNavigate ?? navigateUrl;

  // ── the pane, and everything that reads off it ─────────────────────────────
  const paneFrame = useRef<HTMLIFrameElement | null>(null);
  // STABLE: `AppPane`'s own `setFrame` is a `useCallback` keyed on this, so an
  // inline arrow made React detach and re-attach the iframe's callback ref on
  // every render — every 400 ms poll tick — transiently nulling the element the
  // app-state watcher reads.
  const setPaneFrame = useCallback((el: HTMLIFrameElement | null) => {
    paneFrame.current = el;
  }, []);
  // THE HOST'S OWN PANE, and in the hosted layout it is THE pane (R3-5).
  //
  // `?_side=claude` frames this chat as a sidebar beside the shell's content
  // pane, so `chat_only` takes OUR column away — but the app is still on screen,
  // in the middle column, and that frame's document is the app's document. T has
  // exactly this seam: `annFrame` is `annMarkedFrame()` in CHAT_ONLY (T:6026),
  // and `appWindow()` — the one thing every app-state read goes through — reads
  // off `annFrame` whichever of the two it is (T:4865). So the sidebar's pushed
  // block, its `app_state` answers and its receipt all came from the host's
  // frame, and only OUR layout was gone.
  //
  // Natively that frame arrives as `annotateTarget`, a getter the host owns
  // (Preview.tsx looks its own mark up; CanvasWorkspace hands over its workbench
  // frame). Without it `hasPane` was false in the sidebar, no block was pushed,
  // `has_pane:"0"` took `mcp__fused_approvals__app_state` out of the run's
  // roster — and, because `agent.py`'s `_pane_file` reads the pane off the
  // LEADING app-state block, every chat had in that layout was recorded as a
  // FOLDER chat and vanished from the file's Recent list (R3-1/R3-3).
  //
  // A REF, and every call GUARDED: the getter is a host callback that may reach
  // across a document (`parent.document` throws cross-origin), and a chat must
  // never fail to send because a host's lookup did.
  const annotate = useRef(props.annotateTarget);
  annotate.current = props.annotateTarget;
  //
  // AND READABLE, OR IT IS NOT A PANE (Bugbot, PR #1061). The mark says "this
  // frame is the content the reader is looking at", not "its document is yours
  // to read" — the canvases workbench frames a CROSS-ORIGIN document. Counting
  // it made the first send claim `has_pane: 1`, which puts `app_state` on the
  // session's `--allowed-tools` for the WHOLE session with no way back, while
  // `blockForSend` could only ever answer "": the model told it could see an
  // app it cannot, and the session still recorded no pane. Unreadable answers
  // the same as absent, and every reader of the host's pane goes through here.
  const hostFrame = useCallback((): HTMLIFrameElement | null => {
    try {
      const frame = annotate.current?.() ?? null;
      if (!frame) return null;
      // A frame still loading answers with its own `about:blank` document,
      // which is readable and no reason to disown it — the watcher polls.
      if (!frame.contentDocument && !frame.contentWindow) return null;
      return frame;
    } catch {
      return null; // the getter threw, or the document is not ours to read
    }
  }, []);
  /** The frame whose document IS the app: ours when we have a pane, the host's
   *  marked one when we do not. Resolved on every call, never cached — the
   *  host's mark MOVES with its own mode switcher and goes when it shows a
   *  listing (T:6136 `annCapable` is a live fact, not a boot one). */
  const appFrame = useCallback(
    () => paneFrame.current ?? hostFrame(),
    [hostFrame],
  );
  /** The pane's noun, for the app-state block. A REF because `usePaneState` is
   *  called below this line and the watcher outlives every render — and because
   *  the noun RESOLVES late anyway (the `app.py` decision is a round trip), so
   *  even an ordering that allowed a value would be reading a stale one. The
   *  watcher asks at block time (`appState.ts:231`, `:533`), which is what makes
   *  a lazy read the right shape here. */
  const paneNounRef = useRef<PaneNoun>("preview");
  const [watcher] = useState(() =>
    createAppStateWatcher(appFrame, {
      // THE THREE OPTIONS THIS WAS ALWAYS MEANT TO CARRY. `createAppStateWatcher`
      // has taken them since it was written; the call site passed none, so all
      // three defaults were quietly in force.
      //
      // "app", not "preview", for an app folder (T:5352-5411, T:5160-5173): the
      // block was telling the model the user's running app was "the preview".
      paneNoun: () => paneNounRef.current,
      // THE OUTLINE'S NODES CARRY A `path` (T:4977-4990, T:5065-5082). The
      // block's own preamble tells the model `path` is the same anchorPath the
      // pins use (`appState.ts:549-551`) and no node had one — so a pin could
      // not be joined to an outline node, and D146's single-identifier promise
      // was broken from the outline side. `ann/geometry`'s `pathOf` is the
      // builder the pins themselves use, which is what makes the two the same
      // identifier rather than two spellings that happen to agree.
      pathOf,
      // AND THE OUTLINE GOES TO A FILE (T:5177-5218). Without a shots dir the
      // watcher keeps it inline and warns `app-state outline kept inline: no
      // screenshot directory` on EVERY send — so the CLI re-read the whole
      // outline on every later turn, which is the exact cost T:5177-5218 exists
      // to avoid.
      shotsDir: () => shotsDir(agentDir),
    }),
  );
  // The framed document's console stays the app's own once we are gone.
  useEffect(() => () => watcher.dispose(), [watcher]);
  // A thumbnail's pane must neither pull the keyboard nor be recorded as the
  // user opening the app — the shell pairs the two flags for the same reason
  // (Preview.tsx `thumbFlags`, D348).
  const flags = useMemo<PaneSrcFlags>(
    () => ({
      ...(props.preview ? { preview: true, noFocus: true } : {}),
      // D622: fully interactive, but not the user opening the app.
      ...(props.noOpen ? { noOpen: true } : {}),
    }),
    [props.preview, props.noOpen],
  );
  const noPaneFlag = useRef(false);
  /**
   * `enterNoPane`'s steps 1, 2 and 5 — the ANNOTATION half of the teardown, and
   * an ORDER rather than a set (AppPane's header spells out why). Steps 3 and 4
   * fall out of `noPane` here — `useNarrowView` answers `""` for a no-pane
   * target and the root's own `nopane` class carries the rest — and step 6 is
   * `AppPane`'s early return.
   *
   * Through `annRef`, because this object is built BEFORE the hook it drives:
   * the pane resolves the target the annotation layer points at, so the pane's
   * state has to exist first. `[]`-dep, so the teardown the pane closed over is
   * never a stale render's.
   */
  const annRef = useRef<AnnotationsApi | null>(null);
  const noPaneSteps = useMemo(
    () => ({
      clearAnnotations: () => annRef.current?.clearAnnotations(),
      renderAnn: () => annRef.current?.render(),
      annSetMode: (on: boolean) => annRef.current?.setMode(on),
      rescueComposer: () => annRef.current?.rescueComposer(),
    }),
    [],
  );
  const pane = usePaneState({
    file,
    chatOnly,
    agentDir,
    flags,
    initialLeftMode: params.get("leftmode"),
    noPaneFlag,
    watcher,
    noPaneSteps,
  });
  const narrowView = useNarrowView({
    params,
    noPane: pane.noPane,
    // MEASURE THIS CHAT'S BOX, not the window (FIX-12). Legacy's media query
    // was evaluated inside the chat's own iframe, so it answered about the
    // PANEL; a window-scoped query meant that at a 380px panel in a 1280px
    // window not one narrow rule fired. `.chat-root` is the box the iframe's
    // viewport used to be.
    boxRef: rootRef,
    // T:8940 — arriving in the narrow CHAT view disarms: the toggle that would
    // undo the mode is hidden there, and an armed mode behind a hidden toggle
    // keeps the frame's click swallower live over a document nobody can see.
    onArriveChat: () => annRef.current?.arriveNarrowChat(),
    onRemeasure: () => annRef.current?.remeasure(),
  });
  // The noun the app-state block reads (see `paneNounRef`'s own note). Written
  // on every render rather than in an effect: the watcher pulls it lazily at
  // BLOCK time, so what matters is that the ref is current whenever a send
  // happens, not that a commit has been observed.
  paneNounRef.current = pane.paneNoun;
  const split = useSplit({
    params,
    narrow: narrowView.narrow,
    noPane: pane.noPane,
    // Every frame of a divider drag moves the frame's box, and every pin is
    // placed against it (T:8849's resize, per tick).
    onDragTick: () => annRef.current?.remeasure(),
  });
  // `has_pane` is the PAGE's answer and is sent on every turn (T:16609). Read
  // through a ref so a pane resolving does not rebuild the controller.
  //
  // TRUE ONLY FOR A PANE THAT IS ACTUALLY THERE — `status === "ready"`, the one
  // status that means a decision resolved to a real `src`. "resolving" used to
  // count, which made the answer a GUESS: the first send of a chat-only mount,
  // or of any target whose stat had not landed yet, told the model it could see
  // the app when there was nothing to see. "error" does not count either — the
  // frame has been swapped for the message (usePaneState's catch).
  // IS THE HOST SHOWING SOMETHING MARKED — polled, because it is a live fact.
  // The mark rides the frame the shell is SHOWING (Preview.tsx `m === shown`),
  // so it appears when that frame paints, moves when the reader switches the
  // pane's mode, and goes when the host shows a listing instead. T polls it for
  // the same reason (`annPollTarget`); this is the app-state half of that.
  //
  // The interval only exists where a host offered a getter at all: a cards tile,
  // a peek modal and the listing pane pass none, and a timer per mount on a
  // six-tile wall for a fact that can never become true is pure cost.
  const hasHostTarget = !!props.annotateTarget;
  const [hostPane, setHostPane] = useState(false);
  useEffect(() => {
    if (!hasHostTarget) {
      setHostPane(false);
      return;
    }
    const look = () => {
      const frame = hostFrame();
      setHostPane(!!frame);
      // The host's document is the app's, so its console is the console the
      // agent asks about — wrapped exactly as `AppPane` wraps ours on load
      // (T:8544/8833 → `watchApp`). Idempotent per document, and the re-wrap
      // after a reload is how "that error was there before your edit" stays
      // answerable.
      if (frame) watcher.watchApp();
    };
    look();
    const id = window.setInterval(look, HOST_PANE_POLL_MS);
    return () => window.clearInterval(id);
  }, [hasHostTarget, hostFrame, watcher]);

  const hasPane = useRef(false);
  hasPane.current = (!pane.noPane && pane.status === "ready") || hostPane;
  // …BUT "not ready yet" IS NOT "no pane" for the field that goes on the wire.
  //
  // `has_pane` is what agent.py builds the session's MCP roster off, once, at
  // spawn: `pane=False` writes an `mcp.json` with no app-state channel and an
  // `--allowed-tools` without `mcp__fused_approvals__app_state`, and there is
  // no way back — `_send` never touches argv, and `host.json` does not even
  // record the pane, so nothing can respawn for one appearing. A first message
  // typed before `statPath`/`runAppEntry` landed therefore cost the WHOLE
  // session its app-state tool, and the CLI said the tool was not reachable
  // (feedback R2-10, and the legacy screenshot behind it).
  //
  // `null` while the decision is outstanding sends the field empty, which
  // agent.py reads as "the page has no opinion" and answers with
  // `_has_pane(file)` — the same fact, off the filesystem, without the race.
  // "error" still answers false: the frame really has been swapped for a
  // message, so nothing would respond to an app-state read.
  const paneAnswer = useRef<() => boolean | null>(() => null);
  paneAnswer.current = () => {
    // A frame we can see RIGHT NOW settles it, ours or the host's.
    if (hasPane.current || appFrame()) return true;
    // Our own decision is outstanding.
    if (!pane.noPane && pane.status === "resolving") return null;
    // Or the HOST has an app-state slot it has not marked yet — the sidebar's
    // copy of the same race, and the one with the same price: the mark lands
    // when the content frame paints, and a message typed before that would
    // otherwise cost the whole session its `app_state` tool. `null` sends the
    // field empty and lets agent.py answer off the filesystem (`_has_pane`),
    // which for the file targets this layout is built on is the same answer.
    if (hasHostTarget) return null;
    return false;
  };

  // ── the controller ─────────────────────────────────────────────────────────
  // Rebuilt only for a new target: the model / effort / pane answers it reads at
  // SEND time all go through refs, so a keystroke in a picker cannot restart the
  // run loop.
  const liveModel = useRef("");
  const liveEffort = useRef("");
  /** PR4's run-clock seats, filled below once the stores that answer them
   *  exist. Refs for the reason every other send-time read here is one: the
   *  controller closes over them and is built first. */
  const artTick = useRef<(() => void) | null>(null);
  const snapInvalidate = useRef<(() => void) | null>(null);
  /**
   * PR2's two send-time refs, declared HERE because the controller closes over
   * them and is built before the tray below exists.
   *
   * `inFlight` maps the very `Receipt[]` handed to a send to the ATTACHMENTS it
   * came from — the identity is the key, so a send whose bubble was dropped
   * gives back its own pictures and not another send's. `attachBack` is the
   * tray's own `giveBack`, filled once the tray is built.
   */
  const inFlight = useRef(new Map<Receipt[], Attachment[]>());
  // The one seam the delete-on-success can be read through (see
  // `inFlightSizeForTests` at the top of this file).
  useEffect(() => {
    inFlightForTests = inFlight.current;
    return () => {
      if (inFlightForTests === inFlight.current) inFlightForTests = null;
    };
  }, []);
  // …AND THE PICTURES ALREADY ON THEIR WAY, which nothing else can reach.
  //
  // `take()` moves a send's attachments OUT of the tray and into `inFlight`, so
  // the tray's own unmount revoke — which walks its live list (useAttachments'
  // `alive` effect) — cannot see them, and the hand-back that would have
  // returned them is nulled by `attachBack`'s cleanup below. A chat closed while
  // a send was in flight therefore pinned a full-pane Blob per picture for the
  // life of the page (Bugbot, PR #1064).
  //
  // REVOKED, not handed back: there is no tray left to hand them to. Declared
  // here so it is the FIRST of these three cleanups to run — React runs them in
  // declaration order — and the map is cleared, so a StrictMode re-mount does
  // not walk revoked handles again.
  useEffect(() => {
    const sends = inFlight.current;
    return () => {
      for (const items of sends.values()) for (const att of items) ATTACH_API.revoke(att);
      sends.clear();
    };
  }, []);
  /**
   * THE HANDLES A LANDED SEND HAS FINISHED WITH, held until the swap they were
   * replaced by is actually ON SCREEN.
   *
   * `settleAttachments` re-points the receipts at the copy on disk, but that is
   * a STORE WRITE: React has not rendered, let alone committed, by the time the
   * call returns, so the `<img>` under the bubble is still showing the object
   * URL. Revoking in that same tick pulled the picture out from under it — the
   * img errored, `ShotRow` read the error as "the pruner deleted it" and every
   * successful send of a pasted or captured picture ended in "screenshot no
   * longer on disk" (Bugbot, PR #1064).
   *
   * So the spent handles are released from an EFFECT: a passive effect runs
   * after React has mutated the tree, so by the time it fires every receipt is
   * drawn with `rawUrl(view)` and nothing on screen is holding a `blob:` handle
   * any more.
   *
   * ONE QUEUE, in a ref, and the state is only the TRIGGER. A second copy in
   * state was two bookkeepers for one list, and they diverged: the effect
   * cleared the ref wholesale while state legitimately kept a NEWER send's
   * handles, so those lost their unmount path and were pinned for the life of
   * the document (Bugbot, PR #1064). The effect now drains whatever the ref
   * holds at commit time — every entry in it has had its store write already —
   * and anything queued after that gets its own tick, its own effect run, or the
   * unmount below.
   */
  const spentAlive = useRef<Attachment[]>([]);
  const [spentTick, setSpentTick] = useState(0);
  const dropSpent = useCallback(() => {
    const go = spentAlive.current;
    if (!go.length) return;
    spentAlive.current = [];
    for (const att of go) ATTACH_API.revoke(att);
  }, []);
  // `spentTick` is not read in the body: it IS the message ("a swap has been
  // committed"), and the drain deliberately takes the whole queue rather than
  // the one send that raised the tick.
  useEffect(dropSpent, [dropSpent, spentTick]);
  // AND AT UNMOUNT, when there is no screen left to keep them for: the tray gave
  // these up and `inFlight` has already deleted the send, so nothing else has a
  // reference to release.
  useEffect(() => dropSpent, [dropSpent]);
  const attachBack = useRef<((items: readonly Attachment[]) => void) | null>(null);
  /** WHICH sends have come BACK, by `SendOptions.sendId` — see
   *  `onSendReturned` below, and `sendId`'s own note in controller-api. */
  const returnedSends = useRef(new Set<string>());
  const sendSeq = useRef(0);
  /** THE SEND WINDOW'S LATCH. A ref rather than state, because the composer
   *  reads it in the very tick it calls `onSend` — before React can re-render
   *  with a new prop (`dispatchSend`). */
  const sendBusy = useRef(false);
  /** WHICH send holds the latch, by `SendOptions.sendId`. A release is decided
   *  long after it was armed — at the end of a turn, or by a status the store
   *  reports — and unowned, one send's release would open the door in the
   *  middle of another send's capture window. */
  const sendHolder = useRef("");
  /** The send that has been TAKEN but whose run is not live yet: the window the
   *  status effect below closes (`dispatchSend`). */
  const liveWait = useRef("");
  /** The same fact as state, for the send button's `disabled`. */
  const [sendLocked, setSendLocked] = useState(false);
  /**
   * THE MESSAGES THIS FOLDER WAS TOO BUSY TO TAKE, as the ADMISSION answered
   * them — a seed, not the row.
   *
   * The row itself is drawn from the SERVER (`sched.waitingHere`, or the leader's
   * followers on a chat with no session): that is what makes a reload show the
   * identical picture, and it is the whole difference between this and the chip
   * it replaces. What this holds is the window before the next poll has listed
   * the new entry — up to fifteen seconds in which a bubble the reader just
   * pressed Enter on would otherwise not be on screen at all.
   *
   * A LIST, not one: the composer stays open under the queue, so a reader may put
   * three messages into a busy folder and every one of them is owed its place.
   * Each seed is retired the moment the poll carries its entry (`waitingRows`
   * drops a seed the server has published), and on a bounded number of laps when
   * the entry ran before any poll ever saw it (`useLiveSeeds`).
   *
   * IT CARRIES THE TYPED LINE, because the stored entry holds the COMPOSED
   * message — attachment markers and all — and swapping one for the other under
   * the reader on the next poll would be the row silently rewriting itself.
   */
  const [waitingSeeds, setWaitingSeeds] = useState<WaitingSeed[]>([]);
  /**
   * WHAT THE ADMISSION SAID WAS IN FRONT — the fallback for "behind TASK-038"
   * until this conversation's own `/api/tasks` row has been read.
   *
   * ONE ANSWER FOR THE WHOLE CHAT, not one per message, because that is what the
   * fact IS: a folder is held by one task, and three messages waiting in one line
   * are all behind the same thing. The authority is the server row
   * (`sched.rec`), which now wins outright the moment there is one; this is what
   * the first paint has before it arrives.
   */
  const [admitAhead, setAdmitAhead] = useState<QueueFacts | null>(null);
  /**
   * THE NUMBER THE ADMISSION GAVE THIS CONVERSATION — "TASK-057", for the header
   * until a `/api/tasks` listing says the same thing.
   *
   * A queued send CREATES the task (the entry is the task), so the server can
   * name it in the very answer that queued the message — and the header used to
   * wait for a listing anyway, leaving a conversation numberless for up to a
   * poll interval while the id a reader needs to find it again was in hand
   * (Akshil, 2026-09-12). Ordering against the two listings is `headerTaskId`.
   */
  const [admitTaskId, setAdmitTaskId] = useState("");
  /** Run next is in flight, or already spent: the card's one button is dead. */
  const [runningNext, setRunningNext] = useState(false);
  /**
   * RUN NEXT WAS ACCEPTED, AND NO ROW HAS ANSWERED SINCE — the row generation as
   * it stood at the press (`useSchedule.recGen`), or null for "no claim".
   *
   * The press changes a fact this pane cannot see: the row in hand was read
   * BEFORE it, and it goes on saying `behind TASK-038` until the next listing.
   * Painting the claim through `admitAhead` was not enough, and could not be: the
   * facts prefer the server's row whenever there is one, so the card sat visibly
   * unchanged under a button the reader had just pressed and watched succeed
   * (Bugbot PR #1124).
   *
   * A DEADLINE, NOT A STATE. It is spent the moment a fresher row lands —
   * `schedRefresh` asks for one immediately — and that row then decides, which is
   * what puts the button back when the server turns out to have refused.
   */
  const [nextClaim, setNextClaim] = useState<number | null>(null);
  /** A delete in flight, by entry id: that row's one control is dead for its
   *  duration and the row leaves when it lands. */
  const [deleting, setDeleting] = useState<ReadonlySet<string>>(() => new Set());
  /** Entries a `delete` has TAKEN BACK — dropped on the server's answer rather
   *  than on the next poll, and remembered until a poll stops listing them
   *  because until then the poll's own list still has them and would draw the row
   *  the delete just took down (`sched/waiting` `pruneDropped`). */
  const [droppedEntries, setDroppedEntries] = useState<ReadonlySet<string>>(NO_DROPPED);
  // SUBSCRIBED, AND NOW ALSO READ. The admission is still asked on the keystroke
  // from `queueEnabled()` — a plain synchronous read of the same one
  // `/api/prefs` answer — and subscribing is what puts that answer ON ITS WAY
  // from mount rather than from whenever some other hook happens to ask, so the
  // first send of a freshly opened chat is admitted rather than spawning into a
  // folder somebody has just protected. The VALUE is drawn for two things the
  // flag owns outright: the waiting rows, and the composer's own follow-up note,
  // which the flag takes away (see the Composer's `queueOn`).
  const queueOn = useProjectQueueEnabled();
  const releaseSend = useCallback((id: string) => {
    if (sendHolder.current !== id) return;
    sendHolder.current = "";
    liveWait.current = "";
    sendBusy.current = false;
    setSendLocked(false);
  }, []);
  const [stranded, setStranded] = useState<{ text: string; seq: number } | null>(null);
  const strandSeq = useRef(0);
  /** Words that have nowhere else to be go back in the BOX — the follow-up the
   *  CLI never delivered (`onStranded`) and a send the controller refused
   *  before it ever reached `addUser` both land here. */
  const strand = useCallback((text: string) => {
    if (!text) return;
    strandSeq.current += 1;
    setStranded({ text, seq: strandSeq.current });
  }, []);

  // One collapse policy per MOUNT, not per module: six compact mounts on the
  // cards wall share this module and their chip keys collide by construction
  // (ui/cardPolicy.ts).
  const [cardPolicy] = useState(createCardPolicy);
  const controller = useMemo(
    () =>
      createChatController({
        file,
        agentDir,
        params,
        // The transcript restore takes the in-process road (owner E2E R1, F5):
        // `/api/claude-sessions/history`, with agent.py through `/api/run`
        // behind it. See `fetchHistory`.
        history: (f, s) => fetchHistory(agentDir, f, s),
        historyCache: sharedHistoryCache,
        model: () => liveModel.current,
        effort: () => liveEffort.current,
        hasPane: () => paneAnswer.current(),
        // The controller already announces on THIS document
        // (`announceTasksChanged`); the stamp is for every OTHER one (T:16435).
        onActivity: stampChatActivity,
        // Follow-ups the CLI never delivered come BACK to the box they were
        // typed in rather than being dropped (`still_queued`, T:15911).
        onStranded: (texts) => strand(texts.filter(Boolean).join("\n")),
        // THE PUSH CHANNEL. Read at SEND time from the watcher, which is the
        // same object the pull channel answers through — `blockForSend` does
        // push → offload → block, in T's order (T:16483, 5177-5218). Gated on
        // `appFrame()` — A FRAME, RESOLVED HERE, not the polled `hasPane` flag:
        // this is the moment whose state the user is describing, and asking the
        // frame directly cannot be a render behind a mark that has just landed.
        // A mount with neither pane still sends nothing, so a cards tile never
        // claims to describe an app it cannot see.
        //
        // Through a ref, like every other send-time read here: the watcher
        // outlives a pane reload and rebuilding the controller for it would
        // restart the run loop.
        appStateBlock: () =>
          appFrame() ? watcher.blockForSend() : Promise.resolve(""),
        // PR4's two run-clock hooks. Both go through refs: the controller is
        // built before the strip's store and the landing's counter exist, and
        // rebuilding it for either would restart the run loop (T:16229,
        // 16321-16330).
        onArtifactsTick: () => artTick.current?.(),
        // T:10608/16341 — the run this turn started is over, so the annotations
        // it carried are HANDLED: drop them. Even on error, deliberately (T's
        // own note): an errored run may not have acted on them, but they were
        // already stamped `sent` and folded into the transcript — re-annotating
        // is one click, silently re-sending is not.
        onRunEnded: () => {
          annRef.current?.resolveSent();
          // T:19078 `snapInvalidate` — the turn that just ended may have edited
          // the file, so the checkpoint chain the landing drew is stale. The
          // panel is unmounted while a chat is on screen, so what survives the
          // round trip is the FACT of a run ending, counted here and handed to
          // `useSnapshots` as its invalidation key.
          snapInvalidate.current?.();
        },
        // T:17792 — a stale `run` param is not a turn ending: the annotations
        // still have to come back (this is the one road on which they would be
        // stranded `sent` forever), but nothing ran, so the checkpoint chain
        // the landing drew is exactly as fresh as it was.
        onRunAbandoned: () => {
          annRef.current?.resolveSent();
        },
        /**
         * T:17866 — the caret goes back in the box at the end of EVERY
         * re-attach: a boot `?run=`, an adoption by the standing watch, a
         * scheduled message's attach (P4-17). `autoFocus` covered boot
         * incidentally; a mid-session adoption left a reader who had just been
         * handed a streaming reply having to click to answer it.
         *
         * `preventScroll`, like every other focus in this view: the composer is
         * pinned to the bottom of a scrolling transcript and taking the caret
         * must not move what the reader is looking at — which also means this
         * does not fight the repair's own scroll-to-bottom beside it.
         */
        focusComposer: () => {
          boxRef.current?.focus({ preventScroll: true });
        },
        // The agent saw none of it, so the pictures come back to the tray —
        // never revoked on this road, because those very thumbnails are what the
        // returned chips show (T:16693-16720).
        onSendReturned: ({ attachments, sendId, text, refused }) => {
          // A REFUSED SEND OWES THE WORDS BACK. The composer cleared its box on
          // the keystroke and the controller turned the message away before it
          // ever reached `addUser`, so there is no bubble and no queue entry
          // holding them: unless they come back to the box they are simply
          // gone (Bugbot, PR #1074). The latch below makes this window hard to
          // reach from the composer; ✓ Done and the walkthrough share the seat,
          // and a refusal must never cost the user a sentence.
          if (refused) strand(text);
          // T:16068 — THE ROLL-BACK SIGNAL, and it NAMES ITS SEND: the agent saw
          // none of THAT message, which is what its notes' `sent = 0` and its
          // overview's revoke hang off. It used to be a counter, and a counter
          // cannot tell "my send came back" from "a send came back while mine
          // was still out" — a second submit inside the first send's capture
          // window bumped it, and the first send rolled back a turn the agent
          // had already taken (Bugbot, PR #1074).
          if (sendId) returnedSends.current.add(sendId);
          if (!attachments) return;
          const back = inFlight.current.get(attachments);
          if (!back) return;
          inFlight.current.delete(attachments);
          attachBack.current?.(back);
        },
      }),
    [agentDir, file, params, strand],
  );
  useEffect(() => () => controller.dispose(), [controller]);

  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getState,
    controller.getState,
  );

  /**
   * THE DOOR OPENS WHEN THE RUN IS LIVE, not when the controller took the
   * message.
   *
   * `sendMessage` sets its own `sending` gate before its first await and holds
   * it for the whole turn, but the STATUS the composer reads stays `idle` until
   * `pollLoop` reports — one `start` round-trip away. A line typed inside that
   * window read as "no run yet", so the composer sent it as a fresh message
   * rather than a follow-up: the controller refused it out loud, and the
   * refusal took the optimistic bubble down with it. The words were nowhere
   * (Bugbot, PR #1074).
   *
   * So the latch stays shut across the start window and lifts here, on the
   * first status that is not `idle` — from which point the composer routes the
   * same keystroke to `sendFollowUp`, which the run can actually take.
   * `dispatchSend`'s own release is the other end: a send that never went live
   * (refused, or a `start` that failed) must not latch the box for ever.
   */
  useEffect(() => {
    if (liveWait.current && state.status !== "idle") releaseSend(liveWait.current);
  }, [state.status, releaseSend]);

  // ── the attachments this message will carry (PR2, inventory 03) ────────────
  //
  // The tray lives HERE and not in the composer, for the reason T keeps
  // `shotAttached` at module scope: the camera that fills it is in the control
  // strip, the chips that show it are above the box, the receipts it becomes are
  // in the transcript and the send that empties it is the controller's — four
  // places, one list.
  const attach = useAttachments({
    agentDir,
    // THE FRAME WHOSE DOCUMENT IS THE APP, read at gesture time: ours when we
    // have a pane, the HOST's marked one when we do not (`appFrame`). A capture
    // aimed at the element as it was when this callback was made would
    // photograph a document that has since been replaced by a mode swap — and
    // one aimed at `paneFrame` alone found nothing at all in the hosted
    // `?_side=claude` layout, where the only app frame is the host's. T does not
    // have this seam because `appWindow()` reads off `annFrame` whichever of the
    // two it is (T:4865), and the camera reads `annFrame`.
    frame: () => appFrame(),
    // What the shutter flashes over: that frame's own box, which is the offset
    // parent the pins and the highlight also live in (T:11233). The host's frame
    // is a node in THIS document (Preview.tsx renders both columns), so its
    // parent is a real box to flash over.
    flashHost: () => appFrame()?.parentElement ?? null,
    paneNoun: pane.paneNoun,
  });
  /** The picture the viewer is showing, pending or sent (T:10681 `shotViewing`). */
  const [viewing, setViewing] = useState<Viewable | null>(null);
  // The tray's hand-back, reachable from the controller's callback above. In an
  // EFFECT and not the render body: a render React throws away (a StrictMode
  // double-invoke, a concurrent attempt that loses) must not leave its handle
  // installed for the controller to call.
  useEffect(() => {
    attachBack.current = attach.giveBack;
    return () => {
      attachBack.current = null;
    };
  }, [attach.giveBack]);

  /**
   * THE TWO CAPTURE LISTENERS THE CHAT OWNS, registered from its own mount.
   *
   *   * `watchTopOrigin` learns the top window's viewport origin from the click
   *     that PRECEDES a capture (T:9812). It used to be registered at module
   *     load, which put a document-wide listener on every bundle that so much as
   *     imports `shots/*` — the chat flag off included.
   *   * `watchStreamTeardown` ends the kept tab share on `pagehide` and on this
   *     unmount, which is what the xo-capture header promises: the browser's
   *     "sharing this tab" indicator must not outlive the chat that raised it
   *     (T:9731).
   */
  useEffect(() => {
    const win = typeof window === "undefined" ? null : window;
    const offOrigin = watchTopOrigin(win);
    const offStream = watchStreamTeardown(win);
    return () => {
      offOrigin();
      offStream();
    };
  }, []);

  // ── the annotations this message will carry (PR3, inventory 02) ───────────
  //
  // `useAnnotations` is the door: it owns the store, the mode machine, the
  // target poll, the overlay and the six listeners inside the framed document.
  // What is left here is the SEAMS — eight of them, listed in `ann/README.md` —
  // and the two facts only this file knows: which composer is on screen, and
  // whether a send is allowed right now.
  const viewingRef = useRef(viewing);
  viewingRef.current = viewing;
  const statusRef = useRef(state.status);
  statusRef.current = state.status;
  /** T:8505 `annAutoSubmit` — the composer that is actually mounted (home or
   *  chat, never both) hands its own send in here. */
  const submitBox = useRef<((seed?: string) => boolean) | null>(null);
  /** `.c-leftview`, from the pane. The pins, the ring and every popover
   *  coordinate are measured against this box and not against `.c-left`
   *  (T:6888). */
  const [stage, setStage] = useState<HTMLElement | null>(null);

  /**
   * THE VOICE WALKTHROUGH's state machine, built once. Its ports read `annRef`
   * rather than closing over the hook, because the two need each other: the
   * coordinator asks the recorder whether a mic is live (`AnnRecorder`), and the
   * recorder writes its marks into the coordinator's store.
   *
   * `notes` is a PORT and not the store itself — `ann/rec.ts` deliberately knows
   * nothing about `Annotation` — so `spoken` is translated to `content` here, in
   * the one place that speaks both. The anchor's own `text` (the element digest)
   * is NOT that field and rides through in `rest`: two facts, two names.
   */
  const [recorder] = useState<Recorder>(() =>
    createRecorder({
      capture: (o) => captureAudio(o),
      // Fire-and-forget by contract: the model load overlaps the recording, so
      // the stop is not the first thing that ever asks for it (T:7814).
      warm: () => void warmTranscriber(),
      transcribe: (path) => transcribe({ path, words: true }),
      notes: {
        add: (note) => {
          const store = annRef.current?.store;
          if (!store) return;
          // `RecAnchor`'s own keys ride along untyped by design (the recorder is
          // handed the anchor the click handler built and never reads into it),
          // so the cast is the honest spelling of that contract.
          const { spoken, sent, ...rest } = note as RecAnnotation & Record<string, unknown>;
          store.add({
            ...(rest as unknown as Omit<Annotation, "id" | "createdAt">),
            content: spoken,
            ...(sent ? { sent: 1 as const } : {}),
          });
        },
        get: (id) => {
          const c = annRef.current?.store.list().find((n) => n.id === id);
          if (!c) return undefined;
          const out: RecAnnotation = { id: c.id, spoken: c.content };
          if (typeof c.t === "number") out.t = c.t;
          if (c.kind) out.kind = c.kind;
          if (c.createdAt) out.createdAt = c.createdAt;
          if (c.sent) out.sent = true;
          return out;
        },
        // ONE save, one notification, whatever the count: the transcript folds
        // into a whole walkthrough, not note by note (`store.merge`).
        assign: (texts) => {
          const store = annRef.current?.store;
          if (!store) return;
          const words = new Map(texts.map((t) => [t.id, t.text]));
          const next = store
            .list()
            // NOT ONTO A NOTE THE AGENT ALREADY HAS (Bugbot, PR #1074). A mark
            // sent before its words landed cannot be corrected by rewriting the
            // page's copy: Claude was handed the note as it stood, and a silent
            // edit afterwards makes this page and that transcript disagree about
            // what was asked. `isSendableNow` is the half that keeps a WORDLESS
            // mark from being sent at all; this is the other half, for a mark
            // whose words the reader typed by hand and sent mid-walkthrough.
            .filter((n) => words.has(n.id) && !n.sent)
            .map((n) => ({ ...n, content: words.get(n.id) as string }));
          if (next.length) store.merge(next);
        },
        remove: (ids) => annRef.current?.store.removeMany(ids),
      },
      mode: {
        isArmed: () => annRef.current?.machine.armed() ?? false,
        capable: () => annRef.current?.target.capable() ?? false,
        arm: () => annRef.current?.machine.set(true),
        disarm: () => annRef.current?.machine.set(false),
        epoch: () => annRef.current?.machine.epoch() ?? 0,
        syncParam: () => annRef.current?.store.syncModeParam("2"),
      },
      /**
       * T:8289 — everything said BEFORE the first click is the message's own
       * prompt, so it rides the walkthrough's own send.
       *
       * SYNCHRONOUS, AND THE INTRO TRAVELS WITH THE PRESS. It used to be
       * written into the box through the composer's `restore` seat — a STATE
       * write — and the send pressed from a `setTimeout(0)`, on the assumption
       * that one macrotask is enough for React to have applied it. It is not a
       * guarantee: the timer could run first, `submit` then read an empty box,
       * and the notes went to the agent without the sentence that introduced
       * them (Bugbot, PR #1074). The seat takes the words as an argument
       * instead, so there is no window to lose them in.
       */
      deliver: (intro, spoke) => {
        if (!spoke && !intro) return;
        // `canSend`'s own gate (T:7720 `activeRun || !sending`): only the width
        // of a start request is a moment with nowhere to put them.
        const sent =
          statusRef.current === "starting"
            ? false
            : (submitBox.current?.(intro || undefined) ?? false);
        // REFUSED — no composer mounted, a scheduled message pending, the send
        // window latched. The words are not dropped: they go back to the box,
        // which is the one place the reader can act on them.
        if (!sent && intro) {
          strandSeq.current += 1;
          setStranded({ text: intro, seq: strandSeq.current });
        }
      },
    }),
  );
  const recSnap = useSyncExternalStore(recorder.subscribe, recorder.snapshot, recorder.snapshot);
  /** The mode machine's own view of the mic: four questions, no microphone. */
  const annRecorder = useMemo<AnnRecorder>(
    () => ({
      // "starting" TOO — the width of the getUserMedia prompt. With it excluded
      // the machine called that window Comment mode: the bar showed ✓ Done, the
      // Comment seat came alive beside a mic that was about to open, and
      // `set(false)` skipped `end()`, so a dismissal could not cancel the
      // in-flight capture and the mic came up after the reader had left
      // (Bugbot, PR #1074). `commentSeatName` already read it this way; this is
      // the other half of the same fact — and `rec.ts` arms the MODE at the
      // press for the same reason, so `armed()` (Esc, the nav lock, the narrow
      // view's disarm) is true for that window too.
      //
      // `cancelling` is NOT here: a dismissed start being put back down has
      // already handed the mode back, and calling it a recording would send
      // `set(false)` looking for something to stop.
      recording: () => {
        const s = recorder.snapshot().state;
        return s === "starting" || s === "recording";
      },
      settling: () => recorder.snapshot().busy,
      end: () => void recorder.end(),
      discard: () => void recorder.discard(),
      // The teardown's ending, and the one of the three that is NOT a promise:
      // a `pagehide` handler gets no await, which is why `rec.ts` spells this
      // one synchronously (T:8796-8812).
      abandon: () => recorder.abandon(),
    }),
    [recorder],
  );

  const ann = useAnnotations({
    params,
    // `chat_only=1`: the pane belongs to the host, so the overlay is INJECTED
    // into the marked frame's document rather than being a node of this one.
    hosted: chatOnly,
    noPane: pane.noPane,
    ...(props.annotateTarget ? { annotateTarget: props.annotateTarget } : {}),
    // The ONE thing that genuinely has to live in the parent: the cross-origin
    // overlay stands over the frame's rect, and that rect is in the host's
    // layout. Same origin by construction — the host marked the frame for us —
    // and guarded anyway, because a `parent` we cannot read is the honest
    // "no XO overlay" answer rather than a throw at poll time.
    parentDocument: () => {
      try {
        const win = typeof window === "undefined" ? null : window;
        if (!win || win.parent === win) return null;
        return win.parent.document;
      } catch {
        return null;
      }
    },
    // T:7291 — the parked composer's home is the chat column.
    composerHome: () => columnRef.current,
    // T:7720 `activeRun || !sending`: a live run takes the notes as a follow-up,
    // and only the width of a start request is a moment with nowhere to put
    // them.
    canSend: () => statusRef.current !== "starting",
    autoSubmit: () => void submitBox.current?.(),
    // T:7670 — arming over a cross-origin target is the natural moment for the
    // ONE tab-share prompt, and only where the native screen shot is off: with
    // it there is no prompt at all, and raising one here would be the prompt
    // that change exists to remove. Swallowed whole — a declined share means
    // notes without pictures, which every capture path already degrades to.
    onXOArm: () => {
      if (isNativeOff()) void getStream().catch(() => {});
    },
    viewerOpen: () => viewingRef.current !== null,
    recorder: () => annRecorder,
    // While a walkthrough owns the click the RECORDER writes the note: it mints
    // the id and the `t` the transcript is matched against.
    recMark: (anchor: AnnAnchor) => recorder.mark(anchor as RecAnchor),
    recMarkPoint: (cx, cy, win, nearPath) => recorder.markPoint(cx, cy, win, nearPath ?? null),
  });
  annRef.current = ann;
  // The seam above, parked from an EFFECT — the most recently mounted chat owns
  // it, and a render React discarded must not.
  useEffect(() => {
    annForTests = ann;
    return () => {
      if (annForTests === ann) annForTests = null;
    };
  }, [ann]);

  /**
   * A34 / T:7788 — WHETHER THIS MACHINE HAS A MIC AT ALL. `captureSources` says
   * so WITHOUT prompting for permission (CP-7), which is what makes it safe to
   * ask before anyone has pressed anything: a machine that cannot record gets no
   * mic seat rather than one that could only ever alert — "absent beats dead",
   * the rule the camera seat already follows.
   *
   * Asked at boot AND again on every arm, because `granted` moves with TCC and
   * does not wait for this process to restart. Anything but an explicit
   * `available: false` keeps the seat, so a probe that fails degrades to showing
   * it rather than to hiding the feature.
   *
   * ONE PROBE, TWO ANSWERS, which is how T reads it too (T:7841-7852 takes the
   * still and the mic off the same `src`): the SCREENSHOT half decides whether
   * the native still road is open at all, and getting that from the probe
   * rather than from a live 409 is what makes the cross-origin tab-share
   * prompt fire at arm time, where the user activation is
   * (`shots/noteSourcesProbe`).
   */
  const [micShown, setMicShown] = useState(true);
  const annArmed = ann.mode !== "off";
  useEffect(() => {
    let live = true;
    void captureSources()
      .then((sources) => {
        // BEFORE the `live` gate and outside it: this is module state about the
        // PLATFORM, not component state about this mount, and it is just as
        // true for the next mount as for this one. An unmount racing the probe
        // should not throw the answer away.
        noteSourcesProbe(sources);
        if (!live) return;
        const audio = sources.audio;
        setMicShown(audio?.available !== false);
        // AND SAY WHY THE SEAT WENT (T:7801). The reason string is CP-11's, and
        // it names a browser that CAN record — so a reader with no seat and no
        // explanation at least has the console to go on. Swallowing it left
        // "the feature is missing" indistinguishable from "the feature is
        // broken".
        if (audio?.available === false) {
          console.warn("spoken walkthroughs unavailable:", audio.reason || "");
        }
      })
      .catch(() => {
        /* no answer is not a NO: the seat stays */
      });
    return () => {
      live = false;
    };
  }, [annArmed]);

  /**
   * The recorder's state, told to the mode machine (T:8114's `.busy` seat) and
   * to the bar's clock.
   *
   * `setBusyHold` is `annBusyHold`: the nav lock is the MODE's claim, and
   * through Stopping…/Transcribing… the mode is still armed (the transcription
   * belongs to THIS chat) — so the hold outlives the recording flag and is
   * released by the disarm at the end of the settle.
   */
  useEffect(() => {
    const m = ann.machine;
    m.setBusyHold(recSnap.busy);
    m.setPhase(
      recSnap.state === "transcribing" ? "transcribing" : recSnap.busy ? "settling" : null,
    );
    ann.setClock(recSnap.state === "recording" ? recClockText(recSnap.seconds * 1000, recSnap.marks) : "");
  }, [ann, recSnap]);

  /** The bar's three buttons in the SPLIT layout. (The injected and
   *  cross-origin bars get the same three from inside the hook, since their node
   *  is built over there.) */
  const annBarHandlers = useMemo<AnnBarHandlers>(
    () => ({
      onDone: () => void ann.done(),
      onStop: () => void recorder.end(),
      // T:8419 — ONE dispatcher: the trash throws the walkthrough while one
      // records and the round's notes otherwise.
      onDiscard: () => ann.discard(),
      onResize: (bar) => barFit(bar),
    }),
    [ann, recorder],
  );
  /** `picker: null` — the Element/Point pill is ONE node the coordinator owns
   *  and adopts into the bar's `.slot` on its own paint; a second writer would
   *  fight it for the node. */
  const annBarPaint = useMemo(
    () => ({
      mode: ann.mode,
      clock: recSnap.state === "recording" ? recClockText(recSnap.seconds * 1000, recSnap.marks) : "",
      picker: null,
    }),
    [ann.mode, recSnap],
  );

  // ── the three pills ────────────────────────────────────────────────────────
  const defaults = useComposerDefaults(agentDir, file, params);
  liveModel.current = defaults.model;
  liveEffort.current = defaults.effort;
  // The ask branch waits on these before its automatic send, so a "Fix with AI"
  // run never launches on the fallback model (T:19233-19248).
  const detected = useRef<{ promise: Promise<void>; done: () => void } | null>(null);
  if (!detected.current) {
    let done = () => {};
    const promise = new Promise<void>((res) => {
      done = res;
    });
    detected.current = { promise, done };
  }
  useEffect(() => {
    if (defaults.ready) detected.current?.done();
  }, [defaults.ready]);

  // ── the app-state pull channel (T:15731-15840) ─────────────────────────────
  useAppStateResponder({
    rows: state.appState,
    watcher: hasPane.current ? watcher : null,
    answerAppState: controller.answerAppState,
    // NO `onNote` HERE, DELIBERATELY (audit A's GAP-B1 adjacency / P3-21 reads
    // this as a missing wire; it is not one at this tip). T:15806's one-line
    // "read app state" row is already written by the controller, inside
    // `answerAppState` itself (`run-controller.ts:1905-1909`), under its own
    // once-per-REQUEST latch — and `answerAppState` is what the line above
    // hands this hook. The responder's `onNote` is a second, equivalent seam
    // for a host that answers app state WITHOUT the controller; passing it here
    // would put two identical ◍ rows in the transcript for one tool call.
    //
    // Both paths are pinned: `run-controller.test.ts` for the live one,
    // `useAppStateResponder.test.tsx` for the seam.
  });

  // ── the ask, LATCHED PER MOUNT ─────────────────────────────────────────────
  //
  // THE ASK IS THIS MOUNT'S IDENTITY, NOT A LIVE PROP. The host keys the mount
  // per DELIVERY (`Preview.tsx`'s `claudeMountKey` → `claude:<seq>`, and
  // `Listing.tsx`'s pane key), so a new ask is a NEW MOUNT — which means the
  // prop only ever has to be read once, at boot, and re-reading it later can
  // only go wrong.
  //
  // AND IT DID. The host derives `nativeAsk` at RENDER time from a ref written
  // in a committed effect (`deliveredAsk`), so the prop is non-null for exactly
  // ONE render of the host and `undefined` on the very next one, whatever
  // caused it. With `props.initialAsk` in the boot effect's deps, that flip
  // tore the boot down 32 ms into the ≤1.5 s model/effort detection wait — the
  // cleanup set `cancelled` and re-armed the latch, so the awaited send was
  // skipped, and the re-run read `undefined` and took the "nothing to restore"
  // branch. `entered` stayed true from the first pass, so the user got exactly
  // what QA reported: the chat opens, the composer is live, and the prompt is
  // gone (R4-4). Legacy is immune because its template PULLS the ask at its own
  // boot, where no React prop can vanish underneath it.
  //
  // Sticky and never un-set, so a host re-render inside the wait cannot cancel
  // a send already on its way. Still exactly one send: `bootDispatched` (per
  // boot) and `bootedFor` (per controller) are what enforce that, not the
  // prop's lifetime. And a replay is still impossible — a remount at the SAME
  // key arrives with `initialAsk: undefined` (the host's `deliveredAsk` guard
  // is untouched) and a fresh, empty latch.
  const askRef = useRef(props.initialAsk);
  // SPENT IS SPENT. The re-arm below runs on every render, and the host keeps
  // `initialAsk` on its props until its own one-shot derivation flips it — so a
  // boot that cleared the latch inside its async walk found it re-armed by the
  // very next render, and a controller rebuild (a `file` swap with `agentDir`
  // already cached) fired the same "Fix with AI" prompt at the new target
  // (QA, PR #1061). Once the one dispatch has happened, the prop is history.
  const askSpent = useRef(false);
  if (props.initialAsk && !askSpent.current) askRef.current = props.initialAsk;

  // ── home vs chat (T:1277-1282 `#chat.home`) ────────────────────────────────
  // The host's ids count here as well as the store's: they are seeded into the
  // store by the boot effect below (a COMMITTED effect — a `params.set` in a
  // render body is a history write from a render React may discard), and this
  // initializer runs before it.
  const [entered, setEntered] = useState(
    () =>
      !!(
        params.get("session_id") ||
        // A CHAT THAT HAS NEVER RUN IS STILL A CHAT (`QUEUED_PARAM`): it has a
        // waiting bubble, a task number and a line to be in, and landing on the
        // home view instead would show none of them.
        params.get(QUEUED_PARAM) ||
        params.get("run") ||
        askRef.current ||
        props.initialSessionId ||
        props.initialRunId
      ),
  );
  const inChat = entered || !!state.sessionId;

  // ── the ready signal, fired once ───────────────────────────────────────────
  const readySent = useRef(false);
  // STABLE FOREVER, and the prop is read through a ref: `markReady` is a
  // dependency of the boot effect, and the boot is now cancelled on cleanup —
  // so a host that passes an inline `onReady={() => …}` would otherwise cancel
  // and restart the boot on every one of its own renders, which for the ask
  // branch means restarting the 1.5 s detection wait and never sending at all.
  const onReadyRef = useRef(props.onReady);
  onReadyRef.current = props.onReady;
  /**
   * Is the boot's landing branch the one waiting on the ready signal? Written
   * by the boot effect, read by the effect further down: `markReady` is
   * idempotent, so this is only ever about which FACT the signal is waiting
   * for, never about firing it twice.
   *
   * STATE AND NOT A REF, deliberately. A ref would be written in a commit of
   * its own and the waiting effect would only run again if `recent` happened to
   * change afterwards — so a landing whose list had ALREADY answered by the
   * time the boot's async branch got here would never fire at all, and the host
   * would leave the pane covered. Setting state wakes the effect.
   */
  const [landingReady, setLandingReady] = useState(false);
  const markReady = useCallback(() => {
    if (readySent.current) return;
    readySent.current = true;
    onReadyRef.current?.();
  }, []);

  // ── boot (T:19178-19296, inventory 05 §G) ──────────────────────────────────
  //
  // THE LATCH IS PER CONTROLLER, not per mount (Bugbot, PR #1061). It was a
  // bare boolean, and `ChatBody` is not keyed on `file`: switching to a target
  // whose `agentDir` is already cached rebuilds the controller WITHOUT the
  // `agentDir === undefined` round trip that would have remounted this tree, so
  // the effect re-ran, found the latch set, and the new controller never got its
  // `openSession` / `resumeRun` / ask at all — a live conversation replaced by
  // an empty transcript that boots nothing.
  const bootedFor = useRef<object | null>(null);
  /** The boot's ONE dispatch — the ask, or the restore — the moment it reaches
   *  the controller. What makes the re-arm below safe: a boot cancelled before
   *  it dispatched can be run again, one that dispatched never is. */
  const bootDispatched = useRef(false);
  useEffect(() => {
    if (bootedFor.current === controller) return;
    bootedFor.current = controller;
    // A NEW CONTROLLER IS A NEW BOOT, so the dispatch guard re-arms with it —
    // it exists to keep ONE boot from dispatching twice, not to keep a second
    // target from dispatching at all.
    bootDispatched.current = false;
    // CANCELLED ON THE WAY OUT, and checked after every await. The boot is an
    // async walk over a controller and a piece of React state that both belong
    // to THIS mount: `agentDir` going back to `undefined` (a new `_file`), a
    // StrictMode remount, or a card leaving the wall all dispose the controller
    // under it, and a `sendMessage` / `openSession` / `resumeRun` landing after
    // that is a run started on a corpse — plus a `setEntered` / `markReady` for
    // a tree that is gone. The controller no-ops after `dispose()` as well
    // (run-controller.ts); this is the near end of the same belt.
    let cancelled = false;
    // THE IDS A HOST HANDED OVER, seeded once and here rather than in the render
    // body. On a memory store a render-body write is inert, but on the URL store
    // it is a HISTORY WRITE from a render React is free to discard (StrictMode, a
    // concurrent interruption, a Suspense retry) — and the `seeded` ref that
    // guarded it made the write unrepeatable when that happened. A committed
    // effect is where a write to the outside world belongs, and this one already
    // reads both keys. Seeded ONLY where the store has nothing, so any later
    // write — including the store's own URL — wins. (A memory store is also
    // seeded at construction by `ChatMount`; this is what makes a direct
    // `ClaudeChat` caller boot the same way.)
    const seed: Record<string, string | null> = {};
    if (props.initialSessionId && !params.get("session_id")) {
      seed.session_id = props.initialSessionId;
    }
    if (props.initialRunId && !params.get("run")) seed.run = props.initialRunId;
    // Bare `set`: at boot no gesture has happened, so the store takes the
    // coalesced replace path for it anyway (params/store.ts `sawGesture`) —
    // which is right, since this describes the state the page loaded IN.
    if (Object.keys(seed).length) params.set(seed);
    const sessionId = params.get("session_id") || "";
    const runId = params.get("run") || "";
    // The LATCH, not the prop — see `askRef`. This is what survives the host's
    // one-shot `nativeAsk` flipping to null inside the detection wait below.
    const ask = askRef.current;
    void (async () => {
      if (ask) {
        // A genuinely NEW conversation, and that has to be MADE true rather than
        // assumed: the shell's address bar is not guaranteed clean (closing the
        // sidebar never clears it) and `sendMessage` reads `session_id` again at
        // send time, so a leftover id would append this ask to a DIFFERENT
        // conversation while this mount showed an empty transcript. Disowned
        // with `history: "replace"` — a stale identifier is not a place anyone
        // navigated to, so disowning it must not buy a Back entry (T:19216).
        // `history: "replace"` spelled out, not inherited: a stale identifier is
        // not a place anyone navigated to, so disowning it must buy no entry.
        params.set({ session_id: null, run: null }, { history: "replace" });
        setEntered(true);
        await Promise.race([detected.current!.promise, sleep(ASK_DETECTION_TIMEOUT_MS)]);
        // THE ASK IS SPENT ONCE. The host took its pending ask and cleared it
        // before this mount existed, so a second dispatch is a second "Fix with
        // AI" run on the same prompt — which is what an unmount inside the
        // bounded wait above used to buy (the re-armed boot would find
        // `initialAsk` still on the props).
        if (cancelled || bootDispatched.current) return;
        bootDispatched.current = true;
        // SPENT ON THE LATCH, NOT ON THE BOOT. `bootDispatched` deliberately
        // re-arms with a new controller (`bootedFor` above), and the controller
        // memo's deps include `file` — so a host that swaps `file` in place for
        // an `agentDir` already in the resolver cache rebuilds the controller
        // WITHOUT remounting this tree, and a sticky `askRef` would take this
        // `if (ask)` branch a second time: `session_id`/`run` cleared (disowning
        // the conversation on screen) and the same "Fix with AI" prompt fired at
        // a DIFFERENT file. Cleared here, the moment the one dispatch is
        // committed, so a rebuilt controller has nothing to spend and falls
        // through to the restore branch — which is all a rebuild ever needed
        // (QA, PR #1061). The `entered` initializer reads `askRef` too, but only
        // once, before this effect ever runs.
        askRef.current = undefined;
        askSpent.current = true;
        // The composer went live the moment we entered chat, so the user can
        // have sent their own message inside that bounded wait. `sendMessage`
        // opens with `if (sending) return`, which here would drop the ask on the
        // floor with nowhere to read it back from — the host's pending ask was
        // taken and cleared before this ran. A follow-up is the existing answer
        // to "something wants to send while a turn is live" (T:19250-19266).
        if (controller.getState().status === "idle") await controller.sendMessage(ask);
        else await controller.sendFollowUp(ask);
        if (cancelled) return;
        markReady();
      } else if (sessionId || runId) {
        // A bare `run` (the frame died before the first poll saw a session id)
        // still means an in-flight conversation — enter chat and re-attach.
        if (bootDispatched.current) return;
        bootDispatched.current = true;
        setEntered(true);
        if (sessionId) {
          resetCardPolicy(cardPolicy);
          await controller.openSession(sessionId);
          if (cancelled) return;
        }
        // A bare `run` has nothing to restore, and a restored session is on
        // screen by now: either way this is the moment the host may uncover us.
        markReady();
        if (runId) await controller.resumeRun(runId);
        else if (sessionId) {
          // NO `run` ON THE URL and a session that may well be busy: ask the
          // session itself. `openSession` above already started this watch, so
          // this covers the one path that skips it — a boot handed a
          // `session_id` whose history load was refused or gated. Not awaited:
          // the watch is up to ~3 s and the host has already been told we are
          // ready (T:17506, feedback #25).
          void controller.adoptLiveRun(sessionId);
        }
      } else {
        // NOTHING TO RESTORE, BUT SOMETHING TO WAIT FOR (T:19282-19291, P4-14).
        //
        // T fires `markChatReady()` AFTER `await loadRecent()`, and the host
        // uncovers the pane on that signal — so firing it on this render shows
        // a landing whose one list is still a skeleton, which is the state the
        // read is about to replace. The wait is owned by the effect below,
        // which fires it on the first non-null `recent`; nothing is done here.
        setLandingReady(true);
      }
    })();
    return () => {
      cancelled = true;
      // RE-ARMED, but only from the window where nothing was dispatched. A
      // StrictMode remount runs cleanup between the two effect passes and the
      // guard above would otherwise make the second pass a no-op — the boot
      // would be cancelled and never redone, and a "Fix with AI" mount would sit
      // there with the prompt unsent. Once the boot HAS reached the controller
      // the latch stays: whatever it started is the one thing this mount does.
      if (!bootDispatched.current) bootedFor.current = null;
    };
    // `props.initialAsk` IS DELIBERATELY NOT A DEP. The ask is read through
    // `askRef`, which is stable for the mount, and a new ask is a new mount by
    // the host's key — so listing the prop bought nothing and cost the boot a
    // teardown whenever the host's one-shot derivation flipped it back to
    // `undefined` mid-wait (R4-4, `askRef`'s note). A real UNMOUNT still
    // cancels: `cancelled` is set by the same cleanup either way.
  }, [controller, params, markReady, cardPolicy]);

  // ── the typewriter (T:15063-15142, drained at T:16336) ─────────────────────
  //
  // The cadence lives in `protocol/typer.ts`; this is the paint side, and it
  // covers BOTH shapes of live bubble:
  //
  //   * a turn with no segments — the legacy flat body;
  //   * a turn whose last segment is prose — the growing tail, which moves
  //     along the turn as tool calls and more prose arrive (T:15668-15676).
  //
  // `streamingTailOf` makes that one decision (protocol/segments.ts) and returns
  // the attachment as a value, because React cannot hand a component the element
  // `T`'s `retarget` takes. Its `key` — `<turnKey>#<index>` — is precisely what
  // has to trigger a `retarget`: a new turn, or the tail moving. Growing text
  // alone is an `update`. A tail of `null` PARKS the typer, which draws nothing,
  // not even its cursor (T:15057-15062).
  const last = state.turns.length ? state.turns[state.turns.length - 1] : null;
  const liveTail = streamingTailOf(last);
  /** The streaming TURN, which outlives the tail moving inside it. */
  const liveTurnKey = last && last.role === "assistant" && last.streaming ? last.key : null;
  const typerKey = liveTail ? liveTail.key : null;
  const liveText = liveTail ? liveTail.text : "";
  const [typed, setTyped] = useState<{ key: string | null; text: string; cursor: boolean }>({
    key: null,
    text: "",
    cursor: false,
  });
  // A HIDDEN TAB GETS TIMERS, NOT FRAMES — both when the frame is scheduled and
  // when the tab hides with frames already in flight (ui/frameClock.ts has the
  // whole argument; the short version is that a swallowed frame leaves
  // `finish()` pending, latches `draining`, and stops every later turn being
  // typed at all).
  const [clock] = useState(() => createFrameClock());
  const [typer] = useState<Typer>(() =>
    createTyper({
      onFrame: (frame) => setTyped({ key: frame.key, text: frame.text, cursor: frame.cursor }),
      now: () => Date.now(),
      schedule: (cb) => clock.schedule(cb),
      cancel: (handle) => clock.cancel(handle),
    }),
  );
  useEffect(() => {
    if (typeof document === "undefined") return;
    // Only ever on the way OUT: coming back visible needs nothing, because the
    // rescued timers are still running and rAF resumes for the next frame.
    const onVisibility = () => {
      if (document.hidden) clock.rescueHidden();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [clock]);
  // The typer outlives no mount: a pending frame calling `setTyped` after
  // unmount is a React warning and a leaked loop per unmounted card.
  useEffect(() => () => typer.abort(), [typer]);

  // THE ORDER AT T:16336: drain the trailing prose, THEN one highlight/copy pass
  // over the whole finished transcript — the text segments and every `<pre>` a
  // tool chip added. The other way round highlights a reply still a frame behind.
  //
  // The drain is a STATE the typer is in, not a fire-and-forget: while it lasts
  // the typer stays pointed at the turn that just ended, so `retarget` below has
  // to stand down. The ref is what the effects read (they run in one flush,
  // before any re-render); the state is what the render reads.
  const draining = useRef<string | null>(null);
  const [drainingKey, setDrainingKey] = useState<string | null>(null);
  // Read through a ref, not a dependency: the turns array is replaced on every
  // 400 ms poll, and re-running this would re-drain a typer that has already
  // resolved and re-highlight the whole transcript with it.
  const turnsRef = useRef(state.turns);
  turnsRef.current = state.turns;
  const lastTurnKey = useRef<string | null>(null);
  useEffect(() => {
    const previous = lastTurnKey.current;
    lastTurnKey.current = liveTurnKey;
    if (!previous || previous === liveTurnKey) return;
    const finished = turnsRef.current.find((t) => t.key === previous);
    draining.current = previous;
    setDrainingKey(previous);
    // `finishedTailText` is T's `tailText || ""` for a segment turn and its flat
    // text for a legacy one — the authoritative final string, which is also what
    // makes the typer's own clamp fire when the poll shortened it (T:15075).
    void typer.finish(finishedTailText(finished)).then(() => {
      // ONE pass over the whole finished transcript, which is T's ordering
      // (T:16336) and not the same walk `MarkdownView` does per view: this is
      // the pass that catches the `<pre>`s a TOOL CHIP added, which never went
      // through the markdown funnel. Idempotent either way — `enhanceCodeBlocks`
      // skips anything already carrying `.hljs` / `.copybtn`.
      const log = rootRef.current?.querySelector(".chat-log");
      if (log) enhanceCodeBlocks(log);
      draining.current = null;
      setDrainingKey(null);
    });
  }, [liveTurnKey]);

  useEffect(() => {
    if (draining.current) return; // the typer is finishing the turn that ended
    typer.retarget(typerKey);
  }, [typer, typerKey, drainingKey]);
  useEffect(() => {
    if (draining.current || !typerKey) return;
    typer.update(liveText);
  }, [typer, typerKey, liveText, drainingKey]);

  /** What the transcript draws for the streaming row: the typer's slice and its
   *  caret, placed by the key the frame was drawn for. */
  const tail = useMemo<TranscriptTail | null>(() => {
    if (!typed.key) return null;
    const { turnKey, index } = parseTailKey(typed.key);
    // A frame from the turn the typer has already moved off is not this turn's.
    if (drainingKey ? turnKey !== drainingKey : typed.key !== typerKey) return null;
    return { turnKey, index, text: typed.text, cursor: typed.cursor };
  }, [typed, typerKey, drainingKey]);

  // ── Escape (T:15947-15978, inventory 04 §F) ────────────────────────────────
  //
  // ESCAPE HAS NO CLAIM ON A RUN, and used to have the last one. Its claimants —
  // the erase dialog, the kebab, a card's Other field, and (PR2/PR3) the shot
  // viewer and the annotation composer — are all UI-owned and stop the event
  // themselves, which is the right precedence: Escape closes the innermost thing
  // open. What is left over reaches the HOST, which is how TaskPeek closes.
  //
  // BOUND ON `document`, BUBBLE PHASE, EXACTLY AS T DOES (T:15956). React 18
  // delegates its synthetic keydown at the ROOT CONTAINER — an ancestor of
  // `.chat-root` — so a listener on the chat root runs BEFORE any React
  // handler inside it: the Other textarea's `preventDefault` +
  // `stopPropagation` (QuestionCard `fieldKeys`) had not run yet, and Esc in a
  // question card inside TaskPeek closed the whole modal and threw the typed
  // answer away. On `document` the delegated handler has already run and
  // stopped the event, which is the precedence T has.
  const onEscape = props.onEscape;
  useEffect(() => {
    if (typeof document === "undefined") return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape" || ev.defaultPrevented) return;
      // 1. THE SHOT VIEWER CLAIMS IT FIRST (T:15959), and it is a portalled
      //    dialog that closes itself — so this listener stands down entirely
      //    rather than claiming on its behalf: neither the annotation mode nor
      //    the host may act on a press the viewer is already answering.
      if (viewingRef.current) return;
      const root = rootRef.current;
      const target = ev.target as Node | null;
      // Only this chat's keystrokes: `document` is shared with the shell. A
      // press with nothing focused (body, the root element) is this chat's too —
      // it is how Esc reaches an armed mode whose pointer is over the app.
      const loose =
        !target || target === document.body || target === document.documentElement;
      if (!loose && !(root && root.contains(target))) return;
      // 2/3. the annotation composer, then the mode itself. `onEscape`
      //      preventDefaults exactly when it claimed, which is what makes the
      //      host's own hop below the LEFTOVER case rather than a second
      //      claimant (T:15968-15976, `escapeAction`).
      annRef.current?.onEscape(ev);
      if (ev.defaultPrevented) return;
      // 4. nothing of the chat's own was open, so the press reaches the host —
      //    which is how TaskPeek closes. Never a stop: Escape has no claim on a
      //    run (T:15947-15978).
      if (!onEscape || loose) return;
      onEscape();
      // Claimed, so the host chassis's own Esc does not close the same thing a
      // second time.
      ev.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onEscape]);

  // ── the narrow hard block (T:14652-14663) ──────────────────────────────────
  //
  // An open card is a HARD BLOCK: the run cannot continue without the user. The
  // transcript scrolls to it unconditionally; below the breakpoint the chat
  // column may not be the view on screen at all, so the VIEW moves too.
  // KEYED ON THE IDS, not the count — one card resolving while another opens in
  // the same poll leaves the count unchanged and used to leave the new hard
  // block in a column that is not on screen (PR #447). And never for a
  // NO-PANE target: there is no second view to be in, so T makes no write at
  // all (T:14661) and a bookmarked `paneview=preview` must not be rewritten.
  const openCards = openCardIds(state.permissions);
  useEffect(() => {
    if (openCards && !pane.noPane && narrowView.narrow && narrowView.view === "preview") {
      params.set({ paneview: "chat" });
    }
  }, [openCards, pane.noPane, narrowView.narrow, narrowView.view, params]);

  // Subscribed, not read once: the picker writes `leftmode` and the anchor is
  // spent by clearing `msg`, and both have to re-render this tree when they do.
  const leftMode = useChatParam(params, "leftmode");
  const msgAnchor = useChatParam(params, "msg") ?? null;

  // ── the composer, the topbar and the landing ───────────────────────────────
  const [sent, setSent] = useState<UserTurn | null>(null);
  // The landing's list only: a chat on screen has no lists, and the long-poll
  // behind it should not run for one that is not showing them (T:18339).
  /**
   * T's `leftLive` (T:13066-13071, P4-21). Set by the gesture that LEAVES a
   * chat, read by the landing's list subscription: the two extra `sessions`
   * looks after landing exist to cover the CLI's first transcript write, and
   * only a chat abandoned mid-turn has such a write to race. A cold landing
   * boot was spending them for nothing.
   *
   * State and not a ref, because the value has to reach the subscription's
   * render — and it is written in the same gesture that flips `inChat`, so it
   * is there on the paint the landing arrives on.
   *
   * Declared HERE, ahead of the list it feeds; `onBack` (which writes it) is
   * declared with the other gestures further down.
   */
  const [leftLive, setLeftLive] = useState(false);
  const recentSessions = useRecentSessions(
    inChat ? null : agentDir,
    file,
    undefined,
    leftLive,
  );
  /**
   * …AND THE CHATS IN THIS FOLDER THAT HAVE NOT RUN YET, in the same list.
   *
   * `sessions` lists TRANSCRIPTS, so a chat whose first message was queued is in
   * no list at all: nothing of it has run, nothing has been written, and the
   * landing showed no sign of a conversation the reader had typed into minutes
   * before. Its row is on `/api/tasks` instead, keyed `pending:<leader id>`
   * (`sched/waiting-chats`), and it is folded in here rather than given a section
   * of its own — a waiting chat is not a different kind of thing from one that
   * ran, it is the same conversation earlier, and it sorts by the same clock.
   *
   * ONLY ON THE LANDING and only under the flag: inside a chat the list is not
   * drawn, and flag off there is nothing to merge because nothing queues.
   */
  const waitingChats = useWaitingChats(
    !inChat && queueOn ? file : null,
    queueOn,
    // …AND ON THE RECENT LIST'S OWN TICK. A leader's transcript appearing is news
    // the sessions watch hears and the tasks read does not, and until it re-read
    // the same conversation was drawn twice — once as a transcript, once as the
    // waiting row it had stopped being (🔴 review 2026-09-12).
    recentSessions,
  );
  const recent = useMemo(
    () => mergeWaitingChats(recentSessions, waitingChats),
    [recentSessions, waitingChats],
  );
  /** ONE TRIP'S WORTH. T's `leftLive` is a local in its Back handler, so it is
   *  spent by the landing it was set for; here it has to be cleared by hand, or
   *  every later cold landing of this page's life would go on paying for the
   *  two write-covering reads. Cleared on the way INTO a chat, which is after
   *  the landing that used it and before the next Back that may set it again. */
  useEffect(() => {
    if (inChat) setLeftLive(false);
  }, [inChat]);
  /**
   * THE LANDING IS READY WHEN ITS LIST HAS ANSWERED (T:19282-19291, P4-14).
   *
   * `markReady` is what the host uncovers the pane on, and T fires it after
   * `await loadRecent()` for exactly that reason. Idempotent, so a boot that
   * already fired it on another branch pays nothing here.
   *
   * AND NEVER WAITS FOR A LIST THAT WILL NOT COME. Two roads reach that: a
   * target with no `agentDir` (which never subscribes), and a reader who enters
   * a chat before the first read lands — a recent row clicked on the skeleton,
   * or a deep link resolving late. `useRecentSessions` is handed a null
   * `agentDir` while in a chat, so `recent` would sit at `null` for ever and
   * the pane would stay covered for the life of the page. Entering a chat is
   * itself a reason to uncover it, and a target with no list has answered "no
   * list" — so both count.
   */
  useEffect(() => {
    if (!landingReady) return;
    if (recent !== null || inChat || !agentDir) markReady();
  }, [landingReady, recent, inChat, agentDir, markReady]);

  /**
   * WHAT THE TRAY PUTS ON THE WIRE, on both send roads: the `<pane-shot>` block,
   * the Read rules for the directories real-path attachments live in — granted
   * for the SESSION, not the turn (T:16657-16668) — and the receipts the turn
   * will wear. The tray is emptied by the same call that reads it, so a second
   * Enter cannot send the same pictures twice (T:16532).
   */
  const takeAttachments = useCallback(
    (lead: readonly Attachment[] = []): { opts: SendOptions; items: Attachment[] } => {
      const out = attach.take(lead);
      if (!out.receipts.length) return { opts: {}, items: [] };
      return {
        opts: { blocks: out.blocks, readDirs: out.readDirs, attachments: out.receipts },
        items: out.items,
      };
    },
    [attach],
  );

  /**
   * THE ANNOTATION HALF OF A SEND, in T's order (T:16503-16560):
   *
   *   1. the badge LETTERS, stamped onto the notes — the letter burned into the
   *      picture and the `label` on the wire have to be the same string, and an
   *      index can shift between now and any later read;
   *   2. ONE picture of the whole pane with every letter burned in at its spot,
   *      bounded and swallowing, because no screenshot is worth losing the
   *      user's message over;
   *   3. the picture folded back into the notes — which of them got a badge, and
   *      the sentence naming why each of the rest did not.
   *
   * Steps 1 and 3 are `overviewForSend`'s; the UPLOAD is this file's, because
   * the tray, the receipts and the failed-send road all live here.
   */
  const takeAnnotations = useCallback(async (): Promise<{
    block: string | null;
    notes: Annotation[];
    overview: Attachment | null;
  }> => {
    const a = annRef.current;
    if (!a) return { block: null, notes: [], overview: null };
    const { notes, overview } = await a.overviewForSend();
    if (!notes.length) return { block: null, notes: [], overview: null };
    let shot: Attachment | null = null;
    if (overview) {
      // A failed upload is a chip that says so, not a lost message: the same
      // degradation the capture itself already has (`attachOverview`).
      shot = await ATTACH_API.attachOverview(agentDir, overview.capture).catch(() => null);
    }
    return {
      block: formatAnnotations(notes as AnnotationWire[], pane.noun),
      notes,
      overview: shot,
    };
  }, [agentDir, pane.noun]);

  /**
   * BOTH SEND ROADS GO THROUGH HERE, and they MERGE rather than spread: T's wire
   * order is state, pane-shot, annotations, text, and `{ ...opts, ...mine }`
   * fixed no order at all — it replaced `opts.blocks` outright, which would have
   * silently dropped PR3's `<annotations>` and PR4's `<live-app-state>`
   * (ui/sendMerge.ts, protocol/wire.ts `composeBlocks`).
   *
   * The hand-back is registered under the very `Receipt[]` the controller is
   * handed, because that ARRAY IS THE KEY — a send whose bubble was dropped must
   * give back its own pictures and not another send's — and the merge may have
   * built a new array out of two owners' rows.
   */
  const beginSend = useCallback(
    async (opts: SendOptions): Promise<{ merged: SendOptions; done: (ok: boolean) => void }> => {
      // The notes FIRST, and awaited: their picture rides the same `<pane-shot>`
      // block the tray's own do, first in the list, so it has to be in hand
      // before the tray is emptied (T:16549).
      const notes = await takeAnnotations();
      const mine = takeAttachments(notes.overview ? [notes.overview] : []);
      // THE CALLER'S BLOCKS ARE NOT OURS TO DROP. `{ ...opts, blocks: [ours] }`
      // reads as "add the notes" and is a REPLACEMENT: any block the caller
      // brought — PR4's `<live-app-state>`, a walkthrough's own — vanished
      // silently, the message still going out and succeeding without it. Which
      // is the very bug `mergeSendOptions` exists to prevent, re-introduced one
      // line above the call to it (whole-stack review, PR #1074). ORDERED
      // union, through the same `composeBlocks`, so the wire reads state,
      // pane-shot, annotations whoever emitted which.
      const blocks = sendBlocks(opts.blocks, notes.block);
      const merged = mergeSendOptions({ ...opts, ...(blocks.length ? { blocks } : {}) }, mine.opts);
      const key = mine.items.length ? merged.attachments : undefined;
      if (key) inFlight.current.set(key, mine.items);
      // T:16064 — the send has taken them. Stamped BEFORE the request, because
      // the receipt and a failed send's roll-back both read the stamp.
      if (notes.notes.length) annRef.current?.markSent(notes.notes);
      // DELETED ON EVERY ROAD, not only the failed one: `onSendReturned` fires
      // inside the send, so by the time this runs the entry is either already
      // gone (the pictures went back to the tray) or is a send that LANDED — and
      // a map that only ever grows pins every attachment and blob URL the page
      // has ever sent for as long as it is open.
      //
      // STILL BEING THERE IS WHAT SAYS IT LANDED, which is why this reads before
      // it deletes: a send handed back to the tray was already removed by
      // `onSendReturned`, and its thumbnails are the chips the user is looking
      // at. A send that went out owns nothing on screen any more — the receipts
      // under its bubble are re-pointed at the copy on disk and the blob URLs go
      // (`settleReceipts`), so `newChat` or a file change can drop those turns
      // without pinning a full-pane Blob per picture for the life of the
      // document (Bugbot, PR #1064).
      return {
        merged,
        done: (ok) => {
          // THE NOTES' SIDE OF THE HAND-BACK FIRST. The run never launched, so
          // the agent saw none of it: the notes go back to pending, so the chips
          // return and Done can send them again (T:16086). The OVERVIEW is
          // revoked rather than handed back — it is the page's own picture of a
          // pane that has since moved on, and a retried send takes a fresh one
          // (T:16698-16708).
          if (!ok) {
            if (notes.notes.length) annRef.current?.unmarkSent(notes.notes);
            if (notes.overview) ATTACH_API.revoke(notes.overview);
          }
          // …then the tray's side, which judges landing by the ledger and not by
          // `ok`: a send handed back to the tray was already removed by
          // `onSendReturned`, so the read below is what says this one went out.
          if (!key) return;
          const landed = inFlight.current.get(key);
          if (!landed) return;
          inFlight.current.delete(key);
          const settled = settleReceipts(key, landed);
          if (!settled.spent.length) return;
          // THE STORE FIRST, the revoke A COMMIT LATER: the rows have to be
          // SHOWING the copy on disk — not merely told to — before the handles
          // they were showing stop resolving, and a store write is not a render
          // (`spentBlobs` above).
          controller.settleAttachments(key, settled.receipts);
          spentAlive.current = spentAlive.current.concat(settled.spent);
          setSpentTick((n) => n + 1);
        },
      };
    },
    [controller, takeAttachments, takeAnnotations],
  );

  /**
   * THE TRAY, COPIED WHERE A QUEUED RUN CAN READ IT (the project queue).
   *
   * A send the folder is too busy to take becomes a pending scheduler entry, and
   * that entry fires minutes later with whatever is written ON IT. A chat
   * attachment lives in the claude template's own shots dir on a 12 h TTL and
   * the backend refuses any `attachments` path outside `schedule.shots_dir()` —
   * so the path cannot travel and the BYTES have to, exactly as they do for
   * "Schedule this as a task" (`copyToTaskShots`, the same endpoint the task
   * form's own drop uses). Without this the message queued and fired without its
   * pictures while the tray went on showing them.
   *
   * BEFORE THE ADMISSION, which means a send into a FREE folder pays for copies
   * nobody reads: the answer decides whether they are needed and the answer
   * arrives too late to upload after it. That costs a few unused task-shots (the
   * ordinary send goes out on its ORIGINAL receipts — the bytes the bubble
   * shows) against the alternative of a queued message with no pictures, which
   * is a message that means something else when it finally runs.
   *
   * ONE FAILURE COSTS ONE ATTACHMENT and never the send: `copyToTaskShots` is
   * `allSettled` inside, and a wholesale failure answers `[]`.
   *
   * `lead` IS THE NOTES' OVERVIEW, and it rides in front for the reason it does
   * on the live wire (`beginSend`'s `takeAttachments(lead)`): the
   * `<annotations>` block tells the model to read "the attached overview
   * screenshot", so a queued entry carrying the words without the picture names
   * one that never travelled.
   */
  const carryForQueue = useCallback(
    async (lead: readonly Attachment[] = []): Promise<SchedAttachment[]> => {
      const tray = [...lead, ...attach.items];
      // Pending chips have no bytes yet, and an empty carry must not buy a round
      // trip in front of every send.
      if (!tray.some((a: Attachment) => !a.pending && !!a.view)) return [];
      return copyToTaskShots(tray).catch(() => []);
    },
    [attach],
  );

  /**
   * THE TRAY IS SPENT ON A QUEUED SEND, exactly as it is on one that ran.
   *
   * The server has the pictures now (`carryForQueue` put copies where the
   * scheduled run will read them) and the words are its entry. Leaving the chips
   * in the composer would mean the next message silently carried them a second
   * time — the same double-send `take()` exists to prevent — so the tray is
   * emptied by the same call, and the handles are released through the
   * commit-later queue because nothing on screen is drawing them any more.
   */
  const spendTrayForQueue = useCallback(() => {
    const mine = takeAttachments();
    if (!mine.items.length) return;
    spentAlive.current = spentAlive.current.concat(mine.items);
    setSpentTick((n) => n + 1);
  }, [takeAttachments]);

  /**
   * WHICH QUEUED MESSAGE A FOLLOW-UP JOINS, while this chat has no session.
   *
   * A new chat whose first message was queued is a task called
   * `pending:<entry id>` and NOTHING has run in it, so `sessionId` is still "".
   * A second line typed into the same composer would therefore be admitted as
   * another session-less send — which is what "open a brand-new task" means
   * everywhere else in this app — and the folder would gain a second task while
   * the reader watched one conversation. Naming the first entry (`follow_of`)
   * joins it instead; the rule and both its edges are in `sched/queue-leader`.
   */
  const leader = useQueuedLeader(state.sessionId ?? "");
  /**
   * THIS PANE WAS OPENED ON A CHAT THAT HAS NEVER RUN — `?queued=<entry id>`
   * (`platform/lib/queue.QUEUED_PARAM`).
   *
   * A waiting new chat has no session to name: nothing of it has run. Its name
   * is the LEADER ENTRY its first message is, which is also what the server
   * groups the whole conversation under (`pending:<leader id>`) — so the door
   * into it hands over that id and this pane takes it as its queue leader. From
   * there everything else is the road a chat that queued its own first message
   * already walks: `waitingFor` finds its rows, the `/api/tasks` row for
   * `pending:<id>` gives the header its number, and `adoptSession` swaps in the
   * real transcript the moment the leader runs.
   *
   * REMEMBERED IN AN EFFECT, READ DIRECTLY FOR THE RENDER. `leader` is a ref —
   * writing it during a render would answer differently depending on how many
   * times React ran that render — and the render below needs the id on the FIRST
   * paint, which is before any effect. So the two halves are separate: the paint
   * reads the param, the send path reads the ref the effect filled.
   *
   * A SESSION OUTRANKS IT, always. The moment this chat has one the param is
   * stale by construction, and it is cleared from the URL where the session is
   * adopted.
   */
  const queuedParam = params.get(QUEUED_PARAM) || "";
  useEffect(() => {
    if (!queuedParam || state.sessionId) return;
    leader.remember("", queuedParam);
  }, [queuedParam, state.sessionId, leader]);

  /**
   * THE ADMISSION SAID NEITHER YES NOR NO — and nothing is sent.
   *
   * Falling through to a spawn was this path's first shape, on the argument that
   * a new endpoint failing is no reason to swallow a message. But the failure
   * that actually happens is the server REFUSING this send (a wordless one it
   * will not queue, a 400 of any kind), and "the queue would not take it" is the
   * one answer that must not end in a run: the whole feature exists to stop a
   * second process starting in a folder somebody else is working in.
   *
   * So: no spawn, the words go back in the BOX they were typed in (`strand` —
   * the composer cleared it on the keystroke), the tray is untouched because
   * `beginSend` was never reached, and the reason lands in the chat's own error
   * slot — the same card a failed `start` writes, because from the reader's side
   * this is the same event: the message did not go.
   */
  const refuseQueuedSend = useCallback(
    (text: string, err: unknown) => {
      strand(text);
      const t = troubleFromError(err);
      controller.reportTrouble({ ...t, message: "This message was not sent: " + t.message });
    },
    [controller, strand],
  );

  /**
   * THE SEND WINDOW, SERIALIZED — one road, both kinds of send.
   *
   * `beginSend` is AWAITED (a round of notes has its pane photographed before
   * the wire can be composed) and nothing used to hold the door while it ran: a
   * second Enter started a second `beginSend`, the controller refused its
   * `sendMessage` out loud, and the hand-back that refusal emits was read by
   * the FIRST send — still out, about to land — as its own failure. It unmarked
   * notes the agent had already been given and revoked their overview (Bugbot,
   * PR #1074). Three things answer it, and all three are needed:
   *
   *   * THE LATCH (`sendBusy`, a ref the composer reads in the same tick it
   *     calls in here) closes the door from the first keystroke until THE RUN
   *     IS LIVE — the first status that is not `idle`, not the moment
   *     `sendMessage` hands back its promise. Taken-but-idle is a real window
   *     (one `start` round-trip), and a line typed inside it read as "no run
   *     yet": the composer sent it as a fresh message, the controller refused
   *     it, and the refusal dropped the bubble. Once the run IS live the door
   *     is open and the same keystroke goes to `sendFollowUp`, which is why the
   *     latch cannot simply be held for the whole turn. A send that never goes
   *     live releases it when its promise settles instead.
   *   * THE HAND-BACK IS KEYED (`sendId`), so "a send came back" can never be
   *     mistaken for "my send came back" again.
   *   * THE BUBBLE GOES UP FIRST (`postOptimisticUser`) and the controller's
   *     own bubble ADOPTS that row, so the typed words are never briefly
   *     nowhere — the composer clears its box on the keystroke, and the whole
   *     capture used to happen with an empty box and an empty transcript.
   */
  const dispatchSend = useCallback(
    (text: string, opts: SendOptions, followUp: boolean): void => {
      // The composer refuses this too, from the same ref — this is the guard
      // for every OTHER caller of the seat (✓ Done, the walkthrough).
      if (sendBusy.current) return;
      sendBusy.current = true;
      setSendLocked(true);
      const sendId = `s${++sendSeq.current}`;
      sendHolder.current = sendId;
      // A WORDLESS send (notes or pictures alone) posts no optimistic row: its
      // bubble is the markers `stripBlocks` builds out of the composed wire,
      // and only the controller can write those.
      const optimisticKey = text ? controller.postOptimisticUser(text) : "";
      void (async () => {
        let taken = false;
        try {
          // ---- ADMISSION, AHEAD OF EVERYTHING ELSE (the project queue) ------
          //
          // BEFORE `beginSend`, not after, and that ordering is the whole
          // reason this is safe to add here: `beginSend` PHOTOGRAPHS the pane,
          // empties the attachment tray and stamps the round of notes as sent.
          // A message that turns out to be queued never took any of it — the
          // server stored the words as a pending entry and a picture of a pane
          // taken now would be a picture of a pane from before the run that
          // eventually answers it. So the ask happens while nothing has been
          // spent, and a queued send simply never calls it.
          //
          // …and BEFORE `start`/`send`, which is the point of the feature: the
          // one thing the queue exists to prevent is two runs in one folder,
          // and spawning first would open exactly that window for the length of
          // a round trip.
          //
          // AND THE PICTURES GO WITH THE WORDS. A queued send fires minutes
          // later out of the scheduler, so whatever is not ON the entry is not
          // in the message that eventually runs — see `carryForQueue`.
          //
          // A FAILED ADMISSION SENDS NOTHING. This used to fall through to a
          // spawn, on the argument that a new endpoint failing is no reason to
          // swallow a typed message — but the failure that actually happens is
          // the server REFUSING this send (a 400 on a wordless one, a flag the
          // server does not have), and falling through turns every such refusal
          // into the exact second run in a busy folder the queue exists to
          // prevent. So only a clean `run: true` sends; anything else is
          // `refuseQueuedSend` — words back in the box, reason on the card.
          if (queueEnabled()) {
            // READ ONCE, and read HERE: the session can arrive while the copies
            // below are uploading, and a body whose `session_id` and
            // `follow_of` were asked a round trip apart could carry both — a
            // message addressed to a conversation AND filed under the task it
            // predates.
            const live = controller.getState();
            /**
             * THE FRESHEST SESSION ID THIS PANE CARRIES, which is not always the
             * one in controller state.
             *
             * The URL is the pane's other record of which conversation is on
             * screen — `openSession` writes it, the boot effect reads it, and
             * `newChat` clears it — and in the window right after a first reply
             * it can be ahead of `state.sessionId`. An admission that names
             * neither is an ANONYMOUS one, and the server then has nothing to
             * recognise its own caller by (below).
             */
            const sid = live.sessionId || params.get("session_id") || "";
            /**
             * …AND THE RUN THIS CHAT ALREADY HAS IN FLIGHT, read in the same
             * breath as the session for the same reason.
             *
             * It is what tells the server that the thing holding this folder is
             * THIS page. A session id cannot say it before the first turn has
             * opened one, so a second line typed into a brand-new chat that is
             * still starting queued behind its own run — the reader watched
             * their own message wait for themselves (Akshil, browser QA
             * 2026-09-12). `runId` is minted by `POST /api/run` and is live from
             * the first keystroke of the first turn (`api.admitQueueSend`).
             *
             * OR THE LAST RUN THIS CHAT HAD, and that was round two's finding.
             * `state.runId` is cleared the instant a turn ends, while the host
             * is still tearing the run down and the registry still reads busy —
             * so "hello" → reply → "second" typed straight away was admitted
             * with no run id AND (see `sid`) sometimes no session id either, and
             * the server, seeing an anonymous caller against its own live run,
             * queued the reader behind themselves: `Queued · #1 in line · behind
             * a run in this folder` (Akshil, browser QA 2026-09-12).
             * `lastRunId` outlives the turn and dies with the CONVERSATION,
             * which is the lifetime this question actually has.
             */
            const rid = live.runId || live.lastRunId || "";
            const follow = leader.followOf(sid);
            /**
             * AND THE NOTES GO WITH THE WORDS, for the pictures' reason.
             *
             * A queued send is a scheduler entry that fires minutes later with
             * whatever is written ON IT, and a round of pane notes left in the
             * tray is half the message: the reader drew on the app, pressed
             * Enter, and what eventually reached the agent was the typed line
             * alone. Worse than missing — the notes stayed UNMARKED, so the
             * next send into a free folder silently took somebody else's round
             * (`takeAnnotations` → `markSent`).
             *
             * Taken exactly as `beginSend` takes them (the badge letters, one
             * picture of the pane with those letters burned in, the block built
             * out of both) and composed into the admitted `message` through
             * `composeOutgoing` — the same call the live send's wire goes
             * through — so the entry carries the text this send would have sent.
             *
             * NOT MARKED YET, because the verdict decides who owns them:
             * `run: false` stamps them below (nothing may take them again), and
             * a `run: true` leaves them pending for `beginSend`, which takes its
             * own round the ordinary way. That is the same price `carryForQueue`
             * pays one line down — a send into a free folder buys a capture
             * nobody reads — and it is paid for the same reason: the answer
             * arrives too late to take anything after it.
             */
            const notes = await takeAnnotations();
            const carried = await carryForQueue(notes.overview ? [notes.overview] : []);
            /**
             * THE BADGED PICTURE, PUT DOWN — on every road out of here, and it
             * is only ever the picture.
             *
             * Queued: its COPY is on the entry (`carryForQueue` led with it) and
             * nothing on screen draws the original, so holding the blob would
             * pin a full-pane image for the life of the document (Bugbot, PR
             * #1064's rule). Admitted or refused: it is this page's photograph of
             * a pane that has since moved on, and the send that actually goes
             * takes a fresh one — exactly what `beginSend`'s `done(false)` does
             * with one.
             *
             * The NOTES are a different question and are not touched here: they
             * are marked only where the entry took them (below), so on every
             * other road their chips stand.
             */
            const putDownQueuedShot = () => {
              if (notes.overview) ATTACH_API.revoke(notes.overview);
            };
            let verdict: Awaited<ReturnType<typeof admitQueueSend>> | null = null;
            try {
              verdict = await admitQueueSend({
                project: file || "",
                session_id: sid,
                // EMPTY IS SENT, not withheld. A wordless send (pictures alone)
                // is a send like any other and has to be admitted like one;
                // whether an empty message with attachments may be queued is the
                // server's call, and a refusal is an answer this road already
                // knows how to show. A send carrying NOTES is not one of those:
                // its `<annotations>` block is the message, composed in here the
                // way the live wire composes it.
                message: composeOutgoing(text, [notes.block]),
                ...(opts.model ? { model: opts.model } : {}),
                ...(opts.effort ? { effort: opts.effort } : {}),
                ...(opts.permission ? { permission_mode: opts.permission } : {}),
                ...(carried.length
                  ? { images: carried.map((a) => a.path), attachments: carried }
                  : {}),
                // THE LEADER, and only while there is no session to name
                // instead — `followOf` is asked with the very id that went into
                // `session_id` above, so the two halves of the body can never
                // disagree about what this message is addressed to.
                ...(follow ? { follow_of: follow } : {}),
                // THE LIVE RUN, so the holder can be recognised as this chat's
                // own before a session id exists to say it.
                ...(rid ? { run_id: rid } : {}),
              });
            } catch (err) {
              putDownQueuedShot();
              refuseQueuedSend(text, err);
              return;
            }
            // A shape this build does not understand is not a yes. Read off the
            // wire rather than off the type: the type is what the server is
            // MEANT to answer.
            const run = (verdict as { run?: unknown } | null)?.run;
            if (run !== true && run !== false) {
              putDownQueuedShot();
              refuseQueuedSend(text, new Error("the queue gave no answer."));
              return;
            }
            if (verdict && verdict.run === false) {
              // THE WORDS STAY AND THE ROW MOVES — from the transcript, which
              // cannot keep them, to the chip, which can.
              //
              // This used to keep the optimistic bubble (`taken = true`), on the
              // rule that a bubble vanishing on Enter reads as a message that
              // was lost. That rule is right and the bubble was the wrong home
              // for it: the optimistic row lives only in the live document, and
              // both of the things that replace that document happen to a queued
              // send routinely — the standing watch's `refreshHistory` (a full
              // `turns` replace from the JSONL, four times a minute) and the
              // adoption of the leader's session (`adoptSession` → `openSession`
              // below). A message the scheduler has not sent is in no file, so
              // either one wiped the words and left the chip talking about
              // nothing (Bugbot, PR #1124).
              //
              // So the chip carries the text (`QueuedSend.text`) and draws it in
              // the transcript's own user bubble, directly under the log and in
              // the same column — and the optimistic row goes, because two
              // copies of one message is the other way to get this wrong.
              // `taken` stays false, which is exactly what the `finally` reads
              // to drop it: the same handover a send that reached the controller
              // makes, one paint, no gap where the words are nowhere.
              //
              // …and the tray is spent, because the entry now carries its own
              // copies of those pictures (`spendTrayForQueue`).
              spendTrayForQueue();
              // THE NOTES ARE SPENT TOO, on the same argument and for a sharper
              // reason: their words are on the entry now, so leaving them
              // pending would hand this round to the NEXT send — the very
              // double-take `markSent` exists to stop, and the one a queued send
              // used to cause every time. Stamped exactly where `beginSend`
              // stamps them: after the request the words went out on.
              if (notes.notes.length) annRef.current?.markSent(notes.notes);
              putDownQueuedShot();
              const entryId = String(verdict.entry?.id ?? "");
              // …and if this chat still has no session, THIS is the entry every
              // later message in it joins (`sched/queue-leader`, which ignores
              // the call when a leader is already remembered or a session has
              // arrived).
              leader.remember(sid, entryId);
              // THE ROW IS THE SERVER'S; THIS IS ONLY THE FIRST PAINT OF IT.
              // The entry exists now, so the next schedule tick will list it and
              // the chat will draw it from that (`waitingRows`). The seed covers
              // the up-to-fifteen-seconds before that tick, in which a message
              // the reader just sent would otherwise be nowhere on screen — and
              // it carries the TYPED line, so the row does not rewrite itself
              // when the server's copy (the composed message, markers and all)
              // takes over. Same entry id, so it is never two rows.
              setWaitingSeeds((cur) => [
                ...cur,
                { entryId, text, due: String(verdict?.entry?.due ?? "") },
              ]);
              // …AND WHAT IS IN FRONT, for the paint before this conversation's
              // own `/api/tasks` row has been read. One answer for the whole
              // chat, because a folder is held by one task and every message in
              // this line is behind the same thing.
              setAdmitAhead({
                status: "queued",
                queue_position: verdict.position,
                queue_ahead: verdict.ahead,
                queue_ahead_title: verdict.ahead_title,
                queue_ahead_session: verdict.ahead_session ?? "",
                queue_ahead_target: verdict.ahead_target ?? "",
                queue_ahead_key: verdict.ahead_key ?? "",
              });
              // …AND THE NUMBER THIS CONVERSATION IS NOW CALLED. The entry IS
              // the task, so the answer that queued the message can name it —
              // and the header wore nothing at all until a listing landed.
              if (verdict.task_id) setAdmitTaskId(String(verdict.task_id));
              return;
            }
            // `run: true` — the ordinary road, on the ORIGINAL receipts: the
            // copies made above are task-shots nobody will read, which is the
            // price of asking before spending (see `carryForQueue`). The round
            // of notes is put down the same way: still pending, still chipped,
            // and `beginSend` below takes it the ordinary way.
            putDownQueuedShot();
          }
          const { merged, done } = await beginSend(opts);
          const wire: SendOptions = {
            ...merged,
            sendId,
            ...(optimisticKey ? { optimisticKey } : {}),
          };
          let ok = true;
          try {
            const sent = followUp
              ? controller.sendFollowUp(text, wire)
              : controller.sendMessage(text, wire);
            // The controller has taken it: its own `sending` gate is set and
            // its bubble is up, both before its first await. But TAKEN IS NOT
            // LIVE — the status the composer routes on stays `idle` until
            // `pollLoop` reports, and a line typed in between would be sent as
            // a fresh message the controller then refuses. So the door stays
            // shut until the run is live (the status effect above); a follow-up
            // dispatched into a run that already is opens it right here.
            taken = true;
            if (controller.getState().status === "idle") liveWait.current = sendId;
            else releaseSend(sendId);
            await sent;
          } catch {
            ok = false;
          }
          // MY send, asked of my own id — and spent, whichever way it went.
          done(ok && !returnedSends.current.delete(sendId));
        } finally {
          // THE OTHER END OF THE LATCH. `beginSend` itself threw, so nothing
          // was ever dispatched (the row has nothing behind it) — or the send
          // is over: a turn that ran to its end, and equally one the controller
          // refused or a `start` that failed, neither of which ever reported a
          // live status for the effect above to read. Owned by `sendId`, so a
          // turn ending cannot open the door on a LATER send's window.
          if (!taken && optimisticKey) controller.dropOptimisticUser(optimisticKey);
          releaseSend(sendId);
        }
      })();
    },
    [
      controller,
      beginSend,
      releaseSend,
      carryForQueue,
      takeAnnotations,
      refuseQueuedSend,
      spendTrayForQueue,
      leader,
      params,
    ],
  );

  const onSend = useCallback(
    (text: string, opts: SendOptions) => {
      // OPTIMISTIC, and synchronously BEFORE the dispatch — exactly where T puts
      // it (`enterChat()` in the landing form's own `onsubmit`, T:17935, one line
      // ahead of its `sendMessage(message)`). `inChat` is `entered ||
      // !!state.sessionId` and nothing on this path set `entered`, so the
      // landing stayed up until a POLL reported a session id: one `start`
      // round-trip plus a 400 ms lap, which is the 1-2 s stall the QA measured
      // against :1777.
      //
      // A REFUSED START DOES NOT COME BACK HERE, and T's `enterChat()` is
      // equally one-way: its rollback (T:16693-16720) drops the bubble and posts
      // the failure INTO the chat, where the reader stays to read it. Back is
      // the way out, the same as for a run that started and then failed.
      setEntered(true);
      dispatchSend(text, opts, false);
    },
    [dispatchSend],
  );
  const onFollowUp = useCallback(
    (text: string) => {
      dispatchSend(
        text,
        {
          model: defaults.model,
          effort: defaults.effort,
          permission: defaults.permission,
        },
        true,
      );
    },
    [dispatchSend, defaults.model, defaults.effort, defaults.permission],
  );
  const onStop = useCallback(() => void controller.stopRun(), [controller]);
  /**
   * `sched.reset` — declared HERE, ahead of the hook that fills it, because
   * `onBack` is one of its two callers and is itself declared before the PR4
   * block below. A ref rather than the value: both callers are gestures, so
   * they read it when pressed and neither needs re-binding for a new identity.
   */
  const schedReset = useRef<() => void>(() => {});
  const onBack = useCallback(() => {
    // A fresh transcript is a fresh card policy: an override from the
    // conversation that WAS on screen must not leak a card open in one the user
    // has never touched (ui/cardPolicy.ts).
    resetCardPolicy(cardPolicy);
    // WAS THERE A TURN IN FLIGHT? Asked before `newChat` empties the state that
    // knows. A queued send counts: its transcript write has not happened yet
    // either — and so does a send that is INSIDE the `sending` gate with no run
    // id yet, which is the exact window these extra looks exist to cover (a
    // brand-new session's transcript appears only once the CLI has written its
    // first rows). `isBusy()` is `activeRun || sending`, which is the half
    // `ChatState` cannot see (Bugbot, this batch).
    const s = controller.getState();
    setLeftLive(
      controller.isBusy() ||
        s.status === "running" ||
        !!s.runId ||
        s.queued.length > 0,
    );
    // AND A FRESH SCHEDULE. `newChat` puts `transcriptGen` back to 0, and the
    // reset effect below skips gen 0 by construction (a mount must not
    // re-baseline the poller) — so Back alone left the block that belonged to
    // the conversation just closed standing over the landing composer until the
    // next 15 s tick. Called directly, the way `openSession` gets it from the
    // generation bump (Bugbot PR #1075).
    schedReset.current();
    controller.newChat();
    setEntered(false);
    // AND THE STRANDED TEXT GOES WITH THE CONVERSATION IT WAS TYPED IN. It was
    // never cleared, and `card.restore` reaches the LANDING's composer as well
    // as the chat's — so words typed for session A sat one Enter away from
    // opening a brand-new conversation, and the per-instance delivery ledger
    // (`Composer`'s `delivered`) restarts on the Home/chat remount, so every
    // Back appended them again.
    setStranded(null);
    // …AND SO DO THE QUEUED CHIPS AND THE LEADER THEY NAME. Both are memories of
    // the messages that were ON SCREEN: the chips sit under a transcript that is
    // being emptied, and the leader would file the next chat's first line under
    // a task it has nothing to do with. The leader especially, now that a chat
    // ADOPTS the session its leader's run opens — left standing it would pull
    // the reader back into the conversation they just left, on the next poll.
    // The entries themselves are untouched; the Tasks page is where they live.
    setWaitingSeeds([]);
    setAdmitAhead(null);
    setAdmitTaskId("");
    // …and the deletions with them: both are memories of the rows that were on
    // this screen, and the next conversation's waiting messages are its own.
    setDroppedEntries(NO_DROPPED);
    leader.forget();
    // AND THE DOOR THIS PANE CAME IN BY. `?queued=` names the conversation that
    // is being left; carried into the next one it would re-adopt the leader the
    // line above just forgot, on the first render after it.
    params.set({ [QUEUED_PARAM]: null }, { history: "replace" });
  }, [controller, cardPolicy, leader, params]);
  const onOpenSession = useCallback(
    (sessionId: string) => {
      resetCardPolicy(cardPolicy);
      setEntered(true);
      // Same rule as Back: a hand-back belongs to the conversation it was typed
      // in, and this is a different one — and so do the queued chips, which sit
      // under a transcript that is about to be replaced.
      setStranded(null);
      setWaitingSeeds([]);
      setAdmitAhead(null);
      setAdmitTaskId("");
      setDroppedEntries(NO_DROPPED);
      params.set({ [QUEUED_PARAM]: null }, { history: "replace" });
      void controller.openSession(sessionId);
    },
    [controller, cardPolicy, params],
  );

  // T:16714 — one `scrollBottom()` after the turn has settled, which T runs
  // after the awaited pollLoop. `status` leaving "running" is that moment.
  const settled = state.status === "idle";
  const wasRunning = useRef(false);
  useEffect(() => {
    if (!settled) {
      wasRunning.current = true;
      return;
    }
    if (!wasRunning.current) return;
    wasRunning.current = false;
    const log = rootRef.current?.querySelector(".chat-logwrap");
    if (log) log.scrollTop = log.scrollHeight;
  }, [settled]);

  // AND UNCONDITIONALLY AFTER A REPAIR (T:17851, P4-10) — the rule, and why it
  // has to be unconditional, live in `useRepairScroll`. Extracted so the
  // renderer half of P4-10 has a suite of its own (batch review, test gap 1):
  // the controller bumps the nonce, and this is what the nonce is FOR.
  useRepairScroll(state.repaired, rootRef);

  const controls = useMemo(
    () => ({
      model: defaults.model,
      effort: defaults.effort,
      permission: defaults.permission,
      setModel: defaults.setModel,
      setEffort: defaults.setEffort,
      setPermission: defaults.setPermission,
    }),
    [defaults],
  );

  const actions = useMemo(
    () => ({
      decidePermission: controller.decidePermission,
      answerQuestion: controller.answerQuestion,
      decidePlan: controller.decidePlan,
      dismissCard: controller.dismissCard,
      stopRun: controller.stopRun,
    }),
    [controller],
  );

  const name = file ? file.split(/[\\/]/).filter(Boolean).pop() || file : "Claude";
  const running =
    state.status === "running" || state.status === "starting" || state.status === "stopping";
  // The picker moves into the shared strip below the breakpoint: there is no
  // persistent left column to hang a bar on (`pickerHost`).
  const host = pickerHost(narrowView.narrow, pane.noPane, pane.decision?.leftModes.length ?? 0);
  const paneShown = !chatOnly && !pane.noPane;
  // IS THERE AN ANNOTATE TARGET — the one question the strip's visibility has
  // ever asked, and it is NOT a question about our layout.
  //
  // T's `annPollTarget` (T:8449-8465) resolves `annFrame` to the pane iframe in
  // the split layout OR, in CHAT_ONLY, to the host's marked frame
  // (`annMarkedFrame`, T:6113) — polled, because the mark moves with the host's
  // own mode switcher — and sets `hidden` on the three buttons off that one
  // fact. `#anncta:not(:has(#annbtn:not([hidden])))` then collapses the group.
  // So the ONLY state that hides them is "nothing to act on": a folder listing,
  // or a standalone mount with no pane.
  //
  // Native read `!chatOnly` instead, which is a question about the LAYOUT, and
  // so the hosted `?_side=claude` sidebar — where the app is on screen in the
  // middle column and `annotateTarget` hands us its frame — lost the whole row,
  // on the landing and in the transcript alike, while `:1777` kept all three
  // buttons on the same URL (measured: legacy `#anncta` 312x26 with
  // viewshot/annbtn/annrec all `hidden:false`; native rendered no `.c-anncta`
  // at all).
  //
  // `hostPane` is the polled answer to the second half and already lives above
  // (HOST_PANE_POLL_MS, T's tick), so this is the same OR that `hasPane` makes.
  const annTarget = paneShown || hostPane;
  // THE STRIP ITSELF IS NOT CONDITIONAL any more (P2-1): T's `#anntools` is
  // static markup and it holds the way out and the ⋮ as well as the three
  // preview seats, so the row stands in every layout and `annTarget` decides
  // only whether `AnnStrip` draws anything inside it. The view toggle and the
  // picker stay narrow-only in there (`pickerHost`).
  // PR3: the host pane's seats are gated on `ann.capable` at the `AnnStrip`
  // call site — the strip itself stands, the seats go ("absent beats dead").
  // T:7566 `annFitStrip` — the strip's words collapse to icons only when they
  // MEASURABLY do not fit (QA #2: at 1280px with a pane the chat column is
  // ~308px and the three full labels overflowed it by 8px).
  const stripRef = useFitStrip();
  // Where focus goes when the erase confirm closes, whichever way it closed
  // (T:13293, 13319-13321). It lived in the top bar with the menu; the menu is
  // in the shared strip now, so its seat is too.
  const kebabBtn = useRef<HTMLElement | null>(null);
  // The page must not stay on a transcript that no longer exists
  // (T:13348-13366); the menu has already dropped every cache keyed by it.
  const onErased = useCallback(() => {
    // THE MODE GOES BEFORE THE NAVIGATION (T:13371-13375), and T's own comment
    // says why: back-to-chats "REFUSES while a comment mode holds the reader
    // here (annNavLocked) — and a page must not stay on a transcript that no
    // longer exists (Bugbot, PR #1049). The notes were about a conversation
    // that is gone, so the mode is dropped first, the way its own discard path
    // drops it."
    //
    // T spells this as three lines — `if (annOn) annSetMode(false);
    // annBusyHold = false; annNavLock();` — and `machine.set(false)` is all
    // three here: it drops the settle's hold on its first line ("a disarm,
    // whoever asks […] releases the settle's hold") and ends in
    // `deps.onLock(locked())`, which is `annNavLock`. Unconditional, unlike
    // T's `if (annOn)`, because the lock can also be held by a settle the
    // reader has already left, and `set(false)` is the one door that clears
    // both.
    //
    // Without it, deleting the task left the mode running against a session
    // that no longer existed — pins over a pane whose conversation is gone,
    // and a nav lock refusing the very navigation that was meant to follow.
    ann.setMode(false);
    onBack();
  }, [ann, onBack]);

  // MEMOIZED, like the two callbacks below it: a fresh object per render defeats
  // every `React.memo` in the tree it is handed to, and this one is handed to
  // the composer AND the landing card.
  // `card.back` is a snapshot of the address bar, so the memo has to see the
  // address bar move: without this the memoized object would hand the composer
  // a `back` from an earlier URL.
  const [urlTick, setUrlTick] = useState(0);
  useEffect(() => params.onChange(() => setUrlTick((n) => n + 1)), [params]);
  // ⌘V. `preventDefault` ONLY when a picture was actually found: this listener
  // sits on a box the user types in all day, and stealing an ordinary paste
  // would be a far worse bug than never having had the feature. A paste carrying
  // both an image and its alt text attaches the picture and drops the words,
  // which is the same rule (T:11719-11728).
  const onPaste = useCallback(
    (ev: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const picks = ATTACH_API.filesFromPaste(ev);
      if (!picks.length) return;
      ev.preventDefault();
      void attach.addFiles(picks);
    },
    // THE ONE MEMBER IT CALLS, not the whole hook object (D7). `useAttachments`
    // returns a fresh literal every render, so `[attach]` made this callback —
    // and through it the `card` memo that lists it as a dep — recompute on every
    // render, which is what MASKED the missing `attach.capturing` dep below. The
    // dep list has to be the truth about what a memo reads, not a coincidence
    // that happens to cover it. (`onSend`/`onFollowUp` still move every render,
    // through `dispatchSend`, so `card` is not yet a memo that holds — which is
    // why no test can fail on the missing dep alone. One mask at a time.)
    [attach.addFiles],
  );

  /**
   * DRAG AND DROP (T:11745-11790). Four listeners, and the class is driven by a
   * COUNTER rather than toggled: dragenter/dragleave fire for every child
   * element the pointer crosses, so a plain toggle flickers the highlight off
   * the moment the cursor moves over a chip. The counter is RESET on drop,
   * because a drop delivers no leave for the enters that preceded it.
   *
   * `dragover` MUST preventDefault or the drop event never fires — the default
   * action is "refuse the drag" — and both it and `dragenter` answer only for a
   * drag that actually carries an attachment, so dragging TEXT out of the log
   * into the box keeps the browser's own insert behaviour.
   */
  const [dropping, setDropping] = useState(false);
  const dragDepth = useRef(0);
  const onDragEnter = useCallback((ev: React.DragEvent) => {
    if (!ATTACH_API.dragHasAttachment(ev.dataTransfer)) return;
    ev.preventDefault();
    dragDepth.current += 1;
    setDropping(true);
  }, []);
  const onDragOver = useCallback((ev: React.DragEvent) => {
    if (!ATTACH_API.dragHasAttachment(ev.dataTransfer)) return;
    ev.preventDefault();
    // What makes the cursor say "copy" rather than show the forbidden sign.
    ev.dataTransfer.dropEffect = "copy";
  }, []);
  // NO `dragHasAttachment` GUARD HERE, unlike its three neighbours — AND
  // UNLIKE T, which does guard this one (T:11771). A DELIBERATE divergence and
  // the better answer: several engines expose no `types` at all on
  // `dragleave` — it is the one drag event whose DataTransfer is deliberately
  // protected — so a guarded leave never fired, the depth never came back down,
  // and the accent ring stayed on the column until the next drop. An extra
  // decrement is free: the counter floors at zero and only an ENTER that carried
  // an attachment ever raised it.
  const onDragLeave = useCallback(() => {
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (!dragDepth.current) setDropping(false);
  }, []);
  const onDrop = useCallback(
    (ev: React.DragEvent) => {
      if (!ATTACH_API.dragHasAttachment(ev.dataTransfer)) return;
      ev.preventDefault();
      dragDepth.current = 0;
      setDropping(false);
      // REAL PATHS FIRST. Both payloads can ride one drag (a source that sets
      // the path type may set `files` too), and when they do they describe the
      // same file — one by where it lives, one by a copy of its bytes. The path
      // is the better answer for every reader downstream: no upload, no 12-hour
      // expiry, and an edit the agent makes lands on the user's own file
      // (T:11777-11787).
      const paths = ATTACH_API.pathsFromDrop(ev.dataTransfer);
      if (paths.length) void attach.addPaths(paths);
      else void attach.addFiles(Array.from(ev.dataTransfer.files || []));
    },
    [attach],
  );

  /** The viewer's Discard: the one place the user can judge that this is the
   *  wrong picture (T:10961). Only ever offered for a PENDING shot, so the tray
   *  is where it is looked up. */
  const onDiscardShot = useCallback(
    (shot: Viewable) => {
      // BY ID. Every refusal has `view: null`, so the old `view === shot.view &&
      // kind === shot.kind` match could not tell two failed pictures apart:
      // Discard on the second one removed the first (`Viewable.id`, PR2 review).
      const att = attach.items.find((a: Attachment) => a.id === shot.id);
      if (att) attach.remove(att);
    },
    [attach],
  );

  /**
   * THE VIEWER READS THE LIVE ROW, not the snapshot it was opened from (D8).
   *
   * `attachApi.liveViewable` is the rule and carries the why; what is this
   * file's own is WHERE the sent rows are: the receipts hang off the user turns
   * in the store, which is the copy `settleAttachments` rewrites.
   */
  const liveViewing = useMemo<Viewable | null>(() => {
    if (!viewing || !viewing.id) return viewing;
    const sent: Receipt[] = [];
    for (const turn of state.turns) {
      if (turn.role === "user" && turn.attachments) sent.push(...turn.attachments);
    }
    return liveViewable(viewing, attach.items, sent);
  }, [viewing, attach.items, state.turns]);

  // ── PR4: scheduled runs, the standing watch, the artifact strip ───────────

  const followBottom = useCallback(() => {
    transcriptFollow.current?.();
  }, []);

  /**
   * THE ENTRY THIS CHAT'S MESSAGES ARE GROUPED UNDER while it has no session of
   * its own (`sched/queue-leader`) — read here, for the render, as well as in the
   * send window where it is written.
   *
   * `useSchedule` takes it because three things hang off it and all three are
   * that hook's: which entries are this conversation's (`waitingFor`), whether
   * there is a card at all, and which `/api/tasks` row to read (a queued new chat
   * is named `pending:<leader>`, never after the entry at the front of its line).
   */
  const leaderId = leader.peek() || (state.sessionId ? "" : queuedParam);

  const sched = useSchedule({
    controller,
    file,
    sessionId: state.sessionId ?? "",
    leaderId,
    inChat,
    navLocked: ann.locked,
    followBottom,
    onNavigate,
    // T:17437 — `history: "replace"`: a fired scheduled run is not a place
    // anyone navigated to, so re-attaching from it must buy no Back entry.
    setRunParam: (runId) => params.set({ run: runId }, { history: "replace" }),
  });
  /** Stable across renders (`useSchedule` memoises it on the watcher), so the
   *  delete below may depend on it by name. */
  const schedRefresh = sched.refresh;

  /**
   * A SEED COMES DOWN WHEN ITS ENTRY GOES — and not one poll sooner.
   *
   * "The entry fired" is exactly "it is no longer pending", and it is also what a
   * cancel from the Tasks page looks like. `pendingIds` is the schedule poll's
   * UNFILTERED pending set rather than `waitingHere`, because the chat that most
   * needs this is a brand-new one with no session yet, for which the
   * session-filtered list is empty by construction.
   *
   * NOT A PLAIN FILTER, and that was the bug the chip version shipped with: a
   * non-null `pendingIds` is a photograph taken up to a poll interval before this
   * send existed, so the entry the admission has just created is legitimately
   * missing from it (`sched/waiting`).
   */
  const liveSeeds = useLiveSeeds(waitingSeeds, sched.pendingIds);

  /**
   * …AND A DELETION IS FORGOTTEN once a poll agrees the entry is gone.
   *
   * In an effect rather than a render for the liveness watch's reason: a render
   * that writes the memory it read answers differently depending on how many
   * times React ran it.
   */
  useEffect(() => {
    setDroppedEntries((cur) => pruneDropped(cur, sched.pendingIds));
  }, [sched.pendingIds]);

  /**
   * THE WAITING MESSAGES THIS CHAT DRAWS — ONE list, asked of the whole schedule.
   *
   * It used to be two, swapped on one fact: with a session, the session-filtered
   * pending list; without one, the leader's followers. Adoption flips exactly
   * that fact and both lists miss the followers in the instant it does — the
   * server fills a follower's session only when it CLAIMS it, and the leader id
   * is a client memory a reload drops — so the reader's queued messages vanished
   * from the transcript while sitting safely in the line (Bugbot PR #1124).
   * `sched.waitingHere` is now `waitingFor`: the group, by session AND by
   * `follow_of` in either direction, off every row the poll saw.
   *
   * IT IS THE SERVER'S ANSWER, which is the whole point: a reload re-reads the
   * same list and paints the same rows, where the chip this replaces was client
   * state and vanished.
   */
  const serverWaiting = sched.waitingHere;
  /** The rows themselves — server first, then the seeds no poll has listed yet,
   *  one row per entry id. */
  const waiting = useMemo(
    () => waitingRows(serverWaiting, liveSeeds, droppedEntries),
    [serverWaiting, liveSeeds, droppedEntries],
  );
  /**
   * WHAT IS IN FRONT, for every one of those rows and for the card above the box.
   *
   * ONE ANSWER, from this conversation's own `/api/tasks` row (`sched.rec`, which
   * the schedule hook already fetches) — the server's, so a reload says the same
   * thing. A chat with no session has one too: it is named after its leader
   * (`pending:<id>`), which is the key the row read falls back to. `admitAhead`
   * is the fallback for the paint before that row lands, and `claimedNext` the
   * one thing that outranks both — for one lap, after a Run next the server has
   * already accepted.
   */
  /**
   * FOLLOW-UPS THE LIVE RUN IS STILL HOLDING — the bubbles a reload used to lose.
   *
   * A line typed into a running chat is absorbed by the live host: it sits in the
   * CLI's own queue until the current turn ends. For that window the only copy on
   * screen was this page's optimistic bubble, which is client memory — so a
   * reload, or the standing watch's four-a-minute `refreshHistory`, replaced the
   * transcript with a JSONL that does not have the message either (nothing has
   * consumed it) and the reader's own words simply vanished (Akshil,
   * 2026-09-12). The RUN knows, and now says so (`PollResponse.inbox`).
   *
   * DEDUPED HERE rather than in the controller, because "does this still need a
   * bubble" is a question about what is on screen: the optimistic list and the
   * turns both answer it, and both move without the inbox moving. One entry
   * leaves for one of three reasons, all of them somebody else drawing the same
   * message — see `protocol/inbox`.
   */
  const inboxRows = useMemo(
    () =>
      inboxBubbles(
        state.inbox,
        state.queued,
        state.turns.filter((t) => t.role === "user").map((t) => t.text),
      ),
    [state.inbox, state.queued, state.turns],
  );
  const claimedNext = nextClaim !== null && nextClaim === sched.recGen;
  const waitFacts = useMemo(
    () => waitingFacts(sched.rec, admitAhead, claimedNext),
    [sched.rec, admitAhead, claimedNext],
  );
  /**
   * HOW MANY MESSAGES THE CARD SAYS ARE WAITING — the server's own count.
   *
   * NOT `waiting.length`, which is the number of ROWS this chat is drawing and a
   * different question: it includes a message scheduled for next Tuesday (drawn,
   * correctly, as `scheduled`) and a seed no poll has confirmed, so a chat with
   * one calendar entry a week out read "1 message waiting" for six days about
   * nothing anybody was waiting behind (Bugbot PR #1124). `queue_waiting` is
   * counted where every entry can be seen and means DUE AND HELD (design.md, UI).
   *
   * The fallback for a chat with no row yet counts only the rows that are
   * actually in the line — the same sentence, said with what is in hand.
   */
  const waitCount = useMemo(() => {
    const said = sched.rec?.queue_waiting;
    if (typeof said === "number") return said;
    return waiting.filter((r) => r.word === "queued").length;
  }, [sched.rec, waiting]);

  /**
   * RUN NEXT — this conversation's waiting work to the front of its folder's
   * line. The same endpoint the Tasks row and the Board's drag spend, so one verb
   * cannot mean two things in two places, and it interrupts NOTHING: the run
   * holding the folder keeps running and this goes when it ends.
   *
   * THE TASK WHEN THERE IS ONE, THE ENTRY OTHERWISE. `sched.rec.key` is read off
   * a listing the poll just took, so it cannot be the stale `pending:<leader>`
   * key the admission answered and the store has since rekeyed — and the task
   * form promotes every message this chat has waiting, which is what the card
   * over the composer is a summary of. With no row yet there is no key to trust,
   * and one entry id — minted once, never rekeyed — is the honest fallback.
   */
  const runNext = useCallback(async () => {
    const key = sched.rec?.key || "";
    const first = waiting[0]?.entryId || "";
    if (!key && !first) return;
    setRunningNext(true);
    try {
      await skipQueue(key ? { key } : { entry_id: first });
      // The claim is painted straight onto the card — on the FACTS rather than on
      // the fallback, because the facts prefer the server's row whenever there is
      // one and the row in hand was read before this press. It is held only until
      // a fresher row lands, which is what `schedRefresh` goes and asks for.
      setAdmitAhead((cur) => ({ ...(cur ?? {}), status: "queued", queue_priority: true }));
      setNextClaim(sched.recGen);
      schedRefresh();
    } catch (err) {
      // AND A REFUSAL IS SAID OUT LOUD. The press has a visible control behind
      // it, so silence is a button that did nothing — and the failure that
      // actually happened in review was a 404 on a stale key, invisible for
      // exactly as long as nobody was reading the network tab.
      const t = troubleFromError(err);
      controller.reportTrouble({ ...t, message: "Run next did not go through: " + t.message });
    } finally {
      setRunningNext(false);
    }
  }, [controller, schedRefresh, sched.rec, sched.recGen, waiting]);

  /**
   * DELETE, from a waiting row: the words are dropped and nothing runs.
   *
   * THE SAME ENDPOINT the Tasks page's cancel posts — `POST /api/schedule/cancel`
   * on the ENTRY id. One press and no arming: a repeat's stop spends every future
   * run and has to be confirmed; this drops one message, whose words are in the
   * bubble directly above the word the reader pressed.
   *
   * AND THE ROW GOES ON THE ANSWER, not on the next poll: a row that stayed up
   * for fifteen seconds after a successful delete reads as a control that did
   * nothing. It is remembered as dropped until a poll stops listing the entry,
   * because the poll's own list is up to a lap older than the press and would
   * otherwise put the row straight back (`sched/waiting` `pruneDropped`).
   */
  const deleteWaiting = useCallback(
    async (entryId: string, stopId: string = "") => {
      setDeleting((cur) => new Set(cur).add(entryId));
      try {
        // THE TEMPLATE WHEN THERE IS ONE (`schedStopTarget`, handed in by the
        // row): cancelling a repeat's OCCURRENCE only skips that run and the
        // template arms the next, so "stop repeating" has to reach the template
        // or it is the same button as "skip this run" wearing another word.
        await cancelScheduledMessage(stopId || entryId);
        setDroppedEntries((cur) => new Set(cur).add(entryId));
        setWaitingSeeds((cur) => cur.filter((q) => q.entryId !== entryId));
        schedRefresh();
      } catch (err) {
        const t = troubleFromError(err);
        controller.reportTrouble({
          ...t,
          message: stopId
            ? "This repeat was not stopped: " + t.message
            : "This message was not deleted: " + t.message,
        });
      } finally {
        setDeleting((cur) => {
          const next = new Set(cur);
          next.delete(entryId);
          return next;
        });
      }
    },
    [controller, schedRefresh],
  );


  /**
   * THE LEADER RAN, SO THIS CHAT HAS A SESSION NOW — adopt it.
   *
   * The gap this closes: a new chat whose first message was queued has no
   * session, so every later send joins that entry as a follower
   * (`sched/queue-leader`). The SCHEDULER runs the leader, not this page — and
   * none of the chat's roads to a session id begin anywhere but here. The
   * schedule watcher's attach is the nearest thing and it is not enough: it
   * needs a live `run_id` to probe, so a leader that ran and finished while this
   * tab was in the background leaves nothing to attach to, and a FOLLOWER's run
   * is written off outright (`scheduledRunIsOurs` adopts only entries naming no
   * session on a session-less screen). Without this the chat stays a chat with
   * no session for ever: new followers behind a leader that is long gone, and a
   * transcript showing none of what it said.
   *
   * THE ENTRY IS THE RECORD — `claude_session_id`, read off the poll this pane
   * already pays for (`sched.ranSessions`) — and the adoption is `openSession`,
   * which is what every other "open that conversation" gesture in this file
   * spends (`onOpenSession`, the sessions list, Peek). It brings the real
   * transcript with it, which is the point: the optimistic bubbles are replaced
   * by what the run actually said.
   *
   * THE ROWS STAY, AND THEY STAY BY THEMSELVES. They are the server's
   * (`sched/waiting.waitingFor`, through `sched.waitingHere`): those follower
   * entries are still waiting in this folder's line, and the group they belong to
   * is read off `follow_of` and the leader's own session — so the very poll that
   * hands this chat its session id goes on listing them. Nothing here has to
   * carry them across, and nothing here may drop them: the adoption replaces the
   * TRANSCRIPT, not the line.
   *
   * NO GUARD REF. `openSession` emits the id, `state.sessionId` stops being ""
   * and `leaderSession` answers "" from then on — the effect's own dependency is
   * what closes it. `setEntered` is for the landing case: this chat may never
   * have been anywhere else.
   */
  const adoptSession = leaderSession(sched.ranSessions, leader.peek(), state.sessionId ?? "");
  useEffect(() => {
    if (!adoptSession) return;
    resetCardPolicy(cardPolicy);
    setEntered(true);
    // THE ENTRY WAS THE NAME UNTIL NOW. `openSession` writes the real one, and
    // leaving `?queued=` beside it would re-remember a leader this chat has just
    // outgrown on any later render (see `queuedParam`).
    params.set({ [QUEUED_PARAM]: null }, { history: "replace" });
    void controller.openSession(adoptSession);
  }, [adoptSession, controller, cardPolicy, params]);

  /**
   * T:16776/18000 — the block and both attach sets belong to the conversation
   * that WAS on screen, so a REPLACED transcript takes them with it.
   *
   * KEYED ON THE REPLACEMENT, not on the session id. T calls
   * `scheduleResetForNewTranscript()` from `loadHistory`'s non-refresh branch
   * and from nowhere else, and `openSession` is this port's only such branch.
   * The id is a different fact: it also changes on MOUNT (a second
   * `/api/schedule` fetch racing the one `watcher.start()` already issues) and
   * when the first poll of a brand-new chat reports one, MID-RUN — where the
   * reset re-arms `baselined = false` and the next tick then silently baselines
   * away a scheduled run that fired in that window, which is the exact failure
   * T's "at LOAD, not one interval later" note exists to prevent.
   *
   * `transcriptGen` starts at 0, so the mount is skipped by construction.
   */
  schedReset.current = sched.reset;
  const transcriptGen = state.transcriptGen;
  useEffect(() => {
    if (!transcriptGen) return;
    schedReset.current();
  }, [transcriptGen]);

  /** `inChat` is the fifth argument because the strip's two lifecycle rules —
   *  emptied on BOTH crossings, read on the way into a chat (P4-01/P4-02) — are
   *  facts about the strip, and the hook is where they can be tested. */
  const art = useArtStrip(
    agentDir ?? null,
    file,
    state.sessionId ?? "",
    undefined,
    inChat,
  );
  artTick.current = art.poll;
  const [snapNonce, setSnapNonce] = useState(0);
  snapInvalidate.current = () => setSnapNonce((n) => n + 1);

  /**
   * THE STANDING LIVE WATCH (D415). Armed for the life of a chat that has a
   * session, disarmed on the way home — the landing page has no conversation to
   * adopt a turn into, and `live_run` with no session matches on the target
   * alone, which would drag another chat's run onto this screen.
   *
   * `ownRunEndedAt` is milliseconds in `ChatState` (the controller's own clock)
   * and EPOCH SECONDS in the follower's rule, because that is the unit
   * `os.stat` reports. Converted here, at the seam, rather than in either.
   */
  useEffect(() => {
    if (!inChat || !state.sessionId) return;
    const watch = createLiveWatch({
      sessionId: () => controller.getState().sessionId ?? "",
      busy: () => controller.isBusy(),
      // The NARROW one, for the external line's OFF edge only (T:17709).
      hasActiveRun: () => controller.hasActiveRun(),
      adopt: (id) => controller.adoptLiveRun(id, { laps: 1, quiet: true }),
      transcriptMark: () => controller.getState().transcript,
      ownRunEndedAt: () => controller.getState().ownRunEndedAt / 1000,
      liveness: (path) => getClaudeSessionLiveness(path),
      refreshHistory: (id) => controller.refreshHistory(id),
      setExternalWorking: (on) => controller.setExternalWorking(on),
      activityKey: CHAT_ACTIVITY_KEY,
    });
    return watch.start();
  }, [controller, inChat, state.sessionId]);

  const card = useMemo(
    () => ({
      file,
      sessionId: state.sessionId ?? "",
      controls,
      status: state.status,
      queued: state.queued,
      onSend,
      onFollowUp,
      onStop,
      // ONLY INSIDE A CHAT (FIX-9). T focuses the box from `enterChat()`
      // (T:13087-13094) — which runs on a send from the landing, on opening a
      // recent row, on "new chat", and on a BOOT that arrives carrying a
      // `session_id`/`run` (T:19267) — and from nowhere else. The landing page
      // never takes the keyboard: `focusBox` has exactly three callers and not
      // one of them is boot.
      //
      // Native focused on MOUNT, unconditionally, so the landing came up with
      // `:focus-within` already true and its composer painted
      // `--border-strong` where legacy paints `--border` — the whole of the
      // measured "composer border colour" delta — and opening a file with the
      // panel on moved the reader's keyboard into the chat.
      //
      // `&& inChat` gets both halves from one expression, because the flip to
      // true is a re-render and the composer's focus effect is keyed on this
      // prop: no focus on the landing, focus the moment a conversation is
      // entered, which is `enterChat` exactly.
      autoFocus: props.autoFocus && inChat,
      columnRef,
      back: currentUrl(),
      onNavigate,
      boxRef,
      // The nav lock reaches BOTH composers' Schedule seats (T:12075/12099
      // guard every `.schedbtn`), unlike the schedule block, which is
      // chat-only. `styles/ann.css` already dims them; this is the guard for
      // the hand — and the keyboard, which `pointer-events: none` never stopped.
      navLocked: ann.locked,
      navLockedReason: NAV_LOCKED_REASON,
      // Pictures — and NOTES — alone are sendable, with no words at all
      // (T:17903, and ✓ Done's whole gesture: a round of comments IS the
      // message).
      // ...and the ONE predicate the send path and ✓ Done read as well
      // (`ann/store.isSendableNow`): a chip with neither words nor a recording
      // stamp is not a sendable note, so Send must not light up for one — a
      // single click in Comment mode makes exactly that chip, and the three
      // answers used to disagree about it.
      //
      // ASKED AT THIS MOMENT, because a walkthrough's marks are stamped long
      // before their words land: while it records or settles a wordless mark is
      // not sendable, so Send does not light up for one and `beginSend` (which
      // filters by the same predicate) cannot fold it into a line the reader
      // typed meanwhile. Sending it there uploaded an empty note and the
      // transcript then wrote words onto it after it was stamped `sent`
      // (Bugbot, PR #1074).
      // The scheduler handoff's two directions (owner E2E R1, F4): what the
      // tray holds when Continue is pressed, and the paths that come back with
      // "Back to chat" (task-shots copies, registered as real paths — no
      // upload, `useAttachments.addPaths`).
      attachments: () => attach.items,
      onRestoreAttachments: (paths: string[]) => void attach.addPaths(paths),
      hasAttachments:
        attach.items.length > 0 ||
        ann.chips.some((c) => isSendableNow(c.note, walkthroughOwns(ann.mode))),
      // ... but not while one of them is still on its way: `take()` leaves a
      // `pending` chip in the tray, so a send fired now would go out WITHOUT
      // the files whose chips made it sendable (Bugbot, PR #1064).
      //
      // AND THE CAMERA COUNTS, even though it plants no chip. `capture()` puts
      // nothing in the tray until the bytes are in hand — the seat swap is the
      // whole of its commit — so its window (up to the native path's several
      // seconds on a large pane) was invisible to this gate: the flash had
      // already fired, so the picture LOOKED taken, an Enter in that window went
      // out without it, and it then landed in the tray for the NEXT message. T
      // holds the send for the in-flight shot for the same reason
      // (`shotBusy`/`shotAttachPane`); `capturing` is that flag.
      attachPending: attach.capturing || attach.items.some((a: Attachment) => a.pending),
      chips: (
        <AttachTray
          items={attach.items}
          paneNoun={pane.paneNoun}
          onOpen={setViewing}
          onRemove={attach.remove}
        >
          {/* The SAME pill a screenshot's chip is, in the same row: both are
              things this message is about to carry and both come off with the
              same ✕ (T:927). The tray renders them first. */}
          <AnnChips items={ann.chips} onEdit={ann.editNote} onRemove={ann.removeNote} />
        </AttachTray>
      ),
      // T:8505 — whichever composer is mounted hands its send in, for the
      // walkthrough's auto-submit and for ✓ Done.
      submitRef: submitBox,
      // THE SEND WINDOW'S LATCH, in both its forms: the ref is read in the tick
      // the composer calls `onSend`, the flag dims the button on the next paint
      // (`dispatchSend`).
      busyRef: sendBusy,
      sendBusy: sendLocked,
      onPaste,
      // The chip row is ABOVE the control row and changes the composer's height,
      // never the row's width — but T re-measures on exactly this kind of change
      // (its MutationObserver watches the rows' subtree), and the footnote's
      // two-line budget is measured in the same pass (T:12455-12474).
      fitRevision: attach.items.length + ann.chips.length,
      // The composer closes for as long as the schedule holds a pending message
      // for this session — box AND calendar, off the SAME answer (T:17217-17246).
      // The SEND button is deliberately left alone: while a run is live it is
      // the STOP button, and a chat that cannot stop its own running turn is a
      // worse state than the one this feature prevents.
      blocked: sched.blocked,
      blockedPlaceholder: sched.placeholder,
      blockedReason: sched.reason,
      // THE FLAG TAKES THE FOLLOW-UP FOOTNOTE AWAY (see `ComposerCardProps`):
      // under the queue this pane says "waiting" about messages that really are,
      // and a count of follow-ups the live host will drain in seconds is the one
      // waiting state with nothing to act on.
      queueOn,
      // ONLY INSIDE A CONVERSATION. `card` is spread into `Home`'s composer as
      // well as the chat's, and a hand-back is about the turn that was running
      // — the landing has none.
      ...(stranded && entered ? { restore: stranded } : {}),
    }),
    [
      file,
      state.sessionId,
      state.status,
      state.queued,
      controls,
      onSend,
      onFollowUp,
      onStop,
      props.autoFocus,
      // The enter transition IS the focus trigger (see `autoFocus` above).
      inChat,
      onNavigate,
      boxRef,
      stranded,
      entered,
      sendLocked,
      urlTick,
      attach.items,
      attach.remove,
      // THE CAMERA'S OWN WINDOW (D7). `attachPending` reads `attach.capturing`,
      // which moves without the tray moving (a capture puts nothing in `items`
      // until the bytes land), so without it here the send gate went stale for
      // the whole of the in-flight shot. It was masked only by `onPaste`
      // depending on the whole `attach` object — a coincidence, not a dep.
      attach.capturing,
      onPaste,
      pane.paneNoun,
      ann.chips,
      // THE MOMENT `hasAttachments` IS ASKED AT: while a walkthrough records or
      // settles its wordless marks are not sendable, so the Send affordance has
      // to be recomputed when the mode moves and not only when the chips do.
      ann.mode,
      // The Schedule seat's guard reads it, so the card has to be rebuilt when
      // the lock moves — otherwise the seat stays live through the whole of an
      // armed round.
      ann.locked,
      ann.editNote,
      ann.removeNote,
      sched.blocked,
      sched.placeholder,
      sched.reason,
      queueOn,
    ],
  );
  /**
   * THE SPENT ANCHOR IS REMOVED WITH `replace` (T:12978-12984, P4-20).
   *
   * T argues it: "arriving on the message is not a place anyone navigated to
   * twice, and a Back that re-fired the flare would be a history entry nobody
   * made. Left behind it would also re-scroll a reload the reader has since
   * scrolled away from."
   *
   * A bare `set` reaches the replace path only while no gesture has happened on
   * the document (`params/store.ts:292`), which is not guaranteed here at all:
   * the anchor is spent when the turn it names is on screen, which is usually
   * after the reader has clicked something.
   */
  const onAnchorSpent = useCallback(
    () => params.set({ msg: null }, { history: "replace" }),
    [params],
  );

  /**
   * T:17198-17213 — a bottom-pinned transcript is put back at the bottom when
   * the banner appears, because the banner SHRINKS the scrollport.
   *
   * ONLY A PINNED ONE. This used to write `scrollTop = scrollHeight` off a raw
   * `.chat-logwrap` lookup, which jumped a reader who had scrolled up to the
   * latest turn the moment a pending message landed (Bugbot, PR #1075). T
   * calls `followBottom()` here, not `scrollBottom()`, and that function is
   * the follow FLAG's — so the port asks the flag too, through the handle the
   * scrollport lends out (`Transcript`'s `followRef`).
   *
   * The flag, not a geometry read taken here: this banner shrinks the
   * scrollport as it appears, so by the time an effect could measure, the gap
   * to the tail has crossed any threshold because the VIEWPORT moved and not
   * because the reader did (T:17211-17213). A flag survives a resize.
   */
  const transcriptFollow = useRef<(() => void) | null>(null);
  /**
   * "WHILE YOU WERE AWAY" (.claude-design/session-recap.md).
   *
   * The three facts the hook cannot read for itself:
   *
   *   * `recapAnchor` — WHERE the transcript stands. The last user turn's uuid,
   *     because assistant turns carry none (protocol/recap.ts);
   *   * `hasDraft` — read off the TEXTAREA, not off state. The composer's text
   *     is its own `useState` and only that component can see it (Composer's
   *     `submitRef` note says so in as many words), and `boxRef` is the seat
   *     this file already holds for exactly that reason. A function, sampled at
   *     the moment of the check, so nothing here re-renders per keystroke;
   *   * `enabled` — `prefs.chat.recap`, off the one prefs read every chat embed
   *     already makes (`feature-flag.ts`).
   */
  const recapEnabled = useChatRecapEnabled();
  const recapFor = useMemo(() => recapAnchor(state.turns), [state.turns]);
  const hasDraft = useCallback(
    () => !!boxRef.current && boxRef.current.value.trim().length > 0,
    [boxRef],
  );
  /** The mount's own box, for the hook's on-screen check — a getter because
   *  the ref is null on the render that binds the listeners and only the check
   *  runs late enough to have it. */
  const recapRoot = useCallback(() => rootRef.current, [rootRef]);
  const away = useAwayRecap({
    file,
    sessionId: state.sessionId,
    forUuid: recapFor,
    running,
    hasDraft,
    // THREE facts, and the host's is the one that is new: the pref, this mount
    // being a conversation rather than the landing, and the host having said
    // this is the chat the reader opened (`recap`, above).
    enabled: recapEnabled && inChat && !!props.recap,
    root: recapRoot,
  });
  // The task number this session is (`#session`, T:12696 showSession). Read here
  // rather than inside the topbar so the landing's kebab and the erase dialog
  // see the same one answer.
  //
  // A CHAT THAT HAS NEVER RUN IS ASKED ABOUT BY ITS LEADER. Its `/api/tasks` row
  // is keyed `pending:<entry id>` — there is no session to key on — and that key
  // is all `useTaskId` ever compares, so the same read answers for both kinds of
  // conversation.
  const taskKey = state.sessionId || (leaderId ? PENDING_KEY_PREFIX + leaderId : "");
  const listedTaskId = useTaskId(taskKey);
  // …and the three sources in freshness order (`sched/waiting.headerTaskId`), so
  // a queued chat wears its number from the admission rather than waiting a poll
  // interval for a listing to repeat it.
  const taskId = headerTaskId(listedTaskId, sched.rec?.task_id, admitTaskId);
  /** Is this conversation's work still IN ITS FOLDER'S LINE? The row's own word
   *  when there is a row, and "this chat has a leader entry and no session yet"
   *  before there is one — the two ways a chat can be waiting. What the kebab
   *  drops its terminal and archive items on. `inChat` gates it because the
   *  LANDING's one item is "New session in terminal", which is about no
   *  conversation at all and must never be taken away. */
  const queuedChat =
    inChat && (sched.rec?.status === "queued" || (!state.sessionId && !!leaderId));
  /** …and the other thing this chat's row can say: the plan's usage limit
   *  stopped this session and it starts again at a known time — "paused ·
   *  resumes 4:00 AM" at the top of the pane. "" on every ordinary chat. */
  const limitWord = usageLimitStatusWord(sched.rec);

  return (
   <CardPolicyProvider value={cardPolicy}>
    <div
      ref={rootRef}
      className={rootClass(
        props,
        [
          narrowView.classNames,
          split.dragging ? "dragging" : "",
          pane.noPane ? "nopane" : "",
          // T:1466 — A MODE LOCKS THE CHAT: while Comment or Annotate is on, or
          // a recording is still settling, the ways OUT are drawn inert. The
          // notes belong to this chat and this app; leaving would strand them.
          ann.locked ? "annlock" : "",
        ]
          .filter(Boolean)
          .join(" "),
      )}
      data-variant={variantOf(props)}
      {...(file ? { "data-file": file } : {})}
    >
      {paneShown ? (
        <>
          <AppPane
            pane={pane}
            params={params}
            file={file}
            narrowView={narrowView}
            {...(split.width ? { width: split.width } : {})}
            flags={flags}
            watcher={watcher}
            frameRef={setPaneFrame}
            // The frame's own `load` re-wires the six listeners inside the new
            // document and repaints every pin against it (T:8544).
            onFrameLoad={ann.onFrameLoad}
            stageRef={setStage}
            onRemeasure={ann.remeasure}
            annBar={
              <AnnBar
                handlers={annBarHandlers}
                paint={annBarPaint}
                barRef={ann.bindBar}
              />
            }
          />
          <SplitDivider split={split} />
          {/* The ring and the pin host, created into `.c-leftview` — ordinary
              nodes of THIS document, because in the split layout the pane is
              ours (the other two layouts build the same two boxes inside a
              shadow root over there). */}
          <AnnPins stage={stage} onBind={ann.bindPins} />
        </>
      ) : null}
      <div
        className={"c-chat" + (dropping ? " dropping" : "")}
        ref={columnRef}
        // DRAG AND DROP ON THE WHOLE COLUMN rather than on the textarea: a
        // target the size of a one-line input is a target people miss, and
        // everything in this column is part of the same message (T:11745-11751).
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        {!compact && !peek ? (
          // ONE HEADER ROW, WHICH IS WHAT T HAS (T:3934-4010, P2-1). `#anntools`
          // is a real layout row above both views and it carries FIVE things:
          // `← Chats` at its left end, then the picker, the three preview seats,
          // the view toggle and `#kebab` riding the right-hand end — on the
          // landing and in the transcript alike (`#chat.home #topbar` hides only
          // the IDENTITY row below, T:1277). Native had split those across three
          // rows — this strip, `.c-hdr-tools` in the top bar and
          // `.c-home-tools` on the landing — so the seats sat on a left-aligned
          // row of their own ABOVE the row that held the ⋮ (Akshil, 2026-09-09:
          // "just follow the UI we had previously").
          //
          // THE ROW IS ALWAYS THERE, and only its CONTENTS answer to the target:
          // T's markup is static and `annPollTarget` hides the three buttons, so
          // a folder listing keeps the row with the way out and the menu in it
          // (T:526 `body.nopane #kebab { margin-left: auto }` is that exact
          // state). `AnnStrip` returns null for itself when there is nothing to
          // photograph.
          <div className="c-anntools" ref={stripRef}>
            {/* The way back, at the strip's left end (T:3941). Absent on the
                landing: there is no chat to leave. */}
            {inChat ? (
              <button
                type="button"
                className="c-back"
                // AND WHY, while it is locked (T:6896 writes exactly this
                // sentence onto `#back.title` and clears it on unlock). It goes
                // into the accessible NAME as well as the `title`, because
                // `disabled` takes the button out of tab order: a hover-only
                // answer is no answer for a control the keyboard cannot land
                // on, and a dead way-out that will not say why is the one
                // refusal face worth spelling twice.
                aria-label={
                  ann.locked ? "Back to chats — " + NAV_LOCKED_REASON : "Back to chats"
                }
                title={ann.locked ? NAV_LOCKED_REASON : undefined}
                // PR3: locked while a comment round or a walkthrough owns the
                // page (`useAnnotations().locked`) — leaving mid-round would
                // orphan the notes. Main moved Back from the top bar into this
                // strip (P2-1), so the lock moved with it.
                disabled={ann.locked}
                aria-disabled={ann.locked ? "true" : "false"}
                onClick={onBack}
              >
                ← Chats
              </button>
            ) : null}
            {/* NO SPACER ELEMENT HERE. The slack is `#anncta`'s own
                `margin-left: auto` (T:255-262, chat.css), which is not the same
                thing: a spacer is a flex ITEM, so it kept its 12px gap even
                after collapsing to zero width — and on the landing the seats
                already fill the content box, so the whole group sat +10.34px
                right of legacy's and `⋮` overflowed its 16px padding down to
                5.66px (visual pass 2, FIX-6B). An auto margin contributes 0
                when there is no slack. `.c-hdr-slack` itself stays for the
                composer row that still uses it. */}
            <AnnStrip
              paneNoun={pane.paneNoun}
              // The row itself follows the ANNOTATE TARGET — ours or the
              // host's — because all three seats act on it
              // (`enterNoPane`'s step 6 note).
              shown={annTarget}
              // NO CAMERA GATE — T RENDERS THIS SEAT IN THE NARROW CHAT VIEW,
              // and the owner's rule is identical UX (visual pass 2, FIX-18).
              // T:3823's `body.view-chat .viewshot { display: none }` has
              // specificity 0,2,1 and LOSES to `#anncta button`, so it never
              // fires: measured live at a 736px pane, `#viewshot` computes
              // `display: flex` and 106px. T's own comment above that rule
              // argues the seat should go ("a view that shows no preview offers
              // no features OF the preview") — but the argument is not what
              // legacy draws, and the pane is parked rather than unmounted here
              // (`pane.css`: `visibility: hidden` keeps a real viewport), so
              // the photograph it takes is a real one. `AnnStrip` keeps
              // `cameraShown` for the hosts that do have nothing to shoot.
              // THE COMMENT SEAT, THOUGH, DOES GO in that view (T:3822
              // `body.view-chat #annbtn { display: none }` — specificity 0,2,1
              // against `#annbtn`'s own 1,0,0, so unlike its neighbour above
              // this one WINS and legacy hides it too): the pane the
              // clicks would land on is parked off screen, so there is nothing
              // to arm against — and arming anyway put the framed document's
              // capture-phase click swallower live over an invisible pane.
              // `useNarrowView`'s `onArriveChat` disarms on ARRIVAL only, and
              // nothing stopped a fresh arm afterwards. Same expression as the
              // camera's, deliberately: it is the same fact about the same
              // pane, and a prop rather than a CSS rule so `useFitStrip` keeps
              // reading a stable node set.
              commentShown={!(paneShown && narrowView.narrow && narrowView.view === "chat")}
              capable={ann.capable}
              mode={ann.mode}
              // `annOn` beside the mode, because the seat follows the READER's
              // mode and the mode value follows the RECORDER's phase — and they
              // part company for the width of an Esc'd transcription.
              armed={ann.armed}
              capturing={attach.capturing}
              // ALL THREE SEATS END IN THE COMPOSER, and a pending scheduled
              // message has it shut (P4R1-2): a picture lands as a chip above
              // the box, a comment round and a walkthrough send their notes
              // through it. The banner right below says why, so the seats carry
              // no second wording of it.
              blocked={sched.blocked}
              onScreenshot={() => void attach.capture()}
              onComment={ann.onCommentSeat}
              recSeat={
                <RecControls
                  rec={recSnap}
                  shown={micShown}
                  commentArmed={ann.mode === "comment"}
                  blocked={sched.blocked}
                  // NO SECOND TRASH IN THE STRIP. Discard moved off the strip
                  // and onto the bar over the app on 2026-09-06 (T:6240-6248,
                  // inventory 02 §I), so the strip carries only Screenshot ·
                  // Comment · Annotate in every state. `RecControls` still
                  // knows how to draw its own trash — a host that has no bar to
                  // put one on can ask for it — but this mount has
                  // `ann/AnnBar`'s, and two identical destructive controls on
                  // screen at once is the decision undone.
                  discardable={false}
                  onBegin={() => void recorder.begin()}
                  onEnd={() => void recorder.end()}
                  onDiscard={() => void recorder.discard()}
                />
              }
            />
            {host === "anntools" ? (
              <LeftModePicker
                modes={pane.decision?.leftModes ?? []}
                params={params}
                leftMode={leftMode}
              />
            ) : null}
            {narrowView.narrow ? <ViewToggle narrowView={narrowView} /> : null}
            {/* The menu rides the same seat in BOTH views (T's `#kebab` is on
                the one strip they share), so it never appears out of nowhere on
                entering a chat. On the landing it is the one item that can mean
                anything without a session (T:13415). */}
            <Kebab
              agentDir={agentDir}
              file={file}
              sessionId={inChat ? (state.sessionId ?? "") : ""}
              btnRef={kebabBtn}
              running={running}
              landing={!inChat}
              onErased={onErased}
              // PR3: Archive and Delete both carry the reader off this chat,
              // and a comment round or a walkthrough is about the app beside it
              // — the same nav lock that greys ← Chats and every recent row
              // (`annNavLocked`, T:6888/18181/18779).
              locked={ann.locked}
              lockedReason={NAV_LOCKED_REASON}
              // WAITING WORK OFFERS NEITHER A TERMINAL NOR A FILE (Akshil,
              // 2026-09-12). The row says `queued`, or this conversation is a
              // message that has never run — either way there is no session to
              // resume and nothing finished to put away. Delete stays: calling
              // the message off is exactly what a reader wants here.
              queued={queuedChat}
            />
          </div>
        ) : null}
        {inChat ? (
          <>
            {/* Taken away by the compact and peek cuts: the card's own head and
                the popup's own head already say which task this is
                (T:1412-1438). */}
            {!compact && !peek ? (
              <Topbar
                sessionId={state.sessionId ?? ""}
                subtitle={name}
                {...(taskId ? { taskId } : {})}
                running={running}
                // THE PLAN'S PAUSE, in the seat "running" rides: a session the
                // usage limit stopped says `paused · resumes 4:00 AM` instead
                // (platform/lib/usage-limit). Read off this chat's own row, which
                // is the same field the Tasks page draws the red ring from.
                status={limitWord}
              />
            ) : null}
            <Transcript
              followRef={transcriptFollow}
              state={state}
              comebackPending={sched.blocked}
              actions={actions}
              liveMode={state.permissionMode}
              tail={tail}
              pickerMode={defaults.permission}
              msgAnchor={msgAnchor}
              onAnchorSpent={onAnchorSpent}
              onShowSent={setSent}
              onOpenShot={setViewing}
              paneNoun={pane.paneNoun}
              what={file ? "using the chat on " + file : "using the chat"}
              recap={
                away.recap ? (
                  <RecapFold text={away.recap.text} />
                ) : null
              }
            />
            {/* FOLLOW-UPS THE LIVE RUN IS HOLDING, in the transcript's own user
                bubble and in its own column. Ordinary bubbles, with no line
                under them and no chrome of any kind: the message is not queued,
                it is not behind anything, and the host will drain it in seconds
                — a caption saying so would be this feature narrating the app's
                normal behaviour back at the reader (design.md, UI).

                THEY ARE DRAWN FROM THE RUN (`state.inbox`), which is what makes
                a reload paint the same picture: the optimistic bubble above them
                is this document's memory and does not survive one, and the
                transcript cannot help because nothing has consumed the message
                yet. Deduped against both (`inboxRows`), so no message is ever
                two bubbles. */}
            {inboxRows.map((row) => (
              <div className="c-inbox" key={row.id}>
                <div className="turn user c-inbox-turn">
                  <div className="bubble">{row.text}</div>
                </div>
              </div>
            ))}
            {/* THE MESSAGES THIS CHAT HAS NOT SENT YET, at their place in the
                conversation. They are the LAST rows of the transcript by
                construction — the scheduler sends in `due` order, which for a
                chat's own sends is the order they were typed, and nothing of
                this conversation's can be after them — so drawing them
                immediately under the log IS drawing them in the transcript,
                without threading a per-turn slot through it for rows that belong
                to the schedule rather than to the run.

                DRAWN FROM THE SERVER (`waiting`), which is what makes a reload
                paint the identical picture. The row the admission puts up a
                second after Enter is the same row, minted early from the
                answer and replaced by the server's own on the next poll.

                NOTHING HERE FOR A FOLLOW-UP INTO THIS CHAT'S OWN RUNNING TURN.
                Those bubbles are ordinary bubbles the controller posted, they
                are held by the live host for a matter of seconds, and there is
                no entry to be behind, to run next, or to delete — so they get no
                second row, no card, and (under the flag) not even the composer's
                old footnote. A count of something nobody can act on was three
                pieces of chrome for a state that resolves itself. */}
            {queueOn &&
              waiting.map((row) => (
                <WaitingRow
                  key={row.entryId}
                  row={row}
                  facts={waitFacts}
                  deleting={deleting.has(row.entryId)}
                  onDelete={() => void deleteWaiting(row.entryId)}
                  /* A REPEAT'S SECOND VERB. `delete` on an occurrence skips one
                     run and the template arms the next, so the row offers the
                     thing the reader actually meant — and it posts the TEMPLATE
                     id the row carries, which is the only id that stops it. */
                  onStopRepeat={() => void deleteWaiting(row.entryId, row.stopId)}
                />
              ))}
            {/* …AND ONE SUMMARY OVER THE BOX. The rows are in a transcript that
                scrolls; this is pinned where the composer is, so a reader twenty
                turns down still knows something of theirs is held, what by, and
                the one press that changes it. */}
            {queueOn && waitCount > 0 ? (
              <WaitingCard
                count={waitCount}
                facts={waitFacts}
                busy={runningNext}
                onRunNext={() => void runNext()}
              />
            ) : null}
            {/* DIRECTLY ABOVE THE COMPOSER and kept by BOTH host cuts, which is
                T's own arrangement: `body.chat-compact` and `body.chat-peek`
                take the topbar, the strip, the box and the footnote and leave
                this (T:1412-1438). A compact tile whose chat is shut has to be
                able to say so — it is the only thing in that tile that explains
                why the wall's own composer refuses. */}
            <SchedBlock
              blockers={sched.blockers}
              rec={sched.rec}
              armed={sched.armed}
              refused={sched.refused}
              stopping={sched.stopping}
              tick={sched.tick}
              onStop={sched.onStop}
              onRow={sched.onRow}
              cardRef={sched.cardRef}
            />
            {/* A card is READ, not typed into: compact is the one cut that takes
                the composer away, which is the whole difference from peek
                (T:1412-1422). The strip rides INSIDE the composer's own block
                (between the box and the footnote, T:4203) when there is one,
                and stands alone in the compact tile that has none. */}
            {!compact ? (
              <Composer
                {...card}
                footnote={footnoteFor(pane.noun)}
                artStrip={<ArtStrip items={art.items} />}
              />
            ) : (
              <ArtStrip items={art.items} />
            )}
          </>
        ) : (
          <Home
            agentDir={agentDir}
            snapInvalidation={snapNonce}
            {...card}
            name={name}
            {...(file ? { path: file } : {})}
            placeholder={homePlaceholderFor(pane.noun)}
            recent={recent}
            onOpenSession={onOpenSession}
            listsDisabled={ann.locked}
          />
        )}
        {/* THE NOTE COMPOSER'S IDLE HOME (T:7291): ONE node, parked in the chat
            column while it is not lent to the target's document — and parked
            OUTSIDE the home/chat branch, because the round of notes survives the
            first send that leaves the landing. */}
        <AnnPopover handlers={ann.popHandlers} popRef={ann.bindPop} />
      </div>
      <SentPop
        open={!!sent}
        onClose={() => setSent(null)}
        outgoing={sent?.raw ?? ""}
        paneNoun={pane.paneNoun}
        onOpenShot={setViewing}
      />
      {/* ABOVE the popup (z 90 against 80): a picture opened from inside it must
          land ON TOP or the click looks dead (T:1147-1148). */}
      <ShotViewer
        shot={liveViewing}
        paneNoun={pane.paneNoun}
        onClose={() => setViewing(null)}
        onDiscard={onDiscardShot}
      />
    </div>
   </CardPolicyProvider>
  );
}

export default ClaudeChat;
