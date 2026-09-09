// ONE PICTURE, FULL SIZE, IN THE APP'S OWN MODAL (T:4369-4397 markup,
// T:10681-10805 behaviour, CSS T:997-1140).
//
// The other half of the report that produced the chip: a 22px thumbnail proved a
// screenshot existed and there was "no way to preview it before sending", so
// what the user was asked to do was trust a smudge and press send. Every
// thumbnail the chat draws opens this, pending or sent, through one builder.
//
// THE CHASSIS IS `platform/ui/modal/Modal` (Akshil, 2026-09-09, P2-3) — the same
// one the kebab's Delete task dialog (`platform/ui/EraseTaskModal`) and "what was
// sent" (`ui/SentPop`) use. T's viewer was an overlay of its own with a bar along
// the bottom holding the path, Discard and Close; ported as a shadcn `Dialog`
// with that bar hand-rolled inside it, it was the third dialog design the same
// conversation could put on screen, and its box was sized by its CONTENT — so a
// framed file preview overflowed the card and pushed the bar, Discard included,
// past the bottom of the viewport (P2-5). The chassis brings the overlay, the
// card, the head with the ✕, the footer, the focus trap, the backdrop and Esc —
// and a card that is `max-height: 85vh` with a scrolling body, which is the
// whole of the overflow report. What is T's own and stays is inside the body.
//
// ESCAPE PEELS ONE LAYER, and the chassis owns that now. This viewer opens OVER
// "what was sent", and the port took the key in the CAPTURE phase and stopped it
// so one press could not dismiss both. `modal/esc-stack` is that same rule
// written once for every caller of the chassis — only the topmost registered
// modal reacts — so the phase games go with the overlay they belonged to.
//
// TWO SIZES, and the second is not a nicety: fitted, a 1600px capture lands at
// ~310px in the sidebar — four times the chip, still not enough to read the UI
// it is a picture OF. One click on the image swaps to natural size with the box
// scrolling, which is the only way a narrow column can show a wide screenshot at
// a legible scale (T:1030-1048).
import { useEffect, useRef, useState } from "react";

import { Modal } from "@platform/ui/modal/Modal";

import "../styles/composer.css";
import { AttachIcon } from "./AttachIcon";
import { previewSrcFor, shotAlt, shotNoun, sizeLabel, type Viewable } from "./attachApi";

export interface ShotViewerProps {
  /** The shot on screen, or null. IDENTITY, not a copy: `pending` is what
   *  decides whether Discard can tell the truth (T:10768). */
  shot: Viewable | null;
  paneNoun: string;
  onClose(): void;
  /** Discard, from the one place the user can judge that this is the wrong
   *  picture. It closes too — the thing it was showing is gone (T:10961). */
  onDiscard?(shot: Viewable): void;
}

/** A FILE is the one thing worth opening that has no pixels, and so is an IMAGE
 *  this engine could not decode: a real path, a real size, no pixels, which is a
 *  file in every way that matters here (T:10722-10726). */
function isBare(shot: Viewable): boolean {
  const pic = shot.src || shot.thumb || "";
  return !pic && (shot.kind === "file" || shot.kind === "image");
}

/**
 * THE DIALOG'S HEAD (T:1108 `#shotview-name`). What a file viewer had instead of
 * a picture — the name the user called it and how big it is — is the TITLE now,
 * where the chassis puts every other dialog's identity; a picture says both by
 * BEING shown, so it gets its noun and no size (T:7099 — a size is only ever
 * shown for something with no picture to look at).
 *
 * Exported because the head belongs to the chassis, which a `react-test-renderer`
 * suite cannot mount (it portals), so this is the seam that pins the words.
 */
export function shotViewerTitle(shot: Viewable, paneNoun: string): string {
  const noun = shotNoun(shot, paneNoun);
  if (!isBare(shot)) return noun;
  const size = sizeLabel(shot.size);
  return size ? noun + " · " + size : noun;
}

/**
 * T's `figure#shotview-box` — the box that SCROLLS at natural size, and
 * everything inside it.
 *
 * Its own component, not an inline block, because the chassis PORTALS itself and
 * a `react-test-renderer` suite has no document to portal into — so this is the
 * seam the tests drive, and it is the whole of what the viewer shows.
 */
export function ShotViewerBody({ shot, paneNoun }: Omit<ShotViewerProps, "onClose" | "onDiscard">) {
  const [zoom, setZoom] = useState(false);
  const [frameSrc, setFrameSrc] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  const pic = shot ? shot.src || shot.thumb || "" : "";
  const bare = !!shot && isBare(shot);

  // Every open starts FITTED. A zoom is something you do to one picture while
  // looking at it, not a preference that follows you to the next one — and a
  // viewer that opened already scrolled into the middle of an image would look
  // broken rather than zoomed (T:10739-10744).
  useEffect(() => {
    setZoom(false);
    const box = boxRef.current;
    if (box) {
      box.scrollTop = 0;
      box.scrollLeft = 0;
    }
  }, [shot]);

  // THE FILE'S OWN PREVIEW, and only a file's: an image already has the picture
  // viewer above (a better view of pixels than any template), and a refusal has
  // no path to frame (T:10775-10784).
  useEffect(() => {
    if (!shot || !bare || shot.kind !== "file" || !shot.view) {
      setFrameSrc(null);
      setLoading(false);
      return;
    }
    let live = true;
    setFrameSrc(null);
    // A first render can take seconds (a folder venv, a big parquet), and a
    // blank box for those seconds reads as a preview that failed (T:4384).
    setLoading(true);
    void previewSrcFor(shot.view).then((src) => {
      // The user may have closed this, or opened another attachment, in the
      // seconds the stat took — the same identity test Discard uses.
      if (!live) return;
      if (!src) {
        // No template for this extension (or the copy is gone): the viewer is
        // what it was before D616, and the line promising a preview goes away.
        setLoading(false);
        return;
      }
      setFrameSrc(src);
    });
    return () => {
      live = false;
    };
  }, [shot, bare]);

  if (!shot) return null;

  const alt = shotAlt(shot, paneNoun);

  return (
    <div className="c-shotview-box" data-zoom={zoom ? "" : undefined} ref={boxRef}>
      {pic ? (
        // The click target is the picture itself, which is where the hand
        // already is and what the two cursors have been advertising; a button
        // in the bar would be a second place to look for something the image
        // is already offering (T:10937-10941).
        <img
          className="c-shotview-img"
          src={pic}
          alt={alt + ", full size"}
          onClick={() => {
            const next = !zoom;
            setZoom(next);
            if (!next && boxRef.current) {
              boxRef.current.scrollTop = 0;
              boxRef.current.scrollLeft = 0;
            }
          }}
        />
      ) : null}
      {frameSrc ? (
        <iframe
          className="c-shotview-frame"
          src={frameSrc}
          title=""
          tabIndex={-1}
          aria-hidden="true"
          // Sealed exactly as the shell seals a display-only frame.
          sandbox="allow-scripts allow-same-origin"
          allow=""
          onLoad={() => setLoading(false)}
        />
      ) : null}
      {loading ? <div className="c-shotview-loading">loading preview…</div> : null}
      {/* NO PICTURE AND NO PREVIEW — a .zip, a refusal, an extension with no
          template. The glyph is then the only thing in the body that says what
          this is, and an empty body reads as a viewer that failed. The name and
          the size are in the head above. */}
      {!pic && !frameSrc && !loading ? (
        <div className="c-shotview-blank">
          <AttachIcon kind={shot.kind} className="c-shotview-glyph" />
        </div>
      ) : null}
      {/* Whatever the picture does NOT show, in the one place the user is
          actually looking at the pixels it is about (T:1094-1100). */}
      {shot.viewNote ? <div className="c-shotview-note">{shot.viewNote}</div> : null}
    </div>
  );
}

/**
 * T's `#shotview-bar`, as the chassis' FOOTER. The path is the one fact a picture
 * cannot show about itself, so it rides the row with the buttons and stays put
 * however far the body has scrolled — which is what the sticky bar inside the old
 * box was for.
 */
export function ShotViewerFooter({ shot, onClose, onDiscard }: ShotViewerProps) {
  if (!shot) return null;
  return (
    <>
      {/* Truncated from the LEFT: the filename is the half that identifies it
          (T:1084-1092). It takes the footer grammar's far-left seat, the one
          `.modal-dirty-hint` and `.btn-danger-text` use. */}
      <span className="c-shotview-path c-mono" title={shot.view || ""}>
        {shot.view || ""}
      </span>
      {/* Shown only for a PENDING shot: a sent picture is already in the agent's
          hands, and a Discard that cannot un-send it would be a lie (T:4392). */}
      {shot.pending && onDiscard ? (
        <button
          type="button"
          className="btn btn-danger-text c-shotview-drop"
          onClick={() => {
            onDiscard(shot);
            onClose();
          }}
        >
          Discard
        </button>
      ) : null}
      <button type="button" className="btn btn-secondary" onClick={onClose}>
        Close
      </button>
    </>
  );
}

/** T:10786 — the file's own template comes down on every close, because a
 *  template with a folder venv holds a warm worker and can poll, so a frame
 *  merely hidden goes on running behind a modal the user shut. In React that is
 *  UNMOUNTING the element, which a closed dialog does for free — and the chassis
 *  cannot unmount itself (it defers `onClose` to animate the exit), so it is
 *  rendered conditionally, the shape every other caller of it uses. */
export function ShotViewer(props: ShotViewerProps) {
  if (!props.shot) return null;
  return (
    <Modal
      title={shotViewerTitle(props.shot, props.paneNoun)}
      onClose={props.onClose}
      // `plainBody`: the body hosts a picture, a framed page and a caveat, not a
      // form, and `deploy-body`'s descendant `button`/`p` skins would re-style a
      // surface that arrives already designed (D489).
      plainBody
      dialogClassName="c-shotview-modal"
      footer={<ShotViewerFooter {...props} />}
    >
      {/* `c-overlay` re-anchors the chat's `--c-*` aliases: the dialog is
          portaled to <body>, outside `.chat-root`'s cascade, and the picture and
          its caveat paint on the chat's palette exactly as `SentPop`'s body
          does. */}
      <div className="c-overlay">
        <ShotViewerBody shot={props.shot} paneNoun={props.paneNoun} />
      </div>
    </Modal>
  );
}

export default ShotViewer;
