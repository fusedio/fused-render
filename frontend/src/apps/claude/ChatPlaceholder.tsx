// ONE WAIT, ONE LOOK: the skeleton every chat embed shows before the chat is on
// screen — the `lazy` chunk loading (ChatMount's `Suspense`) and a host that is
// still deciding whether a chat goes here at all (the tasks wall resolving a
// card's folder). One user turn and one assistant turn in the chat's own
// geometry, so the only visible transition is skeleton → chat.
//
// Styles: styles/chat-frame.css (the class names keep the `chat-frame` prefix
// they were born with, when this skeleton covered a framed chat document).

/** The backstop every chat wait shares: reveal regardless after this long.
 *  Generous on purpose — it is a backstop for something that cannot answer,
 *  not a budget for something slow (`platform/lib/clock.GATE_FALLBACK_MS` is
 *  the same 8 s). */
export const CHAT_FRAME_FALLBACK_MS = 8000;

/** How long the skeleton's crossfade runs. Matches the CSS transition in
 *  chat-frame.css. */
export const CHAT_FRAME_FADE_MS = 160;

/** The skeleton in its own box. Same box, same shapes wherever it stands: to a
 *  reader every one of these waits is the same thing — a chat that is not on
 *  screen yet. */
export function ChatFramePlaceholder({ className }: { className?: string }) {
  return (
    <div className={className ? `chat-frame ${className}` : "chat-frame"}>
      <div
        className="chat-frame-placeholder"
        // A status region, not an alert: it says "this is coming", it does not
        // interrupt. `aria-busy` is the machine-readable half of the same claim.
        role="status"
        aria-busy="true"
        aria-label="Loading chat"
      >
        {/* Two turns and no more: a stand-in for "a conversation", not a guess
            at how long this one is (`.skel-bar` is the app's one shimmer,
            explorer.css). */}
        <div className="chat-frame-skel">
          <div className="chat-frame-skel-turn chat-frame-skel-user">
            <span className="skel-bar" />
          </div>
          <div className="chat-frame-skel-turn chat-frame-skel-assistant">
            <span className="chat-frame-skel-dot" />
            <span className="chat-frame-skel-lines">
              <span className="skel-bar" style={{ width: "88%" }} />
              <span className="skel-bar" style={{ width: "72%" }} />
              <span className="skel-bar" style={{ width: "50%" }} />
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
