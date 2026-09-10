// THE PENDING NOTES' CHIPS (T:6963-7030), which ride the attachment tray as its
// children (`ui/AttachTray`'s `children` slot).
//
// The SAME pill a screenshot's chip is, because both are things this message is
// about to carry and both come off with the same ✕ — one row reading "what is
// attached" rather than two features that happen to be neighbours (T:927).
//
// AND THE SAME PILL MEANS THE SAME MARKUP (P3R1-2). It shared the outer
// `.c-annchip` and nothing else: a bare `<button>` for the ✕, which took the UA
// button chrome and drew a bordered box beside a borderless one, and a
// `.c-annchip-txt` of its own that sat outside `.c-chip-door` — so the badge and
// the words were two flex items rather than one, with no shared inset, and the
// pill's hover lit nothing (owner, 2026-09-10: "misaligned, bordered ✕"). The
// chip now renders the attachment chip's exact three-part shape — `.c-chip-door`
// carrying the glyph slot (`.c-pinlbl`) and the words (`.c-txt`), with
// `.c-chip-x` as its only sibling — so one stylesheet block dresses both kinds
// and the two cannot drift again. The glyph slot is where the attachment chip
// puts its lucide icon; here it holds the pin's own letter, and the crosshair
// before it for a point note.
//
// T has TWO chip rows, one above each composer, "because one node can't be in
// both" (T:7036). React has no such problem: this is one component rendered
// wherever the tray is, and the home and chat composers each get their own
// instance of the tray.
import type { Annotation } from "./types";
import { labelFor } from "./geometry";

export interface AnnChipItem {
  note: Annotation;
  /** The badge letter, off the note's index in the WHOLE list — the same letter
   *  the pin wears and the send stamps into `label` (T:6993). */
  label: string;
  /** A point note's chip leads with the crosshair glyph, so the two kinds read
   *  apart in the pending row without opening either. */
  point: boolean;
}

/** T:6993 — the chips are the PENDING notes, labelled by their position in the
 *  whole list so a letter never changes when a sent note is dropped. */
export function chipsOf(list: readonly Annotation[]): AnnChipItem[] {
  const out: AnnChipItem[] = [];
  list.forEach((note, i) => {
    if (note.sent) return;
    out.push({ note, label: labelFor(i), point: note.kind === "point" });
  });
  return out;
}

export interface AnnChipsProps {
  items: readonly AnnChipItem[];
  /** T:6981 — the popover belongs beside the element the note is about, so the
   *  coordinator resolves that element in the TARGET document and places the
   *  card at its rect: the same coordinates a click on the pin would produce. */
  onEdit(note: Annotation): void;
  /** T:7003 — and if this note's editor is open, close it first, or the popover
   *  lingers with the editor pointing at a deleted id. */
  onRemove(note: Annotation): void;
}

export function AnnChips({ items, onEdit, onRemove }: AnnChipsProps) {
  return (
    <>
      {items.map((c) => (
        <div className="c-annchip" key={c.note.id}>
          {/* THE WHOLE PILL IS THE DOOR, as it is for an attachment: the badge
              and the words are one `<button>` filling the chip, so the part a
              pointer actually lands on is the part that opens the editor. */}
          <button
            type="button"
            className="c-chip-door"
            title={c.note.content + " — click to edit"}
            aria-label={"Note " + c.label + " — click to edit"}
            onClick={() => onEdit(c.note)}
          >
            <span className="c-pinlbl">{(c.point ? "⌖" : "") + c.label}</span>
            <span className="c-txt">{c.note.content}</span>
          </button>
          <button
            type="button"
            className="c-chip-x"
            aria-label={"Remove note " + c.label}
            onClick={() => onRemove(c.note)}
          >
            ✕
          </button>
        </div>
      ))}
    </>
  );
}

export default AnnChips;
