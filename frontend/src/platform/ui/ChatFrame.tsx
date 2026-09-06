// ONE WAIT, ONE LOOK: the host-owned placeholder that covers a chat frame from
// the moment it mounts until the framed document says its transcript is painted.
//
// The problem it exists for: an embedded chat used to show FOUR states on three
// clocks — the host's "Starting…" text, the iframe's own blank/black cold
// document, the template's skeleton, then the chat. On a wall of twelve cards
// they popcorn, and two of the four are pure overhead. Here the host draws ONE
// skeleton, shaped like the template's own (`renderLogSkeleton`), and holds it
// over an `opacity: 0` iframe until the frame reports ready — so the only
// visible transition is skeleton → chat, a crossfade of identical geometry
// rather than a redraw.
//
// THE READY SIGNAL IS AN ATTRIBUTE, NOT A MESSAGE (D3/D4). /render is always
// same-origin, so the sanctioned channel between a host and a framed template
// in this app is a direct `contentDocument` read — the same thing Preview.tsx
// does to lift a rendered page's <title>. The chat template stamps
// `document.documentElement.dataset.chatReady = "1"` once its transcript is
// painted (and immediately on the landing / no-session paths, which have
// nothing to restore). This component reads that attribute on `load` and, if it
// is not there yet, watches for it with a MutationObserver. No postMessage, no
// handshake, nothing for a template author to remember to answer.
//
// NEVER BROKEN outranks the wait: a frame that never stamps — a cross-origin
// document, a template that does not know the contract, a boot that threw — is
// revealed anyway, by the null-contentDocument branch on `load` or by the 8s
// fallback timer. The worst case is the old behaviour, never a permanently
// blank pane.
import { useCallback, useEffect, useRef, useState } from "react";

/** The attribute the framed chat stamps on its `<html>` when its transcript is
 *  on screen. Written by fused_render/templates/claude/template.html; read
 *  here. The pair is the whole contract. */
export const CHAT_READY_ATTR = "data-chat-ready";

/** Reveal the frame regardless after this long. Generous on purpose: it is a
 *  backstop for a template that cannot answer, not a budget for a slow one —
 *  a transcript that takes six seconds to restore should still crossfade
 *  rather than tear. */
export const CHAT_FRAME_FALLBACK_MS = 8000;

/** How long the crossfade runs. Matches the CSS transition in chat-frame.css;
 *  the placeholder is unmounted just after it ends. */
export const CHAT_FRAME_FADE_MS = 160;

/** Has this document said its transcript is painted?
 *
 *  Pure, and the one piece of the contract worth testing directly: everything
 *  else in this file is timers and listeners around this single question. A
 *  missing document answers `false` — "not ready yet", not "ready" — because
 *  the CROSS-ORIGIN case (where there is no document to ask, ever) is decided
 *  at the call site on `load`, where it is knowable; here it would only mean
 *  the frame has not navigated yet. */
export function isChatDocReady(doc: Document | null | undefined): boolean {
  try {
    return doc?.documentElement?.dataset?.chatReady === "1";
  } catch {
    // A document that has gone away mid-read. Not ready; the fallback covers it.
    return false;
  }
}

/** The skeleton itself, without the frame — the `resolving` state, where the
 *  template path is not known yet so there is nothing to mount an iframe for.
 *  Same box, same shapes: a card that is still resolving its folder and a card
 *  whose frame is still booting look identical, because to a reader they ARE
 *  the same thing (a chat that is not on screen yet) and were only ever two
 *  states to the code. */
export function ChatFramePlaceholder({ className }: { className?: string }) {
  return (
    <div className={className ? `chat-frame ${className}` : "chat-frame"}>
      <PlaceholderBody />
    </div>
  );
}

/** One user turn and one assistant turn, in the template's own geometry
 *  (`renderLogSkeleton`: a 34%-wide 34px pill hard right, then a 24px avatar
 *  beside three lines at 88/72/50%), in the app's own shimmer (`.skel-bar`,
 *  explorer.css — one animation for every skeleton in the shell).
 *
 *  Two turns and no more: it is a stand-in for "a conversation", not a guess at
 *  how long this one is, and a taller stack of bars reads as a claim about the
 *  content (design.md, out of scope: matching the real turn count). */
function PlaceholderBody({ out = false }: { out?: boolean }) {
  return (
    <div
      className={out ? "chat-frame-placeholder is-out" : "chat-frame-placeholder"}
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

export function ChatFrame({
  src,
  title,
  className,
  frameRef,
  onLoad,
}: {
  /** The /render URL. A CHANGE here is a different conversation, so the wait
   *  starts over — see the `src`-keyed state below. */
  src: string;
  title: string;
  /** The consumer's own iframe class — `.task-card-frame`, `.task-peek-frame`,
   *  `.preview-side-frame`. Kept ON THE IFRAME so the sizing rules those files
   *  already own (the card's 133% + scale(0.75) fit, chiefly) still apply. */
  className?: string;
  /** For a caller that needs the element itself — TaskPeek attaches an Esc
   *  listener to the framed document and hands the ref to the modal chassis as
   *  its initial focus. Filled through the callback ref below rather than
   *  `forwardRef`, because this component needs the node too. */
  frameRef?: React.MutableRefObject<HTMLIFrameElement | null>;
  onLoad?: () => void;
}) {
  const ownRef = useRef<HTMLIFrameElement | null>(null);
  // STATE KEYED BY `src`, not booleans reset in an effect: a new src is a new
  // wait, and comparing against the src that was revealed makes that automatic
  // — no reset render, and no window where a fresh iframe is uncovered because
  // the previous document's answer is still in state.
  const [readyFor, setReadyFor] = useState<string | null>(null);
  const [fadedFor, setFadedFor] = useState<string | null>(null);
  const ready = readyFor === src;
  const showPlaceholder = fadedFor !== src;

  const setFrame = useCallback(
    (el: HTMLIFrameElement | null) => {
      ownRef.current = el;
      if (frameRef) frameRef.current = el;
    },
    [frameRef],
  );

  useEffect(() => {
    const frame = ownRef.current;
    let observer: MutationObserver | null = null;
    let watched: Element | null = null;
    const reveal = () => setReadyFor(src);

    // Read the attribute if it is already there; otherwise watch for it. Called
    // on `load` ONLY — never at mount. Until this src's own document has loaded,
    // `contentDocument` is whatever the element held before: `about:blank` on
    // a fresh mount, and on a src CHANGE the previous conversation, which is
    // already stamped and would uncover the new src over a stale, still
    // navigating document (Bugbot, #1024). A frame that somehow loaded before
    // this effect attached is what the fallback timer is for.
    const inspect = () => {
      const el = ownRef.current;
      if (!el) return;
      let doc: Document | null = null;
      try {
        doc = el.contentDocument;
      } catch {
        // Not ours to read. Nothing to wait for; show it.
        reveal();
        return;
      }
      if (isChatDocReady(doc)) {
        reveal();
        return;
      }
      const root = doc?.documentElement ?? null;
      // `about:blank` before the real navigation has its own documentElement,
      // and the observer on it would never fire — hence re-inspecting on load,
      // and hence comparing roots so a second load does not stack observers.
      if (!root || root === watched) return;
      // A host with no MutationObserver (a non-DOM renderer, a test) cannot
      // watch for the stamp — the fallback timer is what uncovers those.
      if (typeof MutationObserver === "undefined") return;
      observer?.disconnect();
      watched = root;
      observer = new MutationObserver(() => {
        if (isChatDocReady(ownRef.current?.contentDocument ?? null)) reveal();
      });
      observer.observe(root, { attributes: true, attributeFilter: [CHAT_READY_ATTR] });
    };

    const onFrameLoad = () => {
      let doc: Document | null = null;
      try {
        doc = ownRef.current?.contentDocument ?? null;
      } catch {
        doc = null;
      }
      // A loaded frame with no readable document is cross-origin: it will never
      // stamp, and holding a skeleton over a document that is already painted
      // is the worse failure. Reveal.
      if (!doc) {
        reveal();
        return;
      }
      inspect();
    };

    frame?.addEventListener("load", onFrameLoad);
    // The backstop. Cleared on unmount and restarted per src, so a wall of
    // cards carries one timer each and none outlive their frame.
    const fallback = setTimeout(reveal, CHAT_FRAME_FALLBACK_MS);
    return () => {
      frame?.removeEventListener("load", onFrameLoad);
      observer?.disconnect();
      clearTimeout(fallback);
    };
  }, [src]);

  // The placeholder leaves in two beats: opacity to 0 (CSS), then unmounted, so
  // a wall of revealed cards is not a wall of retained skeleton DOM. The extra
  // frame's worth of delay keeps the node alive until the transition it is
  // running has actually finished.
  useEffect(() => {
    if (!ready) return;
    const t = setTimeout(() => setFadedFor(src), CHAT_FRAME_FADE_MS + 20);
    return () => clearTimeout(t);
  }, [ready, src]);

  return (
    <div className="chat-frame">
      <iframe
        ref={setFrame}
        className={
          className
            ? `chat-frame-iframe ${className}${ready ? " is-ready" : ""}`
            : `chat-frame-iframe${ready ? " is-ready" : ""}`
        }
        src={src}
        title={title}
        onLoad={onLoad}
        // NO `sandbox`, like every other /render frame in this app: the template
        // reads its own URL and talks to its host through the runtime, so it
        // would need `allow-same-origin allow-scripts` — a sandbox granting both
        // grants everything, at the cost of being the one frame whose contract
        // differs from the rest.
      />
      {showPlaceholder && <PlaceholderBody out={ready} />}
    </div>
  );
}
