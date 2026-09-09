// THE RECEIPT a sent turn wears (T:10815 `shotReceipt`, T:11079 `annReceiptRow`,
// T:10903 `shotRestoreReceipt`; CSS T:1716-1801).
//
// ONE builder for the live send and for a restored transcript, which is the
// whole point: a screenshot that is visible while the session lasts and gone the
// moment it is reopened is a screenshot the user cannot rely on having sent
// (T:10809-10814). So this component takes ONE input either way — the turn — and
// the two sources differ in exactly one thing: a live send carries its
// `Receipt[]` (blob thumbnails and all), a restored one rebuilds them from the
// `<pane-shot>` block its `raw` still holds.
import { useEffect, useMemo, useState } from "react";

import { Pin } from "lucide-react";

import "../styles/transcript.css";

import type { UserTurn } from "../protocol/controller-api";
import { parseInbound, type AnnotationWire } from "../protocol/wire";
import { probePruned, receiptsFromWire } from "../shots";
import type { Receipt } from "../shots/types";
import { ShotThumb } from "./AttachTray";
import { AttachIcon } from "./AttachIcon";
import {
  glyphDoor,
  prunedLabel,
  receiptViewable,
  shotAlt,
  SHOT_GONE,
  type Viewable,
} from "./attachApi";

export interface ReceiptsProps {
  turn: UserTurn;
  paneNoun: string;
  /** A thumbnail / glyph door opens the full-size viewer. */
  onOpenShot(shot: Viewable): void;
  /** The HEAD probe, injectable so a render test makes no request (T:10875). */
  probe?: (receipt: Receipt) => Promise<boolean>;
  /**
   * "What was sent". A comment ROW is the door — no separate button, the row
   * itself is the affordance (T:11053-11056) — and when comments ride the
   * message the screenshot thumbs open the same popup instead of the plain
   * viewer, because the picture belongs to the comments there (T:11068-11077).
   * A picture-only send is never wired, so a lone picture keeps the viewer.
   */
  onShowSent?(turn: UserTurn): void;
}

/** T:11079 `annReceiptRow` — one comment row, the same rendering for a live send
 *  and for a turn restored from the transcript, so the two cannot drift. */
function NoteRow({ note, onOpen }: { note: AnnotationWire; onOpen?: () => void }) {
  const meta = note.tag ? "<" + note.tag + ">" : "";
  const clickable = !!onOpen;
  return (
    <div
      className="annsum-row annsum-note"
      {...(clickable
        ? {
            title: "Click to see exactly what was sent to the agent",
            tabIndex: 0,
            role: "button",
            onClick: onOpen,
            onKeyDown: (ev: React.KeyboardEvent) => {
              if (ev.key === "Enter" || ev.key === " ") {
                ev.preventDefault();
                onOpen?.();
              }
            },
          }
        : {})}
    >
      {/* The stored label — the same letter burned into the overview and sent on
          the wire, so the receipt, the picture and the JSON can never drift.
          The pin beside it is a lucide glyph, not 📌 (P2-7). */}
      <span className="annsum-lbl">
        <Pin width="1em" height="1em" strokeWidth={1.5} aria-hidden="true" focusable="false" />
        {note.label || "•"}
      </span>
      <span className="annsum-txt">{note.content || ""}</span>
      {meta ? <span className="annsum-el">{meta}</span> : null}
    </div>
  );
}

function ShotRow({
  receipt,
  paneNoun,
  onOpenShot,
  onShowSent,
  probe,
}: {
  receipt: Receipt;
  paneNoun: string;
  onOpenShot(shot: Viewable): void;
  onShowSent?(): void;
  probe: (receipt: Receipt) => Promise<boolean>;
}) {
  const shot = receiptViewable(receipt);
  const alt = shotAlt(shot, paneNoun);
  // Never for a FILE: an <img> pointed at a .csv is a broken-image glyph, which
  // reads as a bug (T:10819).
  const src = receipt.kind === "file" ? "" : shot.src || "";
  const [pruned, setPruned] = useState(!!receipt.pruned);
  const [gone, setGone] = useState(false);

  // A picture's pruned copy announces itself (the <img> 404s and `onError` says
  // so in words); a row with no <img> has to ASK, or it goes on claiming a path
  // the pruner deleted. Only a RESTORED receipt has the question — a live send's
  // bytes were written seconds ago (T:10869-10879).
  useEffect(() => {
    if (src || receipt.pruned || !receipt.probe) return;
    let live = true;
    void probe(receipt).then((isGone) => {
      if (live && isGone) setPruned(true);
    });
    return () => {
      live = false;
    };
  }, [probe, receipt, src]);

  // T:11068-11077 `sentPopWire` overrides exactly TWO things when comments ride
  // the message: the `.annsum-pane` thumb and the `.annsum-note` rows. The
  // `.annsum-lbl` GLYPH DOOR is not one of them — T:10851 leaves it on
  // `shotViewOpen`, because a file's receipt opens the same viewer (with the same
  // template preview inside it, D616) that its pending chip did, and a door that
  // led somewhere else once the message was sent is the very inconsistency the
  // thumbnail-as-button was added to end.
  const openThumb = onShowSent ?? (() => onOpenShot(shot));

  if (!src) {
    const door = glyphDoor(shot);
    return (
      <div className="annsum-row">
        {door ? (
          <button
            type="button"
            className="annsum-lbl"
            title="Click to see what was attached"
            aria-label={alt + " — open details"}
            onClick={() => onOpenShot(shot)}
          >
            <AttachIcon kind={shot.kind} />
          </button>
        ) : (
          <span className="annsum-lbl">
            <AttachIcon kind={shot.kind} />
          </span>
        )}
        <span className="annsum-txt" title={receipt.viewNote || receipt.view || ""}>
          {receipt.label}
        </span>
        {pruned ? (
          <span className="annsum-gone" title={receipt.view || ""}>
            {prunedLabel(receipt.kind)}
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <>
      <div className="annsum-row">
        <span className="annsum-txt" title={receipt.viewNote || receipt.view || ""}>
          {receipt.label}
        </span>
      </div>
      {/* A restored turn whose file the shots directory has pruned (12h/200
          files) 404s here. Said in words rather than left as a broken-image
          glyph, which a reader takes for a bug in the chat (T:10886-10894). */}
      {gone ? (
        <div className="annsum-gone" title={receipt.view || ""}>
          {SHOT_GONE}
        </div>
      ) : (
        <ShotThumb
          className="annsum-pane"
          shot={shot}
          alt={alt}
          title={onShowSent ? "Click to see exactly what was sent to the agent" : receipt.view || ""}
          onOpen={openThumb}
          onError={() => setGone(true)}
        />
      )}
    </>
  );
}

export function Receipts({
  turn,
  paneNoun,
  onOpenShot,
  onShowSent,
  probe = probePruned,
}: ReceiptsProps) {
  // The turn's own receipts when it is one this page just sent; otherwise the
  // wire block it still carries. `raw` is read ONCE per turn object, and a
  // settled turn's object is carried across every poll (Turn is memoized).
  const { receipts, notes } = useMemo(() => {
    if (turn.attachments && turn.attachments.length) {
      return { receipts: turn.attachments, notes: parseInbound(turn.raw).annotations ?? [] };
    }
    const wire = parseInbound(turn.raw);
    return { receipts: receiptsFromWire(wire.paneShots), notes: wire.annotations ?? [] };
  }, [turn.attachments, turn.raw]);

  if (!receipts.length && !notes.length) return null;
  // A send with no comments never gets wired to the popup (T:11068-11077).
  const sent = notes.length && onShowSent ? () => onShowSent(turn) : undefined;

  return (
    <div className="annsum">
      {/* The overview first — it is the one picture every annotation row refers
          to — then the user's own attachments, then the labelled rows
          (T:16565-16572). */}
      {receipts.map((r, i) => (
        <ShotRow
          key={(r.view || r.label) + ":" + i}
          receipt={r}
          paneNoun={paneNoun}
          onOpenShot={onOpenShot}
          probe={probe}
          {...(sent ? { onShowSent: sent } : {})}
        />
      ))}
      {notes.map((note, i) => (
        <NoteRow key={(note.id || note.label || "n") + ":" + i} note={note} {...(sent ? { onOpen: sent } : {})} />
      ))}
    </div>
  );
}

export default Receipts;
