// THE ONE MOUNT every chat embed site calls: the native `<ClaudeChat/>`, with
// the per-mount param store, the host ids that arrive later, the code-split
// boundary and the chunk's own failure screen around it. Props are what the six
// embed sites need from a chat (00 §1a/§1b) — a target, the view cuts
// (`chatOnly`/`compact`/`peek`), where params live, and the handful of signals a
// host wires back (`onReady`, `onEscape`, `focusRef`).
//
// There is no second implementation behind it any more: the legacy
// `templates/claude` iframe and the `native_chat_enabled` flag that chose
// between them are gone, so every site here renders the same chat.
import {
  Component,
  Fragment,
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  type MutableRefObject,
  type ReactNode,
} from "react";
import type { ClaudeAsk } from "./ClaudeChat";
import { ChatFramePlaceholder } from "./ui/ChatPlaceholder";
import {
  createMemoryParamsStore,
  type ParamsSnapshot,
  type ParamsStore,
} from "./params/store";

/**
 * THE CODE-SPLIT BOUNDARY, and the reason it is here rather than anywhere else:
 * this is the one place that knows whether a chat is going to be rendered at
 * all. A static import made every host that merely CAN frame a chat — the
 * explorer, the tasks wall, the canvases workspace — bundle the whole native
 * chat and its markdown stack (marked + DOMPurify + highlight.js, 75 kB gz)
 * into the shell's entry graph for routes that never mount one.
 *
 * The wait it costs is a wait the reader already had: `Suspense` falls back to
 * the same skeleton the chat itself holds over its own boot (ui/ChatPlaceholder),
 * so the transition is skeleton → chat either way.
 */
const ClaudeChat = lazy(() => import("./ClaudeChat"));

/** The cover for the chunk's wait. Classless on purpose: it is INSIDE
 *  `.chat-mount`, which already has the host's box, so it needs no geometry of
 *  its own — and a host class handed to it as well would be that box's layout
 *  applied twice, one inside the other, which is how the cover and the chat
 *  that replaces it come to sit in different places and pop on the swap. */
const placeholderFor = () => <ChatFramePlaceholder />;

/**
 * WHAT A FAILED DYNAMIC IMPORT ACTUALLY SAYS, so the boundary below can tell a
 * chunk that never arrived from a chat that threw while rendering. Two screens
 * hang off this answer, and they have opposite advice, so guessing is worse
 * than either.
 *
 * Vite 6 produces exactly two kinds of message here (checked against this
 * repo's own `node_modules/vite@6.4.3`, the preload helper that
 * `buildImportAnalysisPlugin` emits into every chunk —
 * `dist/node/chunks/dep-Dm0c1Wj2.js`):
 *
 *   1. its OWN, for a stylesheet dependency that 404s:
 *        `Unable to preload CSS for ${dep}`        (that file, ~:45475)
 *   2. the BROWSER'S, rethrown verbatim — the helper ends in
 *      `baseModule().catch(handlePreloadError)` (~:45495) and `handlePreloadError`
 *      re-`throw`s whatever `import()` rejected with. That is a `TypeError`
 *      whose text is engine-specific:
 *        Chromium  `Failed to fetch dynamically imported module: <url>`
 *        Firefox   `error loading dynamically imported module: <url>`
 *        WebKit    `Importing a module script failed.`
 *
 * `Loading chunk N failed` / `Loading CSS chunk N failed` are WEBPACK's wording,
 * not Vite's, and this app is built by Vite — they are matched anyway because
 * the cost is a few characters and the alternative is this check silently
 * degrading to "crash" the day the bundler changes under it.
 *
 * MESSAGE-MATCHING IS THE ONLY HANDLE THERE IS: every one of these arrives as a
 * plain `TypeError`/`Error` with no code, no name and no cause to key on. So
 * the rule is deliberately one-way — a message that matches is a chunk failure;
 * ANYTHING ELSE, including something that fails to stringify, is treated as a
 * crash, because the crash screen's advice ("try again, then reload") is safe
 * for a chunk failure while the deploy screen's ("reload, you are out of date")
 * is a lie about a chat that threw.
 */
const CHUNK_LOAD_MESSAGE =
  /failed to fetch dynamically imported module|importing a module script failed|error loading dynamically imported module|unable to preload css for|loading chunk .* failed|loading css chunk/i;

export function isChunkLoadError(error: unknown): boolean {
  const message =
    error instanceof Error ? error.message : typeof error === "string" ? error : "";
  return CHUNK_LOAD_MESSAGE.test(message);
}

/**
 * THE CHUNK'S OWN FAILURE, which `Suspense` has no opinion about: a `lazy`
 * import that REJECTS throws from render, and with no boundary above it React
 * unmounts to the root — the reader loses the whole shell (explorer, tasks,
 * sidebar), not just the chat. And it is not a hypothetical: a tab left open
 * across a deploy asks for a hashed chunk that is no longer on disk, which is
 * exactly the case `__BUILD_VERSION__` exists for.
 *
 * BUT IT CATCHES MORE THAN THAT, and used to admit to none of it: a boundary
 * here is above the WHOLE chat, so every render throw inside `ClaudeChat` — a
 * transcript that hit a bad shape, a controller that read an undefined field —
 * lands in it too, and the one screen it had said "The app was updated. Reload
 * to continue." A reader who reloads on that advice gets the same crash from
 * the same build, and has been told the wrong thing about their own app.
 *
 * So the error is KEPT (`isChunkLoadError` above sorts it) and the fallback is
 * a function of it — two screens with different copy and different actions
 * (`ChatLoadFailed` below). `reset` is what the crash screen's "Try again"
 * calls: it clears the error AND bumps `attempt`, which is the child subtree's
 * `key`, so React builds a genuinely fresh tree rather than re-rendering the
 * one that just threw. (The chunk case is not offered a retry: `lazy` caches
 * its rejected promise, so re-rendering it throws the same rejection straight
 * back and the only real fix is the reload.)
 */
export interface ChatFailure {
  /** Which screen to show — see `isChunkLoadError`. */
  kind: "chunk" | "crash";
  /** The thrown value, for the crash screen's own line. */
  error: unknown;
  /** Drop the error and rebuild the subtree from scratch. */
  reset: () => void;
}

export class ChatChunkBoundary extends Component<
  { fallback: (failure: ChatFailure) => ReactNode; children: ReactNode },
  { error: unknown; failed: boolean; attempt: number }
> {
  state = { error: null as unknown, failed: false, attempt: 0 };
  static getDerivedStateFromError(error: unknown) {
    return { error, failed: true };
  }
  componentDidCatch(error: unknown) {
    // Not a toast: the fallback already says this to the reader, in the box
    // where the chat was — but a chat that died is worth having in a console
    // too, ONCE and in full, whichever kind it turned out to be. (`error` in
    // state is only ever read for its message; this is the whole object.)
    console.error("chat failed to render", error);
  }
  reset = () => {
    this.setState((s) => ({ error: null, failed: false, attempt: s.attempt + 1 }));
  };
  render() {
    if (this.state.failed) {
      return (
        <>
          {this.props.fallback({
            kind: isChunkLoadError(this.state.error) ? "chunk" : "crash",
            error: this.state.error,
            reset: this.reset,
          })}
        </>
      );
    }
    // KEYED ON THE ATTEMPT, so "Try again" is a remount and not a re-render:
    // the subtree that threw is thrown away with its state, and the chat boots
    // the way it does on arrival. Nothing of the conversation rides on it — the
    // transcript is the server's, and the mount re-reads it.
    return <Fragment key={this.state.attempt}>{this.props.children}</Fragment>;
  }
}

/**
 * A HOST ID THAT ARRIVES LATER, pushed into the mount's own store as the write
 * it is — ONE EFFECT PER KEY, and that is the whole reason there are two. A
 * single effect over `[sessionId, runId]` re-runs its whole body when EITHER
 * changes, so a new `sessionId` (the tasks listing re-reads every 20-30 s and
 * hands a card a fresh one routinely) re-wrote `run` as well — resurrecting a
 * run the controller had already ended and cleared (`clearRunParam`), which
 * `resumeRun` then polls for as a dead id. Each key only ever moves for its own
 * prop, and only when the store does not already hold that value.
 *
 * Exported for its own test: the store is per-mount and internal, so effect
 * DEPS — the actual bug — are only observable through the hook.
 */
export function useHostIds(
  memory: ParamsStore,
  sessionId?: string,
  runId?: string,
  msgAnchor?: string,
) {
  useEffect(() => {
    if (sessionId && memory.get("session_id") !== sessionId) {
      memory.set({ session_id: sessionId });
    }
  }, [memory, sessionId]);
  useEffect(() => {
    if (runId && memory.get("run") !== runId) memory.set({ run: runId });
  }, [memory, runId]);
  // …and the anchor, which unlike the two above can be re-handed for a
  // conversation that is ALREADY open: pressing a second message row in the
  // same thread swaps nothing but this.
  useEffect(() => {
    if (msgAnchor && memory.get("msg") !== msgAnchor) memory.set({ msg: msgAnchor });
  }, [memory, msgAnchor]);
}

/** The crash screen's one line of evidence. ONE LINE and capped: this sits in a
 *  box that can be a 300px card, and a stack trace folded into it would push the
 *  actions the reader came for below the fold. The full object went to the
 *  console in `componentDidCatch` — that is where a stack belongs. */
const DETAIL_MAX = 200;
export function failureDetail(error: unknown): string {
  const raw =
    error instanceof Error ? error.message : error == null ? "" : String(error);
  // Newlines and runs of space collapse first, so the cap counts CONTENT rather
  // than the indentation of a wrapped traceback.
  const line = raw.replace(/\s+/g, " ").trim();
  return line.length > DETAIL_MAX ? line.slice(0, DETAIL_MAX - 1) + "\u2026" : line;
}

/**
 * THE FAILURE'S OWN SCREEN — the boundary's fallback, in the app's own
 * error-card shapes (`.trouble-card`, styles/dialogs.css) rather than a look of
 * its own. Everything a reader needs is three things: that this box is a chat
 * that is not there, WHY, and the press that ends it.
 *
 * TWO KINDS, because the boundary above catches two things and they want
 * opposite advice (see `ChatChunkBoundary`):
 *
 *   `chunk` — the JS never arrived. This tab is asking for a hashed file that a
 *     deploy took off disk, so the app it is running is the stale thing: there
 *     is nothing to retry (a second render asks `lazy` for the same cached
 *     rejection), and a reload is the whole fix. One action, and the copy names
 *     the cause rather than the symptom.
 *
 *   `crash` — the chat loaded and threw while rendering. Reloading fetches the
 *     same build and, most likely, the same crash; saying "the app was updated"
 *     here is simply false. So: what actually happened, verbatim, and TWO
 *     actions — Try again first, because a render throw is often about one
 *     conversation's state rather than the code, and the boundary's reset
 *     rebuilds the subtree from nothing (the conversation is the server's; a
 *     remount re-reads it and loses none of it). Reload stays as the harder
 *     press behind it.
 *
 * No copy-the-details button and no troubleshooting link, unlike `TroubleCard`:
 * this is a box beside the reader's work, not a boot failure, and the verbatim
 * text is already on the card and in the console.
 *
 * ITS LOOK IS EAGER (`.chat-mount-failed`, frontend/src/styles/chat-frame.css,
 * imported by the shell barrel) and not in `apps/claude/styles/chat.css`, which
 * would be a stylesheet inside the very chunk that just failed to arrive: this
 * card would then be shown unstyled in exactly the one case it is ever shown.
 * `.trouble-error` is eager for the same reason (styles/dialogs.css).
 *
 * IT REPORTS READY. A host that holds the previous pane on screen until the
 * chat says `onReady` (the explorer content pane's held-frame swap) would
 * otherwise keep this card at opacity 0 until its swap timeout — the actions
 * hidden for exactly the wait they exist to shorten. The card IS the chat's
 * final state for this mount, so it completes the swap the way a loaded chat
 * would.
 *
 * Exported for its own test: the boundary is only reachable from a chunk that
 * fails to load, which no host can stage.
 */
export function ChatLoadFailed({
  kind = "chunk",
  error,
  onRetry,
  onReady,
}: {
  kind?: "chunk" | "crash";
  error?: unknown;
  onRetry?: () => void;
  onReady?: () => void;
}) {
  // Once per mount, through a ref: the card never changes after it appears,
  // and a host's `onReady` is a swap trigger, not a subscription — a host that
  // hands a fresh closure every render must not re-trigger the swap.
  const readyRef = useRef(onReady);
  readyRef.current = onReady;
  useEffect(() => {
    readyRef.current?.();
  }, []);
  const detail = kind === "crash" ? failureDetail(error) : "";
  return (
    <div className="chat-mount-failed">
      <div className="trouble-card" role="alert">
        <div className="trouble-title">
          {kind === "crash" ? "This chat hit an error." : "This chat could not load."}
        </div>
        <p className="trouble-explain">
          {kind === "crash"
            ? "Something in the conversation could not be drawn. Trying again rebuilds it; nothing of the conversation is lost."
            : "The app was updated. Reload to continue."}
        </p>
        {/* Only when there is something to show: an error that stringifies to
            nothing would otherwise draw an empty framed box under the sentence
            that promised an explanation. */}
        {detail && <pre className="trouble-error">{detail}</pre>}
        <div className="trouble-actions">
          {kind === "crash" && onRetry && (
            <button type="button" className="version-panel-link" onClick={onRetry}>
              Try again
            </button>
          )}
          <button
            type="button"
            className="version-panel-link"
            onClick={() => location.reload()}
          >
            Reload
          </button>
        </div>
      </div>
    </div>
  );
}

export interface ChatMountProps {
  /** `_file` — the target folder/file. */
  file: string | null;
  /** No preview column of the chat's own (sites 1-5). */
  chatOnly?: boolean;
  /** The cards wall's read-only cut: no top bar, no composer. */
  compact?: boolean;
  /** The popup's cut: no strip, no top bar (its head says all of that). */
  peek?: boolean;
  /** The conversation to open (cards, peek). */
  sessionId?: string;
  /** `run` handed over by a host (the canvas fix run). */
  runId?: string;
  /** `msg` — ONE TURN inside the conversation, to open scrolled to. The
   *  transcript stamps `data-msg` on every turn it draws and the param is spent
   *  the moment the named one is on screen (params/store.ts). Handed over by
   *  the Tasks list, whose expanded threads list the very turns this addresses
   *  (shell/ScheduleTaskViews `openMessage`). */
  msgAnchor?: string;
  /** The "Fix with AI" prompt, PULLED and cleared by the host before it is
   *  passed (explorer `takeClaudeAsk`) — so it reaches exactly one mount. */
  initialAsk?: ClaudeAsk;
  /** The target's bytes come from a mount (the sidebar under one). */
  remote?: boolean;
  /** Do not take the keyboard (the host's focus contract). */
  noFocus?: boolean;
  /** Display-only — a thumbnail shell (IS_PREVIEW). */
  preview?: boolean;
  /** Do not record an app open (the listing pane). */
  noOpen?: boolean;
  /** WHERE THIS CONVERSATION'S PARAMS LIVE. "url" for the sidebar / content
   *  pane / canvas, whose page IS the chat; "memory" for cards / peek / panel /
   *  tab, where several chats share one URL and each needs its own `run` /
   *  `session_id` — what the iframe's `_fusedParamBoundary` used to buy. */
  paramsSource: "url" | "memory";
  /**
   * A class for the NATIVE mount's own box, where the host needs one that is
   * not frame geometry. Site 6 is the case: the explorer content pane's
   * held-frame swap decides which of two mounted panes is on screen with
   * `.preview-frame.is-shown`, and that has to ride whatever renders there.
   */
  mountClassName?: string;
  /** The transcript's first paint. */
  onReady?: () => void;
  /** Esc with nothing of the chat's own open — TaskPeek's close. */
  onEscape?: () => void;
  /** The composer's textarea, for `Modal initialFocus`. */
  focusRef?: MutableRefObject<HTMLTextAreaElement | null>;
  /** PR3's annotate target — the sibling workbench iframe (canvas). */
  annotateTarget?: () => HTMLIFrameElement | null;
  /** Replaces `window.top.location` hops; defaults to `navigateUrl`. */
  onNavigate?: (url: string) => void;
  /**
   * "While you were away" — OPT-IN, and off unless this is the chat the reader
   * opened (ClaudeChat's `recap`, .claude-design/session-recap.md).
   *
   * Every mount on the page hears the same window `focus`, so a default-on
   * recap made one return into one model call PER MOUNT — seven, on a tasks
   * wall. Passing it is a host saying "this is the full chat, in front of the
   * reader": the explorer's content pane and its claude sidebar. Never a card,
   * a peek, a thumbnail or a listing preview.
   */
  recap?: boolean;
  /**
   * DROP THE UPCOMING LANE FROM THIS MOUNT'S "Recent chats" — `ClaudeChat`'s
   * own prop (and `Lists`' before that), carried through untouched. Set by
   * exactly one host today: the explorer's Claude side panel (Preview.tsx's
   * `?_side=claude` sidebar and ListingPreviewPane's folder pane) — the
   * reader opened it to talk about the thing already on screen, not to be
   * shown a queue of scheduled-for-later work and drafts sitting beside it.
   */
  hideUpcoming?: boolean;
}

export function ChatMount(props: ChatMountProps) {
  // ONE MEMORY STORE PER MOUNT, and per mount is the whole of it: the store
  // holds the live `run` / `permission` / `paneview` of the conversation on
  // screen, and re-seating it re-creates the controller under a running turn.
  // `useMemo` is the wrong primitive twice over — React may drop a memo at
  // will, and the seed object it would key on is fresh on every render where a
  // host's `sessionId`/`runId` changed (the tasks listing re-reads every
  // 20-30 s). So: built once, in a lazy state initializer, and a host id that
  // arrives LATER is pushed in as a write, which is what it is.
  const [memory] = useState(() => {
    const seed: ParamsSnapshot = {};
    if (props.sessionId) seed.session_id = props.sessionId;
    if (props.runId) seed.run = props.runId;
    if (props.msgAnchor) seed.msg = props.msgAnchor;
    return createMemoryParamsStore(seed);
  });
  useHostIds(memory, props.sessionId, props.runId, props.msgAnchor);

  // THE BOX IS OUTERMOST, and the boundary and the wait are both INSIDE it.
  // With the boundary wrapped around the div instead, a chunk that failed
  // replaced the div wholesale — and with it the host's own class. Site 6 is
  // where that shows: the explorer content pane decides which of its mounted
  // panes is on screen with `.preview-frame.is-shown`, so a failure card
  // rendered without those classes is not positioned in the pane at all. The
  // box is host geometry; nothing about the chunk's fate should be able to take
  // it away.
  return (
    <div className={props.mountClassName ? `chat-mount ${props.mountClassName}` : "chat-mount"}>
     <ChatChunkBoundary
       fallback={(failure) => (
         <ChatLoadFailed
           kind={failure.kind}
           error={failure.error}
           onRetry={failure.reset}
           {...(props.onReady ? { onReady: props.onReady } : {})}
         />
       )}
     >
      <Suspense fallback={placeholderFor()}>
        <ClaudeChat
          file={props.file}
          chatOnly={!!props.chatOnly}
          compact={!!props.compact}
          peek={!!props.peek}
          params={props.paramsSource === "url" ? "url" : memory}
          {...(props.sessionId ? { initialSessionId: props.sessionId } : {})}
          {...(props.runId ? { initialRunId: props.runId } : {})}
          {...(props.initialAsk ? { initialAsk: props.initialAsk } : {})}
          // `_preview=1` and `_nofocus=1` are the two host facts that mean "do not
          // take the keyboard": a thumbnail is display-only, and focus inside a
          // frame scrolls that frame into view (D348, platform/lib/frame-focus).
          autoFocus={!props.preview && !props.noFocus}
          {...(props.remote ? { remote: true } : {})}
          {...(props.preview ? { preview: true } : {})}
          {...(props.noOpen ? { noOpen: true } : {})}
          {...(props.onReady ? { onReady: props.onReady } : {})}
          {...(props.onEscape ? { onEscape: props.onEscape } : {})}
          {...(props.focusRef ? { focusRef: props.focusRef } : {})}
          {...(props.annotateTarget ? { annotateTarget: props.annotateTarget } : {})}
          {...(props.onNavigate ? { onNavigate: props.onNavigate } : {})}
          {...(props.recap ? { recap: true } : {})}
          {...(props.hideUpcoming ? { hideUpcoming: true } : {})}
        />
      </Suspense>
     </ChatChunkBoundary>
    </div>
  );
}
