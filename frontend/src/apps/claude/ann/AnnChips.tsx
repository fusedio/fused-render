// THE PENDING NOTES' CHIPS (T:6963-7030), which ride the attachment tray as its
// children (`ui/AttachTray`'s `children` slot).
//
// The SAME pill a screenshot's chip is, because both are things this message is
// about to carry and both come off with the same ✕ — one row reading "what is
// attached" rather than two features that happen to be neighbours (T:927).
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
          <span className="c-pinlbl">{(c.point ? "⌖" : "") + c.label}</span>
          <button
            type="button"
            className="c-txt c-annchip-txt"
            title={c.note.content + " — click to edit"}
            onClick={() => onEdit(c.note)}
          >
            {c.note.content}
          </button>
          <button type="button" aria-label="Remove annotation" onClick={() => onRemove(c.note)}>
            ✕
          </button>
        </div>
      ))}
    </>
  );
}

export default AnnChips;
