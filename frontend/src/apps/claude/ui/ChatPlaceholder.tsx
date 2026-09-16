// ONE WAIT, ONE LOOK: the skeleton every chat embed shows while there is no
// transcript on screen yet — the `lazy` chunk resolving (ChatMount's Suspense),
// a card still resolving which folder it is about (TaskCards), and the chat's
// own boot before its first paint (ClaudeChat).
//
// The problem it exists for: an embedded chat used to show several states on
// several clocks — the host's "Starting…" text, a cold document, a skeleton,
// then the chat. On a wall of twelve cards they popcorn. Here every host draws
// the SAME skeleton in the same geometry, so the only visible transition is
// skeleton → chat rather than a redraw.
//
// NEVER BROKEN outranks the wait, which is what the backstop below is for: a
// boot that cannot answer is revealed anyway rather than leaving a permanently
// blank pane.

/** Give up waiting and show what we have after this long. Generous on purpose:
 *  it is a backstop for a boot that cannot answer, not a budget for a slow one
 *  — a transcript that takes six seconds to restore should still crossfade
 *  rather than tear. */
export const CHAT_FRAME_FALLBACK_MS = 8000;

/** The skeleton in its own box — the `resolving` state, where there is nothing
 *  to mount a chat for yet. Same box, same shapes as a chat that is booting,
 *  because to a reader they ARE the same thing (a chat that is not on screen
 *  yet) and were only ever two states to the code.
 *
 *  The class is passed only where this stands in for a box the host has already
 *  sized; inside `.chat-mount` it must not be. */
export function ChatFramePlaceholder({ className }: { className?: string }) {
  return (
    <div className={className ? `chat-frame ${className}` : "chat-frame"}>
      <PlaceholderBody />
    </div>
  );
}

/** One user turn and one assistant turn (a 34%-wide 34px pill hard right, then
 *  a 24px avatar beside three lines at 88/72/50%), in the app's own shimmer
 *  (`.skel-bar`, explorer.css — one animation for every skeleton in the shell).
 *
 *  Two turns and no more: it is a stand-in for "a conversation", not a guess at
 *  how long this one is, and a taller stack of bars reads as a claim about the
 *  content (design.md, out of scope: matching the real turn count). */
function PlaceholderBody() {
  return (
    <div
      className="chat-frame-placeholder"
      // A status region, not an alert: it says "this is coming", it does not
      // interrupt. `aria-busy` is the machine-readable half of the same claim.
      role="status"
      aria-busy="true"
      aria-label="Loading chat"
    >
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
  );
}
