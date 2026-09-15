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
 * THE CHUNK'S OWN FAILURE, which `Suspense` has no opinion about: a `lazy`
 * import that REJECTS throws from render, and with no boundary above it React
 * unmounts to the root — the reader loses the whole shell (explorer, tasks,
 * sidebar), not just the chat. And it is not a hypothetical: a tab left open
 * across a deploy asks for a hashed chunk that is no longer on disk, which is
 * exactly the case `__BUILD_VERSION__` exists for.
 *
 * So: two screens, and the second one is an APOLOGY WITH AN ACTION
 * (`ChatLoadFailed` below). There is no second implementation to degrade to any
 * more — the legacy template is gone — and the honest thing left to say is what
 * happened and the one press that fixes it: a reload fetches the manifest this
 * tab has never seen, and the conversation itself is on disk and untouched.
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
    // Not a toast: the fallback already says this to the reader, in the box
    // where the chat was — but a 404'd chunk is a deploy fact worth having in a
    // console too.
    console.error("chat chunk failed to load", error);
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

/**
 * THE CHUNK FAILURE'S OWN SCREEN — the boundary's fallback, in the app's own
 * error-card shapes (`.trouble-card`, styles/dialogs.css) rather than a look of
 * its own. Everything a reader needs is three things: that this box is a chat
 * that did not arrive, WHY it is a fact about the app rather than about their
 * conversation, and the press that ends it.
 *
 * No copy-the-details buttons and no troubleshooting link, unlike `TroubleCard`:
 * there is nothing verbatim to hand anybody, and the cause is known exactly —
 * this tab is asking for a build that is no longer on disk.
 *
 * ITS LOOK IS EAGER (`.chat-mount-failed`, frontend/src/styles/chat-frame.css,
 * imported by the shell barrel) and not in `apps/claude/styles/chat.css`, which
 * would be a stylesheet inside the very chunk that just failed to arrive: this
 * card would then be shown unstyled in exactly the one case it is ever shown.
 *
 * IT REPORTS READY. A host that holds the previous pane on screen until the
 * chat says `onReady` (the explorer content pane's held-frame swap) would
 * otherwise keep this card at opacity 0 until its swap timeout — Reload hidden
 * for exactly the wait it exists to shorten. The card IS the chat's final
 * state for this mount, so it completes the swap the way a loaded chat would.
 *
 * Exported for its own test: the boundary is only reachable from a chunk that
 * fails to load, which no host can stage.
 */
export function ChatLoadFailed({ onReady }: { onReady?: () => void }) {
  // Once per mount, through a ref: the card never changes after it appears,
  // and a host's `onReady` is a swap trigger, not a subscription — a host that
  // hands a fresh closure every render must not re-trigger the swap.
  const readyRef = useRef(onReady);
  readyRef.current = onReady;
  useEffect(() => {
    readyRef.current?.();
  }, []);
  return (
    <div className="chat-mount-failed">
      <div className="trouble-card" role="alert">
        <div className="trouble-title">This chat could not load.</div>
        <p className="trouble-explain">The app was updated. Reload to continue.</p>
        <div className="trouble-actions">
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
     <ChatChunkBoundary fallback={<ChatLoadFailed {...(props.onReady ? { onReady: props.onReady } : {})} />}>
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
