// THE FLAG SWITCH every chat embed site calls: the native `<ClaudeChat/>` when
// `native_chat_enabled` is on, else the legacy `<ChatFrame/>` iframe EXACTLY as
// today — same `src`, same class, same title, same `frameRef`, same `onLoad`.
// Props are the union of what the 4 ChatFrame sites and the 2 plain-iframe sites
// pass (00 §1a/§1b); the URL shapes themselves live in `legacy-src.ts` behind a
// byte-for-byte parity test.
//
// WHY THE HOST STILL BUILDS `legacySrc` rather than this component building it:
// the two wrappers around it — `withNoFocus` and `revSrc` — are facts about the
// HOST (a thumbnail shell, a git revision being previewed), and the sites that
// need them apply them at their own level today. Threading them through here
// would buy nothing and would make the parity guard argue about wrappers
// instead of about addresses.
import {
  Component,
  lazy,
  Suspense,
  useEffect,
  useState,
  type MutableRefObject,
  type ReactNode,
} from "react";
import { ChatFrame, ChatFramePlaceholder } from "@platform/ui/ChatFrame";
import type { ClaudeAsk } from "./ClaudeChat";
import { useNativeChatFlag } from "./feature-flag";
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
 * into the shell's entry graph, flag off, for routes that never mount one.
 *
 * The tri-state flag already has to hold a placeholder over this box while the
 * prefs read is in flight (feature-flag.ts), so the wait a `lazy` chunk needs
 * is a wait that already exists: `Suspense` falls back to the same skeleton, and
 * the reader sees ONE wait either way.
 */
const ClaudeChat = lazy(() => import("./ClaudeChat"));

/** The one cover for both waits: the flag read, and the chunk. The class is
 *  passed ONLY where this placeholder stands in for the legacy FRAME itself —
 *  the flag-unknown return, where no branch has been chosen yet and the box has
 *  to hold a frame's geometry. Inside `.chat-mount` it must not be: the frame
 *  classes are frame geometry (`.task-card-frame` lays out at 133.33% and draws
 *  at `scale(0.75)`), and a cover wearing them inside the native box is scaled
 *  twice and pops when the real chat lands. */
const placeholderFor = (className?: string) => (
  <ChatFramePlaceholder {...(className ? { className } : {})} />
);

/**
 * THE CHUNK'S OWN FAILURE, which `Suspense` has no opinion about: a `lazy`
 * import that REJECTS throws from render, and with no boundary above it React
 * unmounts to the root — the reader loses the whole shell (explorer, tasks,
 * sidebar), not just the chat. And it is not a hypothetical: a tab left open
 * across a deploy asks for a hashed chunk that is no longer on disk, which is
 * exactly the case `__BUILD_VERSION__` exists for.
 *
 * So: two screens, and the second one is the LEGACY FRAME. The template is
 * still on disk and still serves this conversation, so falling back to it is
 * honest degradation rather than an apology — the same node the flag-off branch
 * renders, geometry class and all, built by the same `legacyBranch`.
 */
export class ChatChunkBoundary extends Component<
  { fallback: ReactNode; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: unknown) {
    // Not a toast: the fallback IS the chat, so nothing is lost for the reader
    // to act on — but a 404'd chunk is a deploy fact worth having in a console.
    console.error("native chat chunk failed to load; using the legacy frame", error);
  }
  render() {
    return <>{this.state.failed ? this.props.fallback : this.props.children}</>;
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
export function useHostIds(memory: ParamsStore, sessionId?: string, runId?: string) {
  useEffect(() => {
    if (sessionId && memory.get("session_id") !== sessionId) {
      memory.set({ session_id: sessionId });
    }
  }, [memory, sessionId]);
  useEffect(() => {
    if (runId && memory.get("run") !== runId) memory.set({ run: runId });
  }, [memory, runId]);
}

/** The flag-off element, and the boundary's fallback: one builder so the two
 *  cannot drift. Exported for the test that pins the fallback — the boundary is
 *  only reachable from a chunk that fails to load, which no host can stage. */
export function legacyBranch(props: ChatMountProps) {
  if (props.legacy !== undefined) return <>{props.legacy}</>;
  return (
    <ChatFrame
      src={props.legacySrc}
      title={props.title ?? "Claude"}
      className={props.className}
      {...(props.legacyFrameRef ? { frameRef: props.legacyFrameRef } : {})}
      onLoad={props.onReady}
    />
  );
}

export interface ChatMountProps {
  /** `_file` — the target folder/file. */
  file: string | null;
  /** `chat_only=1` (sites 1-5). */
  chatOnly?: boolean;
  /** `compact=1` (cards wall). */
  compact?: boolean;
  /** `peek=1` (TaskPeek). */
  peek?: boolean;
  /** `session_id` on the frame URL (cards, peek). */
  sessionId?: string;
  /** `run` handed over by a host (the canvas fix run). */
  runId?: string;
  /** The "Fix with AI" prompt, PULLED and cleared by the host before it is
   *  passed (explorer `takeClaudeAsk`) — so it reaches exactly one mount. */
  initialAsk?: ClaudeAsk;
  /** `_remote=1` (sidebar under a mount). */
  remote?: boolean;
  /** `_nofocus=1` (the frame-focus contract). */
  noFocus?: boolean;
  /** `_preview=1` (IS_PREVIEW thumbnails). */
  preview?: boolean;
  /** `_noopen=1` — do not record an app open (the listing pane). */
  noOpen?: boolean;
  /** "url" for sidebar / content / canvas; "memory" for cards / peek / panel /
   *  tab — what the iframe's `_fusedParamBoundary` used to buy (00 §1e). */
  paramsSource: "url" | "memory";
  /** The `/render?path=…` URL the legacy iframe loads today (`legacy-src.ts`).
   *  Used to build the flag-off `<ChatFrame>` for the four sites that had one. */
  legacySrc: string;
  /**
   * THE FLAG-OFF ELEMENT, VERBATIM — for the two sites that framed the template
   * with a PLAIN `<iframe>` rather than a `<ChatFrame>` (the canvases workspace
   * and the explorer content pane, 00 §1b). Those two carry attributes no chat
   * cover ever had — `allow="display-capture"`, the annotate and revision marks,
   * the held-frame swap's `is-shown` class — and rebuilding them from props here
   * would be five more props and a worse guarantee than handing the element over.
   * When given it wins outright, so the flag off is the same node it always was.
   */
  legacy?: ReactNode;
  /**
   * The LEGACY iframe's class (`.task-card-frame`, `.task-peek-frame`,
   * `.preview-side-frame`, `.pane-frame`) — and legacy only, deliberately. Those
   * rules are geometry for a FRAME: `.task-card-frame` lays out at 133.33% and
   * draws at `scale(0.75)`, which is precisely the trick the native compact
   * variant replaces with a type scale (styles/chat.css). Stamping it on the
   * native mount would apply both and shrink the card twice.
   */
  className?: string;
  /**
   * A class for the NATIVE mount's own box, where the host needs one that is
   * not frame geometry. Site 6 is the case: the explorer content pane's
   * held-frame swap decides which of two mounted panes is on screen with
   * `.preview-frame.is-shown`, and that has to ride whatever renders there.
   */
  mountClassName?: string;
  title?: string;
  /** The legacy frame's `load`; natively, the transcript's first paint — which
   *  is what `dataset.chatReady` used to say (00 §1e, "Ready signal"). */
  onReady?: () => void;
  /** LEGACY ONLY, and named for it: TaskPeek's Esc listener and `Modal
   *  initialFocus` both wanted the iframe ELEMENT. Natively those are
   *  `onEscape` and `focusRef`, and there is no iframe to hand back — so this
   *  stays null with the flag on rather than a host quietly reading a ref that
   *  the native branch was never going to fill. */
  legacyFrameRef?: MutableRefObject<HTMLIFrameElement | null>;
  /** Natively: Esc with nothing of the chat's own open — TaskPeek's close. */
  onEscape?: () => void;
  /** Natively: the composer's textarea, for `Modal initialFocus`. */
  focusRef?: MutableRefObject<HTMLTextAreaElement | null>;
  /** PR3's annotate target — the sibling workbench iframe (canvas). */
  annotateTarget?: () => HTMLIFrameElement | null;
  /** Replaces `window.top.location` hops; defaults to `navigateUrl`. */
  onNavigate?: (url: string) => void;
}

export function ChatMount(props: ChatMountProps) {
  const native = useNativeChatFlag();
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
    return createMemoryParamsStore(seed);
  });
  useHostIds(memory, props.sessionId, props.runId);

  // NEITHER BRANCH while the flag read is in flight. A `false` here is not
  // "legacy": it is "we have not asked yet", and mounting the legacy template
  // on it boots a whole `/render` document that drains the pending ask and
  // starts a poll before being thrown away (feature-flag.ts's header). The
  // placeholder is the same skeleton `ChatFrame` holds over a booting frame, so
  // the wait the reader sees is one wait either way.
  if (native === null) return placeholderFor(props.className);
  if (!native) return legacyBranch(props);
  return (
   <ChatChunkBoundary fallback={legacyBranch(props)}>
    <div className={props.mountClassName ? `chat-mount ${props.mountClassName}` : "chat-mount"}>
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
      />
     </Suspense>
    </div>
   </ChatChunkBoundary>
  );
}
