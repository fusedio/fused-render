// WHAT AN ATTACHMENT LOOKS LIKE WHEN IT HAS NO PICTURE — one glyph, and it is a
// LUCIDE icon rather than an emoji (Akshil, 2026-09-09, P2-7).
//
// T drew these as stroke SVGs in the button's own ink; the port shipped 📄 and 🖼
// instead, and an emoji is the one glyph an app cannot draw: it arrives at a
// weight and a hue the platform font picked, it is a different picture on macOS,
// Windows and Linux, and beside the shell's own icons it reads as text someone
// pasted in. Every icon in this app comes from `lucide-react` (the shell's
// sidebar, the explorer's rows, the download manager), so these do too —
// `currentColor`, `1em` off the type around them, and the same 1.5 stroke T's
// own strip glyphs use.
//
// ONE MAP, read by the chip, the receipt row and the viewer's head, for the
// reason T keeps one copy of the vocabulary (T:7067): a picture called a file in
// one of the three is a chip that describes the wrong thing.
import { Camera, FileText, Image as ImageIcon, MessageSquare } from "lucide-react";

import {
  MARKER_ANN,
  MARKER_FILE,
  MARKER_IMG,
  MARKER_JOIN,
  MARKER_VIEW,
  markerWord,
} from "../protocol/wire";
import type { ShotKind } from "../shots/types";

/** T:7141's `shotGlyph`, in icons. A screenshot OF the pane is a camera — that
 *  is what took it and what the seat that took it wears; a picture the user
 *  brought in is a picture; anything else is a file. */
const ICONS = {
  pane: Camera,
  overview: Camera,
  image: ImageIcon,
  file: FileText,
} as const satisfies Record<ShotKind, unknown>;

export interface AttachIconProps {
  kind: ShotKind;
  /** Extra class, for the one caller that sizes it off a heading rather than off
   *  body type. */
  className?: string;
}

/**
 * SIZED TO THE TEXT, not to a number: `1em` means the glyph tracks the chip's
 * 12px, the receipt row's 11px and the dialog head's 15px without any of the
 * three restating a size. `aria-hidden` because the words beside it — the noun,
 * the name, the size — already say what it is, and a screen reader announcing
 * "image" before "screenshot of the preview" says it twice.
 */
export function AttachIcon({ kind, className }: AttachIconProps) {
  const Glyph = ICONS[kind] ?? FileText;
  return (
    <Glyph
      className={className}
      width="1em"
      height="1em"
      strokeWidth={1.5}
      aria-hidden="true"
      focusable="false"
    />
  );
}

// ── the marker bubble (T:10525-10545) ───────────────────────────────────────
//
// A send with no typed words still gets a bubble, and what it says is what the
// message CARRIED: "pane screenshot", "images", "files", "annotations", joined
// with " + " (`protocol/wire`'s MARKER_*). T put an emoji in front of each; this
// puts the same lucide icon the chip and the receipt use in front of each, so
// one glyph vocabulary answers for the pill, the receipt row, the dialog head
// and the bubble.
//
// The bubble's TEXT is unchanged by this — the icons are `aria-hidden` svgs and
// the words are the same nodes — which matters because a turn's bubble text is
// what the re-attach probe matches a prior send on.
//
// What IS peeled off here is the marker sigil: the U+2063 that tells a marker
// apart from a reader who typed the word "files" is machinery, and the bubble
// shows the word (`markerWord`, protocol/wire).
const MARKER_KIND: Record<string, ShotKind> = {
  [MARKER_VIEW]: "pane",
  [MARKER_IMG]: "image",
  [MARKER_FILE]: "file",
};

/** One `" + "`-joined marker string, drawn with its icons. Only ever called for
 *  a bubble `isMarkerOnly` has already vouched for, so every part is a known
 *  marker; an unknown one would simply draw its words. */
export function MarkerText({ text }: { text: string }) {
  const parts = text.split(MARKER_JOIN);
  return (
    <>
      {parts.map((part, i) => (
        <span className="c-marker" key={part + ":" + i}>
          {i ? <span className="c-marker-join">{MARKER_JOIN}</span> : null}
          {part === MARKER_ANN ? (
            <MessageSquare
              width="1em"
              height="1em"
              strokeWidth={1.5}
              aria-hidden="true"
              focusable="false"
            />
          ) : (
            <AttachIcon kind={MARKER_KIND[part] ?? "file"} />
          )}
          {markerWord(part)}
        </span>
      ))}
    </>
  );
}

export default AttachIcon;
