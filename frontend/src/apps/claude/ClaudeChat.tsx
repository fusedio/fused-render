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
import type { SendOptions, UserTurn } from "./protocol/controller-api";
import { watchStreamTeardown, watchTopOrigin } from "./shots";
import type { Attachment, Receipt } from "./shots/types";
import { enhanceCodeBlocks } from "./protocol/markdown";
import { createChatController } from "./protocol/run-controller";
import { finishedTailText, parseTailKey, streamingTailOf } from "./protocol/segments";
import { createTyper, type Typer } from "./protocol/typer";
import { createFrameClock } from "./ui/frameClock";
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
  type PaneSrcFlags,
} from "./pane";
import {
  AnnStrip,
  AttachTray,
  Kebab,
  CardPolicyProvider,
  Composer,
  createCardPolicy,
  Home,
  openCardIds,
  resetCardPolicy,
  SentPop,
  settleReceipts,
  ShotViewer,
  Topbar,
  Transcript,
  TroubleView,
  ATTACH_API,
  mergeSendOptions,
  useAttachments,
  useFitStrip,
  useComposerDefaults,
  useRecentSessions,
  useTaskId,
  type TranscriptTail,
  type Viewable,
} from "./ui";
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
    void resolveAgentDir(file).then((dir) => {
      if (live) setAgentDir(dir);
    });
    return () => {
      live = false;
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
    // The template lookup is in flight. The host is still holding its own cover
    // over this box (ChatFrame's skeleton), so a second skeleton on a second
    // clock is exactly what 00 §1e's "one wait, one look" forbids.
    return <div className={rootClass(props)} data-variant={variantOf(props)} />;
  }
  if (agentDir === null) {
    return (
      <div className={rootClass(props)} data-variant={variantOf(props)}>
        <div className="chat-logwrap">
          <div className="chat-log">
            <TroubleView
              trouble={{
                kind: "generic",
                message: file
                  ? "There is no claude template for this folder."
                  : "No target was given to open a chat on.",
              }}
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
  const [watcher] = useState(() => createAppStateWatcher(appFrame));
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
  // `enterNoPane`'s steps 1, 2 and 5 are the ANNOTATION half of the teardown and
  // land in PR3 (AppPane's header spells the order out and why it is an order).
  // Steps 3 and 4 fall out of `noPane` here — `useNarrowView` answers `""` for a
  // no-pane target and the root's own `nopane` class carries the rest — and step
  // 6 is `AppPane`'s early return.
  const noPaneSteps = useMemo(() => ({}), []);
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
  const narrowView = useNarrowView({ params, noPane: pane.noPane });
  const split = useSplit({ params, narrow: narrowView.narrow, noPane: pane.noPane });
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
   * So the spent handles go in STATE and are released from an effect: a passive
   * effect runs after React has mutated the tree, so by the time it fires every
   * receipt is drawn with `rawUrl(view)` and nothing on screen is holding a
   * `blob:` handle any more.
   */
  const [spentBlobs, setSpentBlobs] = useState<readonly Attachment[]>([]);
  /** The same queue, where an UNMOUNT can still reach it. A chat closed between
   *  the store write and its commit would otherwise leave handles nothing has a
   *  reference to any more — the tray gave them up and `inFlight` has already
   *  deleted the send — pinned for the life of the document. */
  const spentAlive = useRef<Attachment[]>([]);
  useEffect(() => {
    if (!spentBlobs.length) return;
    for (const att of spentBlobs) ATTACH_API.revoke(att);
    spentAlive.current = [];
    // Emptied, so a later swap's queue is its own; `revoke` is idempotent, so a
    // re-run before that lands (StrictMode) costs nothing.
    setSpentBlobs((q) => (q === spentBlobs ? [] : q));
  }, [spentBlobs]);
  useEffect(
    () => () => {
      for (const att of spentAlive.current) ATTACH_API.revoke(att);
      spentAlive.current = [];
    },
    [],
  );
  const attachBack = useRef<((items: readonly Attachment[]) => void) | null>(null);
  const [stranded, setStranded] = useState<{ text: string; seq: number } | null>(null);
  const strandSeq = useRef(0);

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
        model: () => liveModel.current,
        effort: () => liveEffort.current,
        hasPane: () => paneAnswer.current(),
        // The controller already announces on THIS document
        // (`announceTasksChanged`); the stamp is for every OTHER one (T:16435).
        onActivity: stampChatActivity,
        // Follow-ups the CLI never delivered come BACK to the box they were
        // typed in rather than being dropped (`still_queued`, T:15911).
        onStranded: (texts) => {
          const text = texts.filter(Boolean).join("\n");
          if (!text) return;
          strandSeq.current += 1;
          setStranded({ text, seq: strandSeq.current });
        },
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
        // PR4 hangs the artifacts read and the snapshot invalidation here
        // (T:16229, 16321-16330); the ticks already run on T's clock.
        onArtifactsTick: () => {},
        onRunEnded: () => {},
        // The agent saw none of it, so the pictures come back to the tray —
        // never revoked on this road, because those very thumbnails are what the
        // returned chips show (T:16693-16720).
        onSendReturned: ({ attachments }) => {
          if (!attachments) return;
          const back = inFlight.current.get(attachments);
          if (!back) return;
          inFlight.current.delete(attachments);
          attachBack.current?.(back);
        },
      }),
    [agentDir, file, params],
  );
  useEffect(() => () => controller.dispose(), [controller]);

  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getState,
    controller.getState,
  );

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
  if (props.initialAsk) askRef.current = props.initialAsk;

  // ── home vs chat (T:1277-1282 `#chat.home`) ────────────────────────────────
  // The host's ids count here as well as the store's: they are seeded into the
  // store by the boot effect below (a COMMITTED effect — a `params.set` in a
  // render body is a history write from a render React may discard), and this
  // initializer runs before it.
  const [entered, setEntered] = useState(
    () =>
      !!(
        params.get("session_id") ||
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
        // Nothing to restore. The landing paints its card and Recent's skeleton
        // on this very render, so it is ready now (T:19291-19296).
        markReady();
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
    if (!onEscape || typeof document === "undefined") return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape" || ev.defaultPrevented) return;
      // Only this chat's keystrokes: `document` is shared with the shell.
      const root = rootRef.current;
      const target = ev.target as Node | null;
      if (!root || !target || !root.contains(target)) return;
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
  const recent = useRecentSessions(inChat ? null : agentDir, file);

  /**
   * WHAT THE TRAY PUTS ON THE WIRE, on both send roads: the `<pane-shot>` block,
   * the Read rules for the directories real-path attachments live in — granted
   * for the SESSION, not the turn (T:16657-16668) — and the receipts the turn
   * will wear. The tray is emptied by the same call that reads it, so a second
   * Enter cannot send the same pictures twice (T:16532).
   */
  const takeAttachments = useCallback((): { opts: SendOptions; items: Attachment[] } => {
    const out = attach.take();
    if (!out.items.length) return { opts: {}, items: [] };
    return {
      opts: { blocks: out.blocks, readDirs: out.readDirs, attachments: out.receipts },
      items: out.items,
    };
  }, [attach]);

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
    (opts: SendOptions): { merged: SendOptions; done: () => void } => {
      const mine = takeAttachments();
      const merged = mergeSendOptions(opts, mine.opts);
      const key = mine.items.length ? merged.attachments : undefined;
      if (key) inFlight.current.set(key, mine.items);
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
        done: () => {
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
          setSpentBlobs((q) => (q.length ? q.concat(settled.spent) : settled.spent));
        },
      };
    },
    [controller, takeAttachments],
  );

  const onSend = useCallback(
    (text: string, opts: SendOptions) => {
      // OPTIMISTIC, and synchronously BEFORE the dispatch — exactly where T puts
      // it (`enterChat()` in the landing form's own `onsubmit`, T:17935, one line
      // ahead of its `sendMessage(message)`). `inChat` is `entered ||
      // !!state.sessionId` and nothing on this path set `entered`, so the
      // landing stayed up until a POLL reported a session id: one `start`
      // round-trip plus a 400 ms lap, which is the 1-2 s stall the QA measured
      // against :1777. The controller puts the user bubble up before anything
      // slow on its own path (`addUser`), so the view this switches to already
      // has the message and the working line in it.
      //
      // A REFUSED START DOES NOT COME BACK HERE, and T's `enterChat()` is
      // equally one-way: its rollback (T:16693-16720) drops the bubble and posts
      // the failure INTO the chat, where the reader stays to read it. Back is
      // the way out, the same as for a run that started and then failed.
      setEntered(true);
      const { merged, done } = beginSend(opts);
      // `then(done, done)` and not `finally`: the promise is deliberately
      // discarded, and a `finally` chain would turn a rejected send into an
      // unhandled rejection in the console.
      void controller.sendMessage(text, merged).then(done, done);
    },
    [controller, beginSend],
  );
  const onFollowUp = useCallback(
    (text: string) => {
      const { merged, done } = beginSend({
        model: defaults.model,
        effort: defaults.effort,
        permission: defaults.permission,
      });
      void controller.sendFollowUp(text, merged).then(done, done);
    },
    [controller, defaults.model, defaults.effort, defaults.permission, beginSend],
  );
  const onStop = useCallback(() => void controller.stopRun(), [controller]);
  const onBack = useCallback(() => {
    // A fresh transcript is a fresh card policy: an override from the
    // conversation that WAS on screen must not leak a card open in one the user
    // has never touched (ui/cardPolicy.ts).
    resetCardPolicy(cardPolicy);
    controller.newChat();
    setEntered(false);
    // AND THE STRANDED TEXT GOES WITH THE CONVERSATION IT WAS TYPED IN. It was
    // never cleared, and `card.restore` reaches the LANDING's composer as well
    // as the chat's — so words typed for session A sat one Enter away from
    // opening a brand-new conversation, and the per-instance delivery ledger
    // (`Composer`'s `delivered`) restarts on the Home/chat remount, so every
    // Back appended them again.
    setStranded(null);
  }, [controller, cardPolicy]);
  const onOpenSession = useCallback(
    (sessionId: string) => {
      resetCardPolicy(cardPolicy);
      setEntered(true);
      // Same rule as Back: a hand-back belongs to the conversation it was typed
      // in, and this is a different one.
      setStranded(null);
      void controller.openSession(sessionId);
    },
    [controller, cardPolicy],
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
  const onErased = useCallback(() => onBack(), [onBack]);

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
    [attach],
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
  // NO `dragHasAttachment` GUARD HERE, unlike its three neighbours (T:11770
  // guards nothing either): several engines expose no `types` at all on
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
      autoFocus: props.autoFocus,
      columnRef,
      back: currentUrl(),
      onNavigate,
      boxRef,
      // Pictures alone are sendable, with no words at all (T:17903).
      hasAttachments: attach.items.length > 0,
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
        />
      ),
      onPaste,
      // The chip row is ABOVE the control row and changes the composer's height,
      // never the row's width — but T re-measures on exactly this kind of change
      // (its MutationObserver watches the rows' subtree), and the footnote's
      // two-line budget is measured in the same pass (T:12455-12474).
      fitRevision: attach.items.length,
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
      onNavigate,
      boxRef,
      stranded,
      entered,
      urlTick,
      attach.items,
      attach.remove,
      onPaste,
      pane.paneNoun,
    ],
  );
  const onAnchorSpent = useCallback(() => params.set({ msg: null }), [params]);

  // The task number this session is (`#session`, T:12696 showSession). Read here
  // rather than inside the topbar so the landing's kebab and the erase dialog
  // see the same one answer.
  const taskId = useTaskId(state.sessionId ?? "");

  return (
   <CardPolicyProvider value={cardPolicy}>
    <div
      ref={rootRef}
      className={rootClass(
        props,
        [narrowView.classNames, split.dragging ? "dragging" : "", pane.noPane ? "nopane" : ""]
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
          />
          <SplitDivider split={split} />
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
                aria-label="Back to chats"
                onClick={onBack}
              >
                ← Chats
              </button>
            ) : null}
            {/* ONE auto margin in the row: everything before it sits left, the
                seats and the ⋮ ride the right-hand end together (T:255-262). */}
            <span className="c-hdr-slack" />
            <AnnStrip
              paneNoun={pane.paneNoun}
              // The camera photographs whatever the annotate target is, so the
              // seats go only when there is nothing to photograph. The narrow
              // CHAT view parks OUR preview off screen (T:3823
              // `body.view-chat .viewshot`) and is the one layout answer left in
              // here; a hosted mount's target is the host's own column, which
              // that view does not move.
              shown={annTarget && !(paneShown && narrowView.narrow && narrowView.view === "chat")}
              capturing={attach.capturing}
              onScreenshot={() => void attach.capture()}
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
              />
            ) : null}
            <Transcript
              state={state}
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
            />
            {/* A card is READ, not typed into: compact is the one cut that takes
                the composer away, which is the whole difference from peek
                (T:1412-1422). */}
            {!compact ? <Composer {...card} footnote={footnoteFor(pane.noun)} /> : null}
          </>
        ) : (
          <Home
            agentDir={agentDir}
            {...card}
            name={name}
            {...(file ? { path: file } : {})}
            placeholder={homePlaceholderFor(pane.noun)}
            recent={recent}
            onOpenSession={onOpenSession}
          />
        )}
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
        shot={viewing}
        paneNoun={pane.paneNoun}
        onClose={() => setViewing(null)}
        onDiscard={onDiscardShot}
      />
    </div>
   </CardPolicyProvider>
  );
}

export default ClaudeChat;
