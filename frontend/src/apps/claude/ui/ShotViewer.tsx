// ONE PICTURE, FULL SIZE, OVER EVERYTHING (T:4369-4397 markup, T:10681-10805
// behaviour, CSS T:997-1140).
//
// The other half of the report that produced the chip: a 22px thumbnail proved a
// screenshot existed and there was "no way to preview it before sending", so
// what the user was asked to do was trust a smudge and press send. Every
// thumbnail the chat draws opens this, pending or sent, through one builder.
//
// TWO SIZES, and the second is not a nicety: fitted, a 1600px capture lands at
// ~310px in the sidebar — four times the chip, still not enough to read the UI
// it is a picture OF. One click on the image swaps to natural size with the box
// scrolling, which is the only way a narrow column can show a wide screenshot at
// a legible scale (T:1030-1048).
import { useEffect, useRef, useState } from "react";

import { Dialog, DialogContent, DialogTitle } from "@platform/shadcn/ui/dialog";

import "../styles/composer.css";
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

/**
 * T's `figure#shotview-box` — the box that SCROLLS at natural size, and
 * everything inside it.
 *
 * Its own component, not an inline block, for two reasons: the `Dialog` panel is
 * a plain function component here (React 18, no `forwardRef`) so a ref cannot
 * reach its element, and a Base UI dialog PORTALS itself, which a
 * `react-test-renderer` suite has no document to portal into — so this is the
 * seam the tests drive, and it is the whole viewer.
 */
export function ShotViewerBody({ shot, paneNoun, onClose, onDiscard }: ShotViewerProps) {
  const [zoom, setZoom] = useState(false);
  const [frameSrc, setFrameSrc] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  const pic = shot ? shot.src || shot.thumb || "" : "";
  // A FILE is the one thing worth opening that has no pixels, and so is an IMAGE
  // this engine could not decode: a real path, a real size, no pixels, which is
  // a file in every way that matters here (T:10722-10726).
  const bare = !!shot && !pic && (shot.kind === "file" || shot.kind === "image");

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

  // ESCAPE BELONGS TO THE TOP DIALOG, and here that has to be said out loud.
  // This viewer opens OVER "what was sent", which is a `platform/ui/modal/Modal`
  // (R4-1) — and that chassis listens for Escape on `document` in the BUBBLE
  // phase so the key keeps working when focus slips to <body>. Left alone, one
  // press would dismiss the picture AND the record behind it, throwing away the
  // thing the reader was in the middle of checking. So the viewer takes the key
  // first, in the CAPTURE phase, and stops it: whoever is on top closes, and
  // only them. Standalone (no modal underneath) this is the same one press it
  // always was.
  useEffect(() => {
    if (!shot) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [shot, onClose]);

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
  const size = bare ? sizeLabel(shot.size) : "";
  const name = bare ? shotNoun(shot, paneNoun) + (size ? " · " + size : "") : "";

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
      {/* What a FILE viewer has instead of a picture: the name the user called
          it and how big it is. An image says both by BEING shown (T:1108). */}
      {name ? <div className="c-shotview-name">{name}</div> : null}
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
      {/* Whatever the picture does NOT show, in the one place the user is
          actually looking at the pixels it is about (T:1094-1100). */}
      {shot.viewNote ? <div className="c-shotview-note">{shot.viewNote}</div> : null}
      <div className="c-shotview-bar">
        {/* The path the AGENT was given, which is the one fact a picture
            cannot show about itself. Truncated from the LEFT: the filename is
            the half that identifies it (T:1084-1092). */}
        <span className="c-shotview-path c-mono">{shot.view || ""}</span>
        <span className="c-spacer" />
        {/* Shown only for a PENDING shot: a sent picture is already in the
            agent's hands, and a Discard that cannot un-send it would be a
            lie (T:4392). */}
        {shot.pending && onDiscard ? (
          <button
            type="button"
            className="c-pill c-shotview-drop"
            onClick={() => {
              onDiscard(shot);
              onClose();
            }}
          >
            Discard
          </button>
        ) : null}
        <button type="button" className="c-pill" onClick={onClose} autoFocus>
          Close
        </button>
      </div>
    </div>
  );
}

/** T:10786 — the file's own template comes down on every close, because a
 *  template with a folder venv holds a warm worker and can poll, so a frame
 *  merely hidden goes on running behind a modal the user shut. In React that is
 *  UNMOUNTING the element, which a closed dialog does for free. */
export function ShotViewer(props: ShotViewerProps) {
  if (!props.shot) return null;
  const alt = shotAlt(props.shot, props.paneNoun);
  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : props.onClose())}>
      {/* The card's width is the sheet's alone (`.c-overlay.c-shotview`), which
          is why there is no `w-auto max-w-none` in the className: two statements
          of one box's width, one of which the other overrides, is a box that
          disagrees with itself. */}
      <DialogContent
        showCloseButton={false}
        className="c-overlay c-shotview gap-0 border-0 bg-transparent p-0 shadow-none ring-0"
      >
        {/* Named for a screen reader, drawn for nobody: the picture and its path
            are the dialog's whole content. */}
        <DialogTitle className="sr-only">{alt}</DialogTitle>
        <ShotViewerBody {...props} />
      </DialogContent>
    </Dialog>
  );
}

export default ShotViewer;
