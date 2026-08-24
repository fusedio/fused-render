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
// **The outcome is SAID, not assumed — but through the global toast (D478),
// not an inline note beside the button.** An inline `<span>` here used to sit
// inside `.am-bench-share`, inside `.am-bench-headtools`, inside the
// right-aligned `.am-section-head` row — so the instant a receipt like "Copied
// as an image" appeared, the whole control group got pushed left (measured:
// the Metric `<select>`'s left edge jumped from x≈1183 to x≈962, ~220px) and
// the button the reader had just pressed moved out from under their pointer.
// It also landed next to the section's own `<h3>`, where a sentence reads as
// a second heading rather than a receipt for one click. A toast has neither
// problem: it renders in `NotificationHost` at the app root, entirely outside
// this row's layout, so nothing beside the button ever moves — and it is
// unambiguously a transient system message, not a second title. `pushToast`
// already owns auto-dismiss (`lib/toast.ts`'s ~6s TTL), so there is no local
// timer to keep in step with it here.
import { useEffect, useRef, useState } from "react";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { pushToast } from "@platform/lib/toast";
import {
  deliverShareCard,
  renderShareCard,
  shareCardFilename,
  shareOutcomeNote,
  type ShareCardInput,
} from "./shareCard";

export function ShareChartButton({ card }: { card: ShareCardInput }) {
  const [busy, setBusy] = useState(false);
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

  const share = async () => {
    setBusy(true);
    try {
      const blob = await renderShareCard(card);
      const outcome = await deliverShareCard(blob, shareCardFilename(card.capability, card.metric));
      if (!alive.current) return;
      // A dismissed share sheet has no receipt to show — the reader is the
      // one who cancelled it, and `shareOutcomeNote` says so by returning "":
      // that empty string must not become an empty toast.
      const msg = shareOutcomeNote(outcome);
      if (msg) pushToast({ msg, tone: "info" });
    } catch (e) {
      if (alive.current) pushToast({ msg: (e as Error).message, tone: "error" });
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  return (
    // Icon-only, per the icon-buttons pass — "Share chart" (and, while busy,
    // "Preparing…") survives as `aria-label`/`title` rather than on screen,
    // and the RECEIPT now travels as a toast rather than living on this row
    // at all (see the file-top comment for why that moved). No wrapping
    // `<span>` any more either: with the inline note gone this button is the
    // whole control, so it sits directly in `.am-bench-headtools`'s flex row
    // like the Metric select beside it, instead of inside a now-empty
    // single-child flex box that existed only to hold the note.
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
  );
}
