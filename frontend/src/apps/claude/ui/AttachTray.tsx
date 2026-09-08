// THE CHIP ROW above the composer: what this message is about to carry
// (T:7129 `shotChip`, T:7192 `shotThumbBtn`, markup T:4154/4220, CSS T:896-988).
//
// The chip's job is to be a DOOR. 22 pixels can prove a picture EXISTS and
// nothing that size can show what is in it — so the thumbnail is a `<button>`
// rather than an `<img>`, and one click opens the picture full size where the
// user can actually read it and, if it is the wrong moment, throw it away from
// there (T:7121-7128).
//
// The same pill an annotation's chip is (`.annchip`), because both are things
// this message is about to carry and both come off with the same ✕ — one row
// reading "what is attached" rather than two features that happen to be
// neighbours (T:927-932). `shotchip` is only the entrance-animation hook.
import "../styles/composer.css";

import type { Attachment } from "../shots/types";
import {
  failLabel,
  glyphDoor,
  shotAlt,
  shotGlyph,
  shotNoun,
  shownSize,
  toViewable,
  type Viewable,
} from "./attachApi";

export interface AttachTrayProps {
  /** The tray, in the order the user brought them (T:11625). */
  items: readonly Attachment[];
  /** "preview" for a file target, "app" for a project (`PaneState.paneNoun`). */
  paneNoun: string;
  /** The thumbnail / glyph door: open this at full size. */
  onOpen(shot: Viewable): void;
  /** The ✕ (T:10654 `shotDrop`). */
  onRemove(att: Attachment): void;
  /** PR3's annotation chips share this row; they are handed in as-is. */
  children?: React.ReactNode;
}

/** T:7192 `shotThumbBtn` — ONE builder for every place a screenshot is shown
 *  small, the pending chip and every sent turn's receipt, because they are one
 *  affordance: a picture the user can open while drafting but not after sending
 *  is exactly the inconsistency that makes a control feel arbitrary. */
export function ShotThumb({
  className,
  shot,
  alt,
  title,
  onOpen,
  onError,
}: {
  className: string;
  shot: Viewable;
  alt: string;
  /** The receipt's thumb hovers the PATH; the chip's says what a click does. */
  title?: string;
  onOpen(): void;
  onError?(): void;
}) {
  return (
    <button
      type="button"
      className={className}
      title={title ?? "Click to see this screenshot full size"}
      aria-label={alt + " — open full size"}
      onClick={onOpen}
    >
      <img src={shot.src || shot.thumb || ""} alt={alt} {...(onError ? { onError } : {})} />
    </button>
  );
}

function Chip({
  att,
  paneNoun,
  onOpen,
  onRemove,
}: {
  att: Attachment;
  paneNoun: string;
  onOpen(shot: Viewable): void;
  onRemove(att: Attachment): void;
}) {
  const shot = toViewable(att);
  const noun = shotNoun(shot, paneNoun);
  const alt = shotAlt(shot, paneNoun);
  const pic = !!shot.thumb;
  const door = glyphDoor(shot);
  // Keyed off the PICTURE and not off the kind: an image this browser cannot
  // decode has no thumbnail either, and then its size is the only thing on the
  // chip that tells it apart from another one (T:7172-7177).
  const size = shownSize(shot);
  return (
    <div className={"c-annchip c-shotchip" + (att.pending ? " is-pending" : "")}>
      {pic ? (
        <ShotThumb className="c-shotthumb" shot={shot} alt={alt} onOpen={() => onOpen(shot)} />
      ) : door ? (
        <button
          type="button"
          className="c-pinlbl c-shotdoc"
          title="Click to see what is attached"
          aria-label={alt + " — open details"}
          onClick={() => onOpen(shot)}
        >
          {shotGlyph(shot)}
        </button>
      ) : (
        <span className="c-pinlbl">{shotGlyph(shot)}</span>
      )}
      <span className="c-txt" title={shot.viewNote || shot.view || ""}>
        {/* Says WHICH it is: "here is your picture" and "no picture, and here is
            why" are different facts, and the second is the one a user has to be
            told BEFORE they press send — the whole advantage of attaching on the
            gesture rather than during the send (T:7157-7163). */}
        {att.pending
          ? // The one state T has no chip for: there a chip exists only once its
            // upload has answered. Here the drop of six puts six seats up at
            // once so nothing re-orders itself as the uploads land, and a seat
            // whose bytes are still on their way has to say so. Its own name is
            // deliberately NOT guessed — the pipeline decides whether a file is
            // a picture, and a chip that said "pasted image" and became a file
            // chip would have told the user the wrong thing first.
            "attaching…"
          : shot.view
            ? noun + (size ? " · " + size : "")
            : failLabel(shot)}
      </span>
      <button
        type="button"
        aria-label={"Remove " + noun}
        // Nothing to take back yet, and the pipeline's own late-arrival handling
        // (T:11288 abandoned captures) is what cleans up an in-flight one.
        disabled={att.pending}
        onClick={() => onRemove(att)}
      >
        ✕
      </button>
    </div>
  );
}

export function AttachTray({ items, paneNoun, onOpen, onRemove, children }: AttachTrayProps) {
  if (!items.length && !children) return null;
  return (
    <div className="c-annchips">
      {children}
      {items.map((att) => (
        <Chip key={att.id} att={att} paneNoun={paneNoun} onOpen={onOpen} onRemove={onRemove} />
      ))}
    </div>
  );
}

export default AttachTray;
