// "What was sent": the composed message a receipt stands for, on demand
// (T:10965-11045, 4403-4413).
//
// A receipt is a summary; this is the RECORD — the overview screenshot (badges
// and all), each comment with its label, anchor, timing and no-badge caveat, the
// other attached pictures, and the exact composed text. T stashes a live payload
// on the receipt at send time and rebuilds a restored turn's from the wire
// (T:10932); here there is only the second road, because `UserTurn.raw` IS the
// wire and every section below is a `parseInbound` away from it — ONE source, so
// a live popup and a reopened one cannot say different things.
//
// THE DOOR IS THE RECEIPT (R4-1). This box is opened by clicking the receipt
// line under the bubble it belongs to — "app state attached", "screenshot
// attached", "image attached: …" — T's own affordance (T:11059
// `row.title = "Click to see exactly what was sent to the agent"`, T:1249), not
// a second control beside it. `ui/Turn` and `ui/Receipts` own those presses.
//
// AND IT WEARS THE APP'S MODAL CHROME, not a skin of its own: the shared
// `platform/ui/modal/Modal` chassis, the same one `EraseTaskModal` (the Delete
// task dialog this chat's kebab opens) uses — same overlay, same card, same
// head with the ✕, same focus trap and Esc/backdrop close. Before this it was a
// shadcn `Dialog` with a hand-rolled bar and pill, so the two dialogs the same
// menu could put on screen looked like they came from different apps.
import { useMemo } from "react";

import { rawUrl } from "@platform/lib/api";
import { Modal } from "@platform/ui/modal/Modal";

import { annClock, parseInbound, type AnnotationWire, type PaneShotWire } from "../protocol/wire";
import { receiptsFromWire } from "../shots";
import { receiptViewable, shotAlt, shotNoun, type Viewable } from "./attachApi";

export interface SentPopProps {
  open: boolean;
  onClose(): void;
  /** The raw outgoing text, wire blocks and all (`UserTurn.raw`). */
  outgoing: string;
  /** "preview" for a file target, "app" for a project (`PaneState.paneNoun`). */
  paneNoun?: string;
  /** A picture in here opens the full-size viewer OVER this popup — the click
   *  looks dead otherwise. */
  onOpenShot?(shot: Viewable): void;
}

/** The kind/name pair the vocabulary functions read, off a wire entry. */
function asViewable(shot: PaneShotWire): Pick<Viewable, "kind" | "name"> {
  return {
    kind: shot.kind as Viewable["kind"],
    ...(shot.name ? { name: shot.name } : {}),
  };
}

/** T:10975 `addShot` — the picture, then the caveat that rode the wire with it.
 *  A caveat with NO picture is still shown: it is the whole story of an
 *  attachment that failed. */
function SentShot({
  shot,
  paneNoun,
  onOpenShot,
}: {
  shot: PaneShotWire;
  paneNoun: string;
  onOpenShot?(shot: Viewable): void;
}) {
  // Never for a file: an <img> pointed at a .csv is a broken-image glyph.
  const src = shot.view && shot.kind !== "file" ? rawUrl(shot.view) : "";
  const alt = shotAlt(asViewable(shot), paneNoun);
  const open = onOpenShot;
  return (
    <>
      {src ? (
        <img
          className="c-sent-shot"
          src={src}
          alt={alt}
          title="Click to see full size"
          {...(open
            ? {
                onClick: () => {
                  const restored = receiptsFromWire([shot])[0];
                  if (restored) open({ ...receiptViewable(restored), src });
                },
              }
            : {})}
        />
      ) : null}
      {shot.viewNote ? (
        <div className="c-sent-caveat">{(src ? "caveat: " : "") + shot.viewNote}</div>
      ) : null}
    </>
  );
}

/** T:11007-11026 — one comment: the accent label pill, the words (or that there
 *  were none), and the context that makes the words checkable. */
function SentNote({ note }: { note: AnnotationWire }) {
  const bits: string[] = [];
  bits.push(
    note.kind === "point"
      ? "exact spot (" + note.x + ", " + note.y + ")"
      : "<" + (note.tag || "element") + ">",
  );
  if (typeof note.t === "number") bits.push("at " + annClock(note.t));
  if (note.offscreen) bits.push("no badge: " + note.offscreen);
  return (
    <div className="c-sent-note-row">
      <span className="c-sent-note-lbl">{note.label || "•"}</span>
      <span>{note.content || "(no words)"}</span>
      <span className="c-sent-note-meta">{bits.join(" · ")}</span>
    </div>
  );
}

/**
 * The body's sections, in T's order. Its own component because the modal
 * chassis PORTALS itself and a `react-test-renderer` suite has no document to
 * portal into — so this is the seam the tests drive, and it is every section
 * there is.
 *
 * `c-overlay` rides along on the wrapper: the pictures and the comment pills
 * paint with the CHAT's `--c-*` aliases on purpose — a comment's label pill has
 * to be the same colour as the badge burnt into the screenshot above it — and
 * the modal is portaled to <body>, outside .chat-root's cascade, so the aliases
 * have to be re-anchored here. The headings and the wire block stay on the
 * platform palette, like the chrome around them.
 */
export function SentPopBody({
  outgoing,
  paneNoun = "preview",
  onOpenShot,
}: Omit<SentPopProps, "open" | "onClose">) {
  const wire = useMemo(() => parseInbound(outgoing), [outgoing]);
  const shots = wire.paneShots ?? [];
  // The overview is the one picture every comment row refers to, so it leads —
  // and the rest keep the order the wire carried them in (T:10996-11006).
  const overview = shots.find((s) => s.kind === "overview");
  const pics = shots.filter((s) => s.kind !== "overview");
  const notes = wire.annotations ?? [];

  return (
    <div className="c-overlay c-sent-body">
      {/* WHY the reader is looking at more than they typed: the pane's app
          state — and any picture they attached — rides along with the prompt,
          and this is the whole of it. */}
      <p className="deploy-muted">
        Everything the agent received for this turn — your message and whatever
        rode along with it.
      </p>
      {overview ? (
        <>
          <h4 className="c-sent-head">Overview screenshot (badges mark each comment)</h4>
          <SentShot shot={overview} paneNoun={paneNoun} {...(onOpenShot ? { onOpenShot } : {})} />
        </>
      ) : null}
      {pics.map((pic, i) => (
        // `display: contents`, so the heading, the picture and its caveat are
        // three children of the body's flex column exactly as T appends them,
        // rather than one boxed group with its own spacing.
        <div className="c-sent-group" key={(pic.view || pic.name || "p") + ":" + i}>
          <h4 className="c-sent-head">{shotNoun(asViewable(pic), paneNoun)}</h4>
          <SentShot shot={pic} paneNoun={paneNoun} {...(onOpenShot ? { onOpenShot } : {})} />
        </div>
      ))}
      {notes.length ? (
        <>
          <h4 className="c-sent-head">Comments</h4>
          {notes.map((note, i) => (
            <SentNote key={(note.id || note.label || "n") + ":" + i} note={note} />
          ))}
        </>
      ) : null}
      <h4 className="c-sent-head">Exact message the agent received</h4>
      <pre className="c-sent-wire">{outgoing}</pre>
    </div>
  );
}

export function SentPop({ open, onClose, ...body }: SentPopProps) {
  // `Modal` cannot unmount itself (it defers `onClose` to animate the exit), so
  // it is rendered conditionally — the shape every other caller of the chassis
  // uses. The `open` prop stays because the hosts hold the turn, not a boolean.
  if (!open) return null;
  return (
    <Modal
      title="What was sent"
      // T:4405 names the dialog in FULL — `aria-label="What was sent to the
      // agent"` — while the visible bar says the shorter "What was sent"
      // (T:4407). The short form is right on screen, where the receipt the
      // reader just clicked supplies the rest; spoken on its own it drops the
      // half that says WHOSE record this is. The receipt rows that open it were
      // already saying it in full (`ui/Receipts.tsx`).
      ariaLabel="What was sent to the agent"
      // The chat's token scope, on a dialog that portals to
      // `document.body` (FIX-17) — see `styles/chat.css`'s `.c-tokens`.
      dialogClassName="c-tokens"
      onClose={onClose}
      footer={
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Close
        </button>
      }
    >
      <SentPopBody {...body} />
    </Modal>
  );
}
