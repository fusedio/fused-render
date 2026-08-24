// The comparison chart's Share button: draws the chart, this machine's
// hardware and the fused render mark into one PNG (`shareCard.ts`) and hands it
// to the OS share sheet, the clipboard, or a download — whichever the browser
// actually supports.
//
// It lives beside the chart's own heading rather than over the chart, because
// what it shares is the SECTION's current selection (capability + metric), not
// a hovered row — and it renders only when there is a chart to share, the same
// rule the chart itself follows: a Share button above "no runs recorded yet"
// offers to send an empty axis.
//
// The outcome is SAID, not assumed. The three channels put the card in three
// completely different places (a share sheet, the clipboard, the Downloads
// folder), and a silent success leaves the reader hunting for a file that is
// actually on their clipboard. Cleared on a timer, since the note is a
// receipt for an action already finished — nothing later depends on it having
// been read.
import { useEffect, useRef, useState } from "react";
import { MenuIcons } from "@platform/ui/MenuIcons";
import {
  deliverShareCard,
  renderShareCard,
  shareCardFilename,
  shareOutcomeNote,
  type ShareCardInput,
} from "./shareCard";

const NOTE_MS = 4000;

export function ShareChartButton({ card }: { card: ShareCardInput }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A card takes a beat to encode; a component unmounted in between (a
  // capability switch remounts the section) must not set state afterwards.
  //
  // Set on the way IN as well as cleared on the way out — `useRef(true)`'s
  // initializer only ever runs on the FIRST render, so leaving it out here
  // was a flag only ever cleared: a dev double-mount (which reuses this same
  // instance and its refs — this app does not run under StrictMode today, but
  // `TranscribeStage.tsx`'s `aliveRef` documents the identical hazard for
  // when it does) runs mount -> cleanup -> mount again with no render in
  // between, so `alive.current` latches `false` on that synthetic cleanup and
  // the button never clears `busy` or shows a receipt again for the rest of
  // the session, even though the component is genuinely still mounted.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (note === null) return;
    const timer = window.setTimeout(() => setNote(null), NOTE_MS);
    return () => window.clearTimeout(timer);
  }, [note]);

  const share = async () => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const blob = await renderShareCard(card);
      const outcome = await deliverShareCard(blob, shareCardFilename(card.capability, card.metric));
      if (!alive.current) return;
      // A dismissed share sheet has no receipt to show — the reader is the one
      // who cancelled it.
      const receipt = shareOutcomeNote(outcome);
      if (receipt) setNote(receipt);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  return (
    <span className="am-bench-share">
      {/* Icon-only, per the icon-buttons pass — "Share chart" (and, while
          busy, "Preparing…") survives as `aria-label`/`title` rather than on
          screen, and the RECEIPT below still says in words what happened
          ("Copied as an image" / "Saved as a PNG"), so the one thing this
          button does that a glyph cannot say for itself is still said. */}
      <button
        type="button"
        className="cc-iconbtn"
        disabled={busy}
        title={busy ? "Preparing…" : "Save this chart, with your hardware, as a PNG you can share"}
        aria-label={busy ? "Preparing…" : "Share chart"}
        onClick={() => void share()}
      >
        <span className={busy ? "am-icon-spin" : undefined}>
          {busy ? MenuIcons.spinner : MenuIcons.share}
        </span>
      </button>
      {/* One slot for both, because they are the same slot's two answers —
          `role="status"` so the receipt is announced rather than only seen. */}
      {(note || error) && (
        <span className={error ? "am-bench-share-error" : "am-bench-share-note"} role="status">
          {error ?? note}
        </span>
      )}
    </span>
  );
}
