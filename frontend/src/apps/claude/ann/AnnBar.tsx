// THE MODE BAR (`#annbar`, T:6198-6301 the node, 6733-6870 the paint).
//
// A ROW ABOVE the frame, not an overlay on it (Akshil, 2026-09-05): arming
// pushes the app down by the bar's 43px instead of hiding its first 43px. Split,
// that is a real flex row before the pane; hosted, the bar stands inside the
// injected layer and the app's own root is pushed down (`barPush`); XO, it
// stands in the overlay in the parent document.
//
// The NODE is built imperatively, in whichever document it has to stand in, and
// that is not a style choice: two of the three bars live in a document React
// does not own, where React's delegated events never fire (a portaled node
// loses its listeners the moment the app iframe unloads). So there is ONE
// builder for all three, and the React component below is a mount point for it
// rather than a second implementation of it.
import { useEffect, useRef } from "react";

import { ANN_BAR, ANN_BAR_TOKENS, ANN_TOKEN_ROOT, type AnnMode } from "./types";
import { barFolds, type BarMetrics } from "./geometry";

export interface AnnBarHandlers {
  /** ✓ Done — commit the open draft, send every pending note, disarm (T:6225). */
  onDone(): void;
  /** ■ — stop the recording, keep the file, transcribe (T:6236). */
  onStop(): void;
  /** The trash — mode-dependent discard (T:6248). */
  onDiscard(): void;
  /** T:6275 — the bar's box changes with the pane (the divider drags, the window
   *  resizes), so the words refit. Guarded by the caller against a bar that is
   *  no longer the current one. */
  onResize(bar: HTMLElement): void;
}

const IC_CMT =
  '<svg class="ic-cmt" viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 2.5h-11a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h2.5v2.9l3.4-2.9h5.1a1 1 0 0 0 1-1v-7a1 1 0 0 0-1-1z"/></svg>';
const IC_MIC =
  '<svg class="ic-mic" viewBox="0 0 16 16" aria-hidden="true"><rect x="6" y="1.5" width="4" height="7.5" rx="2"/><path d="M3.5 7.5a4.5 4.5 0 0 0 9 0"/><line x1="8" y1="12" x2="8" y2="14.5"/></svg>';
const IC_DONE =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 8.5l3.5 3.5 7-8"/></svg>';
const IC_STOP =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1.5"/></svg>';
const IC_TRASH =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4h11"/><path d="M5.5 4V2.75a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V4"/><path d="M3.75 4l.6 9a1 1 0 0 0 1 .95h5.3a1 1 0 0 0 1-.95l.6-9"/><line x1="6.5" y1="7" x2="6.5" y2="10.5"/><line x1="9.5" y1="7" x2="9.5" y2="10.5"/></svg>';

/**
 * WHAT A LIVE BAR NODE OWNS, kept beside the node rather than closed over it.
 *
 * Two facts make this necessary. The bar OUTLIVES the mount that built it (the
 * injected layer's host is found by `querySelector` and reused), so its three
 * handlers have to be re-pointable at the current instance — a bar reused after
 * a remount was calling the dead instance's Done, ■ and trash. And its
 * ResizeObserver is not removed by removing the node, so someone has to be able
 * to disconnect it.
 */
interface BarWiring {
  h: AnnBarHandlers;
  ro: ResizeObserver | null;
}

const BAR_WIRING = new WeakMap<HTMLElement, BarWiring>();

/** Re-point an existing bar's handlers at the CURRENT instance — the same
 *  repair `paintBar` makes for the picker's click listener, for the same
 *  reason. */
export function rewireBar(bar: HTMLElement | null, h: AnnBarHandlers): void {
  const w = bar && BAR_WIRING.get(bar);
  if (w) w.h = h;
}

/** Disconnect the bar's ResizeObserver. Removing the node does not: the
 *  observer holds the node, not the other way round. */
export function disposeBarNode(bar: HTMLElement | null): void {
  const w = bar && BAR_WIRING.get(bar);
  if (!w || !w.ro) return;
  w.ro.disconnect();
  w.ro = null;
}

/** T:6198 `annBarNode` — built for whichever document the layer stands in, so
 *  the paint can write to any of them by class. Empty of words: the paint fills
 *  them from the mode. The PICKER is not built here — it is the strip's own
 *  `#anntool`, adopted into `.slot` by the paint (one node, one state, one set
 *  of handlers). */
export function buildBarNode(doc: Document, h: AnnBarHandlers): HTMLElement {
  // EVERY handler below reads `wiring.h`, never the argument: that indirection
  // is what `rewireBar` repairs.
  const wiring: BarWiring = { h, ro: null };
  const bar = doc.createElement("div");
  bar.className = "annbar";
  bar.setAttribute("role", "toolbar");
  bar.setAttribute("aria-label", "Annotation");

  const lead = doc.createElement("div");
  lead.className = "lead";
  const tag = doc.createElement("span");
  tag.className = "tag";
  // The strip's OWN glyphs, so the tag names the mode with the very icon the
  // user pressed to enter it.
  tag.innerHTML = IC_CMT + IC_MIC + "<b></b>";
  const txt = doc.createElement("span");
  txt.className = "txt";
  lead.append(tag, txt);

  const slot = doc.createElement("div");
  slot.className = "slot";

  const done = doc.createElement("button");
  done.className = "done";
  done.type = "button";
  done.dataset.tip = "Send the notes to Claude and finish · Esc cancels";
  done.setAttribute("aria-label", "Done — send the notes and finish");
  done.innerHTML = IC_DONE + '<span class="lbl">Done</span>';
  done.addEventListener("click", () => wiring.h.onDone());

  // ■ plus the live clock while a walkthrough records — the paint mirrors the
  // strip's clock into `.clk`, one writer for both faces of the same fact.
  const stop = doc.createElement("button");
  stop.className = "stop";
  stop.type = "button";
  stop.dataset.tip = "Stop the recording · Esc also stops it";
  stop.setAttribute("aria-label", "Stop the recording");
  stop.innerHTML = IC_STOP + '<span class="clk"></span>';
  stop.addEventListener("click", () => wiring.h.onStop());

  // Discard (Akshil, 2026-09-06, moved here from the strip): an outline trash
  // LEFT of ✓ Done / ■. The bar is hidden through Stopping…/Transcribing…, so
  // the recording's waiting marks can never be thrown from here.
  const discard = doc.createElement("button");
  discard.className = "discard";
  discard.type = "button";
  discard.dataset.tip = "Discard — nothing is sent";
  discard.setAttribute("aria-label", "Discard the notes and leave the mode");
  discard.innerHTML = IC_TRASH;
  discard.addEventListener("click", () => wiring.h.onDiscard());

  // The instant tooltip: ONE node, shown on hover/focus of any button on the bar
  // (the portaled picker's two included) and placed under that button's right
  // edge. Delegated on the BAR, which is rebuilt with every layer, so it cannot
  // lose its handlers the way a node that outlives a document does.
  const tip = doc.createElement("div");
  tip.className = "tip";
  tip.setAttribute("role", "tooltip");
  const tipFor = (t: EventTarget | null): HTMLElement | null => {
    const el = t as Element | null;
    return el && el.closest ? (el.closest("[data-tip]") as HTMLElement | null) : null;
  };
  const tipShow = (btn: HTMLElement) => {
    const b = btn.getBoundingClientRect();
    const r = bar.getBoundingClientRect();
    tip.textContent = btn.dataset.tip || "";
    tip.style.right = Math.max(0, r.right - b.right) + "px";
    tip.style.left = "auto";
    tip.classList.add("show");
  };
  const tipHide = () => tip.classList.remove("show");
  bar.addEventListener("mouseover", (e) => {
    const b = tipFor(e.target);
    if (b) tipShow(b);
  });
  bar.addEventListener("mouseout", (e) => {
    if (tipFor(e.target) && !tipFor((e as MouseEvent).relatedTarget)) tipHide();
  });
  bar.addEventListener("focusin", (e) => {
    const b = tipFor(e.target);
    if (b) tipShow(b);
  });
  bar.addEventListener("focusout", tipHide);
  bar.addEventListener("click", tipHide);

  bar.append(lead, slot, discard, done, stop, tip);

  // The observer is the BAR'S OWN window's: the node may live in another
  // document (T:6275). KEPT, so it can be disconnected — an observer left
  // running on a removed node is a leak the node's removal does not collect.
  const RO = doc.defaultView && doc.defaultView.ResizeObserver;
  if (RO) {
    wiring.ro = new RO(() => wiring.h.onResize(bar));
    wiring.ro.observe(bar);
  }
  BAR_WIRING.set(bar, wiring);
  return bar;
}

/** T:6787 `annBarTheme` — the SHELL's palette, handed to a bar standing in
 *  another document, as inline custom properties. Read off THIS document's root,
 *  so `data-theme` here is what the bar over there wears. A bar in our own
 *  document needs none of it: the page sheet already has the tokens. */
export function barTheme(bar: HTMLElement, ownDoc: Document): void {
  if (bar.ownerDocument === ownDoc) return;
  const view = ownDoc.defaultView;
  if (!view) return;
  // `.chat-root`, not `<html>`: that is the box the palette is declared on
  // (`ANN_TOKEN_ROOT`), and the theme flip is a `data-theme` on the root ABOVE
  // it — so this one read answers both.
  const src = ownDoc.querySelector(ANN_TOKEN_ROOT) ?? ownDoc.documentElement;
  const cs = view.getComputedStyle(src);
  for (const [read, write] of ANN_BAR_TOKENS) {
    const v = cs.getPropertyValue(read).trim();
    if (v) bar.style.setProperty(write, v);
  }
}

/** T:6801 `annBarClock` — the strip's clock, mirrored onto the stop seat. Same
 *  text, same tick, one writer for both faces. */
export function barClock(bar: HTMLElement, text: string): void {
  const clk = bar.querySelector(".stop .clk");
  if (clk) clk.textContent = text;
}

/** T:6733 `annBarFit` — the measure half of the fold (the arithmetic is
 *  `geometry.barFolds`). Both steps run synchronously inside observer callbacks,
 *  which fire before paint, so the probe never flashes on screen. */
export function barFit(bar: HTMLElement | null): void {
  if (!bar || !bar.classList.contains("show")) return;
  bar.classList.remove("t1", "t2", "t3");
  const view = bar.ownerDocument.defaultView;
  if (!view) return;
  const cs = view.getComputedStyle(bar);
  const gap = parseFloat(cs.columnGap) || 0;
  const box = bar.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const read = (): BarMetrics => ({
    tag: widthOf(bar, ".tag"),
    slot: widthOf(bar, ".slot"),
    discard: widthOf(bar, ".discard"),
    done: widthOf(bar, ".done"),
    stop: widthOf(bar, ".stop"),
  });
  const txt = bar.querySelector(".txt");
  const wide = read();
  const first = barFolds(box, gap, txt ? txt.scrollWidth : 0, wide);
  if (first.t1) bar.classList.add("t1");
  if (!first.t2) return;
  bar.classList.add("t2");
  // Re-measured with the buttons' words GONE: the tag's word is the last to
  // yield, and only if the icon-only buttons still do not fit (T:6753).
  if (barFolds(box, gap, txt ? txt.scrollWidth : 0, wide, read()).t3) bar.classList.add("t3");
}

function widthOf(bar: HTMLElement, sel: string): number {
  const el = bar.querySelector(sel);
  return el ? (el as HTMLElement).offsetWidth : 0;
}

/**
 * T:6805 `annBarPush` — "it covers the top of the page" (Akshil, 2026-09-05) is
 * answered by PUSHING the hosted document down by the bar's height while the bar
 * shows: a margin on its root, `important` so an app's own reset cannot undo it,
 * removed on hide.
 *
 * ONE document at a time, and the handback is not optional: a target that
 * changes under an armed mode gives the margin back before the new one takes it,
 * and a bar that went away entirely still hands it back (Bugbot, PR #1008) —
 * a frame the shell keeps mounted must not keep a 43px gap with no bar on it.
 *
 * Stateful, so the caller holds the returned pusher for the life of the session.
 */
export function createBarPush(): (doc: Document | null) => void {
  let pushed: Document | null = null;
  return (doc) => {
    if (pushed && pushed !== doc) {
      try {
        pushed.documentElement.style.removeProperty("margin-top");
      } catch {
        /* torn down */
      }
    }
    pushed = doc;
    if (doc) doc.documentElement.style.setProperty("margin-top", "43px", "important");
  };
}

export interface BarPaintState {
  mode: AnnMode;
  /** The live clock's text (`m:ss · N`), or "" when nothing is recording. */
  clock: string;
  /** The strip's `#anntool` node, adopted into `.slot` on show. Null for a
   *  cross-origin target: there are no elements to pick (D355). */
  picker: HTMLElement | null;
  /** The picker's ONE named click handler, re-added on every paint — see below. */
  onPickerClick?: (e: Event) => void;
  /** This component's own document, for the theme read. */
  ownDoc: Document;
}

/** T:6820 `annBarPaint`. Derived, never toggled: every path that changes the
 *  answer repaints through here. */
export function paintBar(bar: HTMLElement, s: BarPaintState): void {
  const recording = s.mode === "recording";
  // Shown while a mode is ARMED — the controls it carries are the mode's — and
  // gone through the settle: the mode is still armed then, but the clicks are
  // over and the recording's waiting marks are not a round to throw away.
  const show = s.mode === "comment" || s.mode === "recording";
  const words = ANN_BAR[recording ? "rec" : "comment"];
  bar.classList.toggle("rec", recording);
  barTheme(bar, s.ownDoc);
  barClock(bar, recording ? s.clock : "");
  const b = bar.querySelector(".tag b");
  if (b) b.textContent = words[0];
  // The trash NAMES what it throws — the round's notes, or the walkthrough.
  const discard = bar.querySelector(".discard") as HTMLElement | null;
  if (discard) {
    discard.setAttribute("aria-label", recording ? "Discard the recording" : "Discard the notes");
    discard.dataset.tip = recording
      ? "Discard the recording — nothing is transcribed or sent"
      : "Discard the notes — nothing is sent";
  }
  const txt = bar.querySelector(".txt");
  if (txt) txt.textContent = words[1];
  bar.classList.toggle("show", show);
  if (!show) bar.classList.remove("t1", "t2", "t3"); // a fresh fit on the next show
  if (show && s.picker) {
    const slot = bar.querySelector(".slot");
    if (slot && s.picker.parentNode !== slot) {
      try {
        slot.ownerDocument.adoptNode(s.picker);
        slot.appendChild(s.picker);
      } catch {
        /* document torn down mid-gesture: the next paint retries */
      }
    }
    // RE-WIRED ON EVERY PAINT, not once at boot (Akshil, 2026-09-07 —
    // "Element | Point after repeated use gets stuck"): the picker is one node
    // that rides into the app's document, and when that document unloads the
    // browser strips every listener off the nodes still in it. `adoptNode` keeps
    // listeners; a document TEARDOWN does not. `addEventListener` with the same
    // function is a no-op while the listener is there and a repair the moment it
    // is gone — which only a stable reference makes true.
    if (s.onPickerClick) s.picker.addEventListener("click", s.onPickerClick);
  }
  barFit(bar);
}

// ── the split layout's seat ─────────────────────────────────────────────────

export interface AnnBarProps {
  handlers: AnnBarHandlers;
  /** Everything the paint reads. A new object every render is fine — the paint
   *  is idempotent. */
  paint: Omit<BarPaintState, "ownDoc">;
  /** Handed the live node, so the coordinator can bind it as the current bar
   *  (the pins, the clock and the fit all address it). */
  barRef?: (bar: HTMLElement | null) => void;
}

/**
 * The split layout's bar: a real row in THIS document, mounted before the pane
 * (`pane.css`'s `.c-left > .c-annbar`), holding the one imperative node.
 *
 * React renders the CONTAINER and nothing inside it. The node's own children are
 * written by `paintBar`, which is the same writer the other two layouts use — so
 * there is one bar with three homes rather than three bars.
 */
export function AnnBar({ handlers, paint, barRef }: AnnBarProps) {
  const host = useRef<HTMLDivElement | null>(null);
  const bar = useRef<HTMLElement | null>(null);
  // EVERY CALLBACK THROUGH A REF, and that is what makes the effect below
  // `[]`-dep with nothing missing from it: the node is built ONCE per mount (it
  // outlives every paint, which is what lets its handlers and the adopted picker
  // survive a re-render), so a changed `handlers` object must not rebuild it —
  // it must be what the existing node's handlers already read.
  const live = useRef({ handlers, barRef });
  live.current = { handlers, barRef };

  useEffect(() => {
    const h = host.current;
    if (!h) return;
    const node = buildBarNode(h.ownerDocument, {
      onDone: () => live.current.handlers.onDone(),
      onStop: () => live.current.handlers.onStop(),
      onDiscard: () => live.current.handlers.onDiscard(),
      onResize: (b) => live.current.handlers.onResize(b),
    });
    h.appendChild(node);
    bar.current = node;
    live.current.barRef?.(node);
    return () => {
      live.current.barRef?.(null);
      bar.current = null;
      disposeBarNode(node);
      node.remove();
    };
  }, []);

  useEffect(() => {
    const node = bar.current;
    if (!node) return;
    paintBar(node, { ...paint, ownDoc: node.ownerDocument });
  });

  return <div className="c-annbar-host" ref={host} />;
}

export default AnnBar;
