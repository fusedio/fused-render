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
  CardPolicyProvider,
  Composer,
  createCardPolicy,
  Home,
  openCardIds,
  resetCardPolicy,
  SentPop,
  Topbar,
  Transcript,
  TroubleView,
  useComposerDefaults,
  useRecentSessions,
  useTaskId,
  type TranscriptTail,
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
  /** Replaces the `parent.document` lookup for the annotate target (T:6117).
   *  Carried in PR1 so the hosts can already pass it; PR3 reads it. */
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
  const [watcher] = useState(() => createAppStateWatcher(() => paneFrame.current));
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
  const hasPane = useRef(false);
  hasPane.current = !pane.noPane && pane.status === "ready";

  // ── the controller ─────────────────────────────────────────────────────────
  // Rebuilt only for a new target: the model / effort / pane answers it reads at
  // SEND time all go through refs, so a keystroke in a picker cannot restart the
  // run loop.
  const liveModel = useRef("");
  const liveEffort = useRef("");
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
        hasPane: () => hasPane.current,
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
        // PR4 hangs the artifacts read and the snapshot invalidation here
        // (T:16229, 16321-16330); the ticks already run on T's clock.
        onArtifactsTick: () => {},
        onRunEnded: () => {},
      }),
    [agentDir, file, params],
  );
  useEffect(() => () => controller.dispose(), [controller]);

  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getState,
    controller.getState,
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
  });

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
        props.initialAsk ||
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
  const booted = useRef(false);
  /** The boot's ONE dispatch — the ask, or the restore — the moment it reaches
   *  the controller. What makes the re-arm below safe: a boot cancelled before
   *  it dispatched can be run again, one that dispatched never is. */
  const bootDispatched = useRef(false);
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
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
    const ask = props.initialAsk;
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
        // `adoptLiveRun(session_id)` — a live turn with no `run` on the URL — is
        // PR4 (design.md §8); until then a reopened chat re-attaches on its own
        // next send.
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
      if (!bootDispatched.current) booted.current = false;
    };
  }, [controller, params, props.initialAsk, markReady, cardPolicy]);

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

  const onSend = useCallback(
    (text: string, opts: SendOptions) => void controller.sendMessage(text, opts),
    [controller],
  );
  const onFollowUp = useCallback(
    (text: string) =>
      void controller.sendFollowUp(text, {
        model: defaults.model,
        effort: defaults.effort,
        permission: defaults.permission,
      }),
    [controller, defaults.model, defaults.effort, defaults.permission],
  );
  const onStop = useCallback(() => void controller.stopRun(), [controller]);
  const onBack = useCallback(() => {
    // A fresh transcript is a fresh card policy: an override from the
    // conversation that WAS on screen must not leak a card open in one the user
    // has never touched (ui/cardPolicy.ts).
    resetCardPolicy(cardPolicy);
    controller.newChat();
    setEntered(false);
  }, [controller, cardPolicy]);
  const onOpenSession = useCallback(
    (sessionId: string) => {
      resetCardPolicy(cardPolicy);
      setEntered(true);
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

  /** The raw wire of the newest user turn, for the kebab's "what was sent". */
  const lastRaw = useMemo(() => {
    for (let i = state.turns.length - 1; i >= 0; i--) {
      const t = state.turns[i];
      if (t.role === "user" && t.raw) return t.raw;
    }
    return undefined;
  }, [state.turns]);

  const name = file ? file.split(/[\\/]/).filter(Boolean).pop() || file : "Claude";
  const running =
    state.status === "running" || state.status === "starting" || state.status === "stopping";
  // The picker moves into the shared strip below the breakpoint: there is no
  // persistent left column to hang a bar on (`pickerHost`).
  const host = pickerHost(narrowView.narrow, pane.noPane, pane.decision?.leftModes.length ?? 0);
  const paneShown = !chatOnly && !pane.noPane;
  const stripShown = paneShown && narrowView.narrow;

  // MEMOIZED, like the two callbacks below it: a fresh object per render defeats
  // every `React.memo` in the tree it is handed to, and this one is handed to
  // the composer AND the landing card.
  // `card.back` is a snapshot of the address bar, so the memo has to see the
  // address bar move: without this the memoized object would hand the composer
  // a `back` from an earlier URL.
  const [urlTick, setUrlTick] = useState(0);
  useEffect(() => params.onChange(() => setUrlTick((n) => n + 1)), [params]);
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
      ...(stranded ? { restore: stranded } : {}),
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
      urlTick,
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
      <div className="c-chat" ref={columnRef}>
        {stripShown ? (
          // A row ABOVE both views, never over either, and the ONE row the
          // narrow rules never hide — it carries the way out of each view
          // (T:3833-3843). PR3 puts the annotate switch and the recorder here.
          <div className="c-anntools">
            {host === "anntools" ? (
              <LeftModePicker
                modes={pane.decision?.leftModes ?? []}
                params={params}
                leftMode={leftMode}
              />
            ) : null}
            <ViewToggle narrowView={narrowView} />
          </div>
        ) : null}
        {inChat ? (
          <>
            {/* Taken away by the compact and peek cuts: the card's own head and
                the popup's own head already say which task this is
                (T:1412-1438). */}
            {!compact && !peek ? (
              <Topbar
                agentDir={agentDir}
                file={file}
                sessionId={state.sessionId ?? ""}
                subtitle={name}
                {...(taskId ? { taskId } : {})}
                running={running}
                onBack={onBack}
                {...(lastRaw ? { lastRaw } : {})}
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
      <SentPop open={!!sent} onClose={() => setSent(null)} outgoing={sent?.raw ?? ""} />
    </div>
   </CardPolicyProvider>
  );
}

export default ClaudeChat;
