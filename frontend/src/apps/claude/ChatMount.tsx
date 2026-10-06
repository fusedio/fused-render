// THE ONE CHAT MOUNT every embed site calls (the tasks cards wall and its
// popup, the explorer sidebar and content pane, the folder listing pane, the
// canvases workspace): the native `<ClaudeChat/>` behind a `lazy` boundary,
// with a per-mount memory params store for the hosts that are not the page's
// own URL.
import { Component, lazy, Suspense, useEffect, useState, type MutableRefObject, type ReactNode } from "react";
import { TroubleCard } from "@platform/ui/TroubleCard";
import { ChatFramePlaceholder } from "./ChatPlaceholder";
import type { ClaudeAsk } from "./ClaudeChat";
import {
  createMemoryParamsStore,
  type ParamsSnapshot,
  type ParamsStore,
} from "./params/store";

/**
 * THE CODE-SPLIT BOUNDARY, and the reason it is here rather than anywhere else:
 * this is the one place that knows whether a chat is going to be rendered at
 * all. A static import made every host that merely CAN show a chat — the
 * explorer, the tasks wall, the canvases workspace — bundle the whole chat and
 * its markdown stack (marked + DOMPurify + highlight.js, 75 kB gz) into the
 * shell's entry graph, for routes that never mount one. `Suspense` covers the
 * chunk load with the chat's own skeleton.
 */
const ClaudeChat = lazy(() => import("./ClaudeChat"));

/**
 * THE CHUNK'S OWN FAILURE, which `Suspense` has no opinion about: a `lazy`
 * import that REJECTS throws from render, and with no boundary above it React
 * unmounts to the root — the reader loses the whole shell (explorer, tasks,
 * sidebar), not just the chat. And it is not a hypothetical: a tab left open
 * across a deploy asks for a hashed chunk that is no longer on disk, which is
 * exactly the case `__BUILD_VERSION__` exists for.
 *
 * So the box says what happened and offers the one thing that fixes it — a
 * reload fetches the current build's chunk. Plain platform UI, never anything
 * from the chunk that just failed to load.
 *
 * It also catches every OTHER render-time throw from inside the chat (a bug,
 * not a deploy). Those must not wear the deploy-skew copy — "older copy of the
 * app" sends the reader hunting for an update that does not exist — so only a
 * message that LOOKS like a chunk load (`isChunkLoadError`) gets it; the rest
 * get a plain "the chat hit an error" card with the message. Reload is still
 * the only recovery the boundary has for either, and the copy says so.
 */
/** The messages a failed dynamic import rejects with: Chromium, WebKit, and
 *  webpack/vite-style chunk loaders' own names for it. */
const CHUNK_LOAD = /Failed to fetch dynamically imported module|Importing a module script failed|ChunkLoadError|Loading chunk/i;

/** Did this render-time throw come from the chat's chunk failing to load? */
export function isChunkLoadError(message: string): boolean {
  return CHUNK_LOAD.test(message);
}

export class ChatChunkBoundary extends Component<
  { children: ReactNode },
  { error: string | null }
> {
  state: { error: string | null } = { error: null };
  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
  componentDidCatch(error: unknown) {
    console.error("chat failed to render", error);
  }
  render() {
    if (this.state.error === null) return <>{this.props.children}</>;
    const chunk = isChunkLoadError(this.state.error);
    return (
      // Inline, not chat.css: that sheet ships in the chunk that failed.
      <div
        className="chat-mount-failed"
        style={{
          flex: "1 1 auto",
          width: "100%",
          height: "100%",
          minWidth: 0,
          minHeight: 0,
          overflow: "auto",
          padding: 16,
          boxSizing: "border-box",
        }}
      >
        {chunk ? (
          <TroubleCard
            what="loading the chat"
            error={this.state.error}
            title="The chat didn't load"
            explain={
              "This window is running an older copy of the app than the one on " +
              "disk, or the app went away while it loaded. Reloading the page " +
              "fetches the current one."
            }
            onRetry={() => location.reload()}
            retryLabel="Reload the page"
          />
        ) : (
          <TroubleCard
            what="using the chat"
            error={this.state.error || "the chat failed to render"}
            title="The chat hit an error"
            explain={
              "Something inside the chat broke while it was drawing. Reloading " +
              "the page is the only way to bring it back. The conversation " +
              "itself is saved on disk and is not lost."
            }
            onRetry={() => location.reload()}
            retryLabel="Reload the page"
          />
        )}
      </div>
    );
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
  /**
   * `model` / `effort` — WHAT THIS CONVERSATION IS SET TO, stated by a host that
   * knows (the Tasks page's side peek, from the task's own row).
   *
   * A SEED, NOT A SYNC, and that is the whole of the difference from the three
   * ids above. The composer ranks
   * `record > param > detected > pref > constant` (ui/composer-defaults): these
   * two become the PARAMS, so they answer for a chat that has no record of its
   * own yet — a task set up in the New task card and not yet run — and are
   * outranked the moment it has one. Without them the composer fell through to
   * `detected`, which for a chat with no transcript is the model last used by
   * some OTHER chat in that folder: the peek showed a reader a model and an
   * effort they had never chosen (Akshil, 2026-09-18, "I saw the sidebar peek —
   * the values there were different").
   *
   * They are written ONCE, into the store's seed below, and never pushed again
   * — unlike `sessionId`/`runId`/`msgAnchor`, which `useHostIds` keeps in step.
   * These two are values the READER can change: the pills write the same two
   * params, and an effect that kept re-stating the host's answer would undo the
   * pick on the next render the listing caused (it re-reads every 20-30s).
   *
   * "" and absent are the same answer — "this host has no opinion" — and it is
   * the load-bearing one: it leaves detection speaking for every chat that is
   * not a task, which is most of them.
   */
  model?: string;
  effort?: string;
  /** `msg` — ONE TURN inside the conversation, to open scrolled to. The
   *  transcript stamps `data-msg` on every turn it draws and the param is spent
   *  the moment the named one is on screen (params/store.ts). Handed over by
   *  the Tasks list, whose expanded threads list the very turns this addresses
   *  (shell/ScheduleTaskViews `openMessage`). */
  msgAnchor?: string;
  /** The "Fix with AI" prompt, PULLED and cleared by the host before it is
   *  passed (explorer `takeClaudeAsk`) — so it reaches exactly one mount. */
  initialAsk?: ClaudeAsk;
  /** `_nofocus=1` (the frame-focus contract). */
  noFocus?: boolean;
  /** `_preview=1` (IS_PREVIEW thumbnails). */
  preview?: boolean;
  /** `_noopen=1` — do not record an app open (the listing pane). */
  noOpen?: boolean;
  /** "url" for sidebar / content / canvas; "memory" for cards / peek / panel /
   *  tab, whose page URL is not this chat's (00 §1e). */
  paramsSource: "url" | "memory";
  /**
   * A class for the mount's own box, where the host needs one. Site 6 is the
   * case: the explorer content pane's
   * held-frame swap decides which of two mounted panes is on screen with
   * `.preview-frame.is-shown`, and that has to ride whatever renders there.
   */
  mountClassName?: string;
  /** The transcript's first paint (00 §1e, "Ready signal"). */
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
    // …and the run settings, seeded and then left alone — see `model` above for
    // why these two are not in `useHostIds` beside the ids.
    if (props.model) seed.model = props.model;
    if (props.effort) seed.effort = props.effort;
    return createMemoryParamsStore(seed);
  });
  useHostIds(memory, props.sessionId, props.runId, props.msgAnchor);

  return (
   <ChatChunkBoundary>
    <div className={props.mountClassName ? `chat-mount ${props.mountClassName}` : "chat-mount"}>
     <Suspense fallback={<ChatFramePlaceholder />}>
      <ClaudeChat
        file={props.file}
        chatOnly={!!props.chatOnly}
        compact={!!props.compact}
        peek={!!props.peek}
        params={props.paramsSource === "url" ? "url" : memory}
        // WHOSE `?model=`/`?effort=` the composer is looking at. These two props
        // are the only way a seed is ever STATED — everything else in that pair
        // of params is the composer's own leftover — and a stated one keeps
        // outranking the global pair for a chat that has no session yet
        // (ui/composer-defaults `seedCounts`).
        {...(props.model || props.effort ? { hostSeededSettings: true } : {})}
        {...(props.sessionId ? { initialSessionId: props.sessionId } : {})}
        {...(props.runId ? { initialRunId: props.runId } : {})}
        {...(props.initialAsk ? { initialAsk: props.initialAsk } : {})}
        // `_preview=1` and `_nofocus=1` are the two host facts that mean "do not
        // take the keyboard": a thumbnail is display-only, and focus inside a
        // frame scrolls that frame into view (D348, platform/lib/frame-focus).
        autoFocus={!props.preview && !props.noFocus}
        {...(props.preview ? { preview: true } : {})}
        {...(props.noOpen ? { noOpen: true } : {})}
        {...(props.onReady ? { onReady: props.onReady } : {})}
        {...(props.onEscape ? { onEscape: props.onEscape } : {})}
        {...(props.focusRef ? { focusRef: props.focusRef } : {})}
        {...(props.annotateTarget ? { annotateTarget: props.annotateTarget } : {})}
        {...(props.onNavigate ? { onNavigate: props.onNavigate } : {})}
        {...(props.recap ? { recap: true } : {})}
      />
     </Suspense>
    </div>
   </ChatChunkBoundary>
  );
}
