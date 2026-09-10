// THE OVERLAY: pins, the hover ring, and the two SHADOW layers that carry them
// into a document this component does not own (T:6294-6520, 6880-6960).
//
// A shadow root, and it buys three guarantees that would each otherwise need
// code (T:6357):
//   * style isolation, BOTH ways. The previewed document is arbitrary — user
//     HTML, any template — and neither its `div { … }` rules nor our `.annpin`
//     may reach each other. No naming scheme is safe against arbitrary CSS; a
//     shadow boundary is.
//   * the capture. `shots/dom-capture` clones the app's `<body>`, and
//     `cloneNode` does NOT clone a shadow tree — so the pins cannot appear in a
//     screenshot for the same structural reason they cannot in the split layout.
//   * the MutationObserver. Mutations inside a shadow root are not reported to a
//     light-DOM observer, so drawing a pin cannot re-trigger the render that
//     drew it.
//
// The host element is APPENDED to `<body>`, never prepended: anchors are
// `tag:nth-of-type` paths, and a new first child would renumber every one of
// them (T:6368).
import { buildBarNode, disposeBarNode, rewireBar, type AnnBarHandlers } from "./AnnBar";
import {
  contentBox,
  elementPinXY,
  intrinsicOf,
  labelFor,
  pinAt,
  pointXY,
  rectOf,
  type StageBox,
} from "./geometry";
import { releaseXOTarget } from "../shots";
import { ANN_LAYER_MARK, ANN_XO_SCROLL, type Annotation } from "./types";

export const ANN_LAYER_CSS: string = [
  ":host { all: initial; }",
  // THE TOKENS ARE DECLARED HERE, and that is what keeps someone else's palette
  // out of ours. `all: initial` does not reset custom properties (the `all`
  // shorthand excludes them by spec), so every `var(--accent)` below was
  // resolving against the APP's own `--accent` — a template with a lime accent
  // gave the hosted Element/Point picker a lime fill while the split layout's
  // bar wore the shell's orange: one control, one state, two colours (QA round
  // 2, item 4). Declared on `:host`, the shadow tree inherits OURS and the
  // app's cannot reach in; `annBarTheme` then overrides them inline on the bar
  // node with the shell's live `--c-*` values, so these are the fallbacks for a
  // token that has not arrived rather than the answer.
  ":host { --bg: #191a1e; --surface: #26282f; --border: #34363e;"
  + " --fg: #ececf1; --dim: #9a9fa9; --accent: #d97757; --on-accent: #1a1a1a;"
  + " --error: #f26d6d; --shadow: rgba(0, 0, 0, .4); }",
  // The reset this file's own `* { box-sizing: border-box }` gives every rule
  // above, restated because a shadow tree inherits no stylesheet: without it the
  // ring's 1.5px border grows the box it is supposed to trace (measured — a
  // 400×60 element drew a 402×62 ring) and the pin stops being 22px round.
  "* { box-sizing: border-box; }",
  ".pins { position: absolute; inset: 0; overflow: hidden; pointer-events: none; }",
  // Literal colours, not tokens: this stylesheet lives in someone else's
  // document, which has never heard of this page's palette. They are the
  // template's --accent / --on-accent, with a light ring and a soft shadow so
  // the pin reads on a dark app and a white page alike.
  ".annpin { position: absolute; transform: translate(-50%, -50%); min-width: 22px;"
  + " height: 22px; padding: 0 4px; border-radius: 999px; background: #d97757;"
  + " color: #1a1a1a; border: 1.5px solid rgba(255, 255, 255, .9);"
  + " font: 600 12px/1 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;"
  + " display: flex; align-items: center; justify-content: center;"
  + " pointer-events: auto; cursor: pointer; user-select: none;"
  + " box-shadow: 0 1px 4px rgba(0, 0, 0, .35); }",
  ".annpin.sent { opacity: .55; cursor: default; }",
  // No `inset` on this one, unlike .pins: annPlaceHl gives it all four of
  // left/top/width/height, and a stray `right: 0` would leave an over-constrained
  // box the moment one of them is missing.
  ".hl { position: absolute; display: none; pointer-events: none;"
  + " border: 1.5px solid #d97757; border-radius: 4px;"
  + " background: rgba(217, 119, 87, .14); }",
  // The annotation bar — see .annbar in the page sheet for the why; same
  // numbers, literal colours for the same reason the pin's are. The picker's
  // rules are the strip's #anntool rules restated: the node is portaled in
  // here and the strip's stylesheet cannot follow it.
  // The bar and the picker in it read the SHELL's tokens, not literals: unlike
  // a pin or the composer card — which float over the app and bring one fixed
  // look — the bar is chrome, a row of the same strip the chat pane shows, and
  // a dark strip over a light shell is the wrong theme (Akshil, 2026-09-05).
  // annBarPaint copies this document's tokens onto the bar node (ANN_BAR_TOKENS)
  // and re-copies when the theme flips; the literals are only the dark
  // fallbacks for a token that has not arrived.
  ".annbar { position: absolute; top: 0; left: 0; right: 0; box-sizing: border-box;"
  + " height: 43px; display: none; align-items: center; gap: 12px; padding: 8px 16px;"
  + " background: var(--bg, #191a1e); border-bottom: 1px solid var(--border, #34363e);"
  + " color: var(--fg, #ececf1); pointer-events: auto;"
  + " font: 400 12px/1.3 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }",
  ".annbar.show { display: flex; animation: annbarin .18s ease; }",
  "@keyframes annbarin { from { opacity: 0; transform: translateY(-6px); } }",
  "@media (prefers-reduced-motion: reduce) { .annbar.show { animation: none; } }",
  ".annbar .lead { flex: 1 1 auto; min-width: 0; display: flex; align-items: center; gap: 10px;"
  + " overflow: hidden; }",
  ".annbar .tag { flex-shrink: 0; height: 26px; color: var(--accent, #d97757); font-weight: 400;"
  + " white-space: nowrap; display: inline-flex; align-items: center; gap: 6px; }",
  ".annbar .tag b { font-weight: inherit; }",
  ".annbar .tag svg { width: 14px; height: 14px; fill: none; stroke: currentColor;"
  + " stroke-width: 1.5; stroke-linecap: round; flex-shrink: 0; }",
  ".annbar .tag .ic-mic { display: none; }",
  ".annbar.rec .tag .ic-mic { display: block; }",
  ".annbar.rec .tag .ic-cmt { display: none; }",
  ".annbar.rec .tag { color: var(--error, #f26d6d); }",
  ".annbar .txt { min-width: 0; color: var(--fg, #ececf1); font-size: 13px; white-space: nowrap;"
  + " overflow: hidden; text-overflow: ellipsis; }",
  ".annbar .slot { display: flex; align-items: center; flex-shrink: 0; }",
  ".annbar .slot:empty { display: none; }",
  ".annbar .done, .annbar.rec .stop { border: 1px solid var(--accent, #d97757); border-radius: 8px;"
  + " background: transparent; color: var(--accent, #d97757);"
  + " font: 400 12px/1 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;"
  + " white-space: nowrap; height: 26px; padding: 0 10px; margin: 0; display: inline-flex;"
  + " align-items: center; gap: 6px; cursor: pointer; flex-shrink: 0; }",
  ".annbar .done:hover { background: color-mix(in srgb, var(--accent, #d97757) 14%, transparent); }",
  ".annbar .done svg { width: 14px; height: 14px; fill: none; stroke: currentColor;"
  + " stroke-width: 1.5; stroke-linecap: round; }",
  ".annbar .discard { border: 1px solid var(--border, #34363e); border-radius: 8px;"
  + " background: transparent; color: var(--dim, #9a9fa9); height: 26px; padding: 0 8px; margin: 0;"
  + " display: inline-flex; align-items: center; cursor: pointer; flex-shrink: 0; }",
  ".annbar .discard:hover { color: var(--error, #f26d6d); border-color: var(--error, #f26d6d); }",
  ".annbar .discard svg { width: 14px; height: 14px; fill: none; stroke: currentColor;"
  + " stroke-width: 1.5; stroke-linecap: round; }",
  ".annbar .tip { position: absolute; top: calc(100% + 6px); display: none; z-index: 4;"
  + " padding: 3px 7px; border-radius: 5px; color: var(--fg, #ececf1); background: var(--surface, #26282f);"
  + " border: 1px solid var(--border, #34363e); box-shadow: 0 2px 8px var(--shadow, rgba(0,0,0,.4));"
  + " font-size: 11px; font-weight: 500; line-height: 1.3; white-space: nowrap; pointer-events: none; }",
  ".annbar .tip.show { display: block; }",
  ".annbar.rec .done { display: none; }",
  ".annbar .stop { display: none; }",
  ".annbar.rec .stop { border-color: var(--error, #f26d6d); color: var(--error, #f26d6d); }",
  ".annbar .stop:hover { background: color-mix(in srgb, var(--error, #f26d6d) 14%, transparent); }",
  ".annbar .stop svg { width: 14px; height: 14px; fill: currentColor; stroke: none; flex-shrink: 0; }",
  ".annbar .stop .clk:empty { display: none; }",
  ".annbar.t1 .txt { display: none; }",
  ".annbar.t2 #anntool .lbl, .annbar.t2 .done .lbl { display: none; }",
  ".annbar.t2 #anntool button, .annbar.t2 .done { padding: 0 8px; }",
  ".annbar.t3 .tag b { display: none; }",
  "#anntool { display: flex; flex-shrink: 0; border: 1px solid var(--border, #34363e);"
  + " border-radius: 8px; overflow: hidden; height: 26px; }",
  "#anntool[hidden] { display: none; }",
  "#anntool button { border: 0; background: transparent; color: var(--dim, #9a9fa9); margin: 0;"
  + " font: 400 12px/1 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;"
  + " padding: 0 10px; height: 100%; display: inline-flex; align-items: center; gap: 5px;"
  + " cursor: pointer; }",
  "#anntool button + button { border-left: 1px solid var(--border, #34363e); }",
  "#anntool button:hover { color: var(--fg, #ececf1); background: var(--surface, #26282f); }",
  '#anntool button[aria-checked="true"] { color: var(--on-accent, #1a1a1a); background: var(--accent, #d97757); }',
  "#anntool svg { width: 14px; height: 14px; fill: none; stroke: currentColor;"
  + " stroke-width: 1.5; stroke-linecap: round; flex-shrink: 0; }",
  "#anntool svg .fill { fill: currentColor; stroke: none; }",
  // The note composer's looks, restated for the document it gets PORTALED into
  // (annPortalPop). The node is the very one the markup declares — its handlers
  // and its draft ride along — but a stylesheet does not: `#annpop` and friends
  // in the page's own <style> cannot reach across a shadow boundary into
  // someone else's document, and without these rules the composer arrives as an
  // unstyled UA text box floating over the app. Same selectors, same numbers as
  // the rules up there (280px, 12px radius, 10px padding), so the two copies can
  // be read side by side.
  //
  // Literal colours for the same reason the pin's are, and — like the pin — ONE
  // fixed set rather than a light/dark pair: the app underneath is arbitrary and
  // its theme is not ours to read. So the card brings its own ground (the
  // template's dark --surface/--bg) plus a light hairline and a deep shadow,
  // which is what separates it from a dark app and a white page alike.
  //
  // `pointer-events: auto` is load-bearing: the layer host is `none` so the app
  // stays clickable through it, and a textarea that inherits that cannot be
  // typed in. `position: absolute` inside a `fixed` host means annPlacePop's
  // coordinates are the framed viewport's — the same space the pins use.
  "#annpop { position: absolute; display: none; width: 280px; padding: 10px;"
  + " background: #26282f; border: 1px solid rgba(255, 255, 255, .22);"
  + " border-radius: 12px; box-shadow: 0 6px 24px rgba(0, 0, 0, .45);"
  + " font: 400 13px/1.45 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;"
  + " color: #ececf1; pointer-events: auto; }",
  // `font: inherit` in the page's copy; spelled out here because `:host { all:
  // initial }` means there is nothing above to inherit but the UA default.
  "#annpop textarea { display: block; width: 100%; background: #191a1e;"
  + " border: 1px solid #34363e; border-radius: 8px; color: #ececf1;"
  + " font: 400 13px/1.45 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;"
  + " padding: 7px 9px; margin: 0; resize: none; outline: none; }",
  "#annpop .hint { color: #8a8f99; font-size: 11px; margin-top: 5px;"
  + " display: flex; align-items: center; }",
  "#annpop #anndel { margin-left: auto; border: 0; background: transparent;"
  + " color: #f26d6d; font: inherit; font-size: 11px; cursor: pointer;"
  + " padding: 0 2px; }",
].join("\n");

/** T:6320 — the XO catcher is the one element the shared sheet does not know:
 *  the full overlay surface that swallows clicks while the mode is armed. Its
 *  cursor is the crosshair ALWAYS — over there every click is a spot. */
const ANN_XO_CATCH_CSS =
  "\n.catch { position: absolute; inset: 0; cursor: crosshair;" + " pointer-events: auto; }";

/** T:6522 — what every layer hands back, so the pins, the composer portal and
 *  the placement need no second code path. */
export interface AnnLayerBindings {
  root: ShadowRoot;
  pins: Element | null;
  hl: HTMLElement | null;
  bar: HTMLElement | null;
  /** The box pin coordinates are measured against — i.e. the thing whose
   *  `clientWidth`/`clientHeight` IS the framed viewport. */
  stage: Element;
}

/** T:6499 `annInjectLayer` — the layer in the TARGET's own document, where the
 *  coordinate space is the one `rectOf` already works in and the pins scroll
 *  with the content for free. */
export function injectLayer(
  doc: Document | null,
  barHandlers: AnnBarHandlers,
): AnnLayerBindings | null {
  if (!doc || !doc.body) return null;
  let host = doc.querySelector("[" + ANN_LAYER_MARK + "]") as HTMLElement | null;
  // No shadow root means someone else's element wearing our attribute, or a
  // half-built one from a torn-down document: start again rather than trust it.
  if (host && !host.shadowRoot) {
    host.remove();
    host = null;
  }
  if (!host) {
    host = doc.createElement("div");
    host.setAttribute(ANN_LAYER_MARK, "layer");
    // INLINE, because a stylesheet of ours in a document of theirs is one
    // `div { position: static }` away from being overruled — and these five
    // declarations are the ones that decide whether the layer is a layer at all.
    // `fixed` makes the layer's box the framed VIEWPORT, which is the coordinate
    // space every pin position is already computed in.
    host.setAttribute(
      "style",
      "position: fixed; inset: 0; display: block;" +
        " margin: 0; padding: 0; border: 0; z-index: 2147483646;" +
        " pointer-events: none;",
    );
    const root = host.attachShadow({ mode: "open" });
    const style = doc.createElement("style");
    style.textContent = ANN_LAYER_CSS;
    const hl = doc.createElement("div");
    hl.className = "hl";
    const pins = doc.createElement("div");
    pins.className = "pins";
    // Ring first, pins second: painted in tree order, so a pin is never hidden
    // behind the ring of the element it marks. The bar last of the three, above
    // the pins and — the portaled composer is appended after it — under the
    // card.
    root.append(style, hl, pins, buildBarNode(doc, barHandlers));
    doc.body.appendChild(host);
  }
  const root = host.shadowRoot;
  if (!root) return null;
  const bar = root.querySelector(".annbar") as HTMLElement | null;
  // A host we FOUND was built by an earlier mount, and the bar inside it still
  // reads that mount's Done / ■ / trash. Re-pointed on every resolve, the same
  // repair `paintBar` makes for the picker — a no-op on the host we just built.
  rewireBar(bar, barHandlers);
  return {
    root,
    pins: root.querySelector(".pins"),
    hl: root.querySelector(".hl") as HTMLElement | null,
    bar,
    stage: doc.documentElement,
  };
}

/** T:8794's removal, with the bar's ResizeObserver taken down first: our layer
 *  is a node in a document we do not own, and nothing over there survives us to
 *  clean it up. Idempotent — no host is the same outcome as one removed. */
export function removeLayer(doc: Document | null): void {
  const host = doc && (doc.querySelector("[" + ANN_LAYER_MARK + "]") as HTMLElement | null);
  if (!host) return;
  const root = host.shadowRoot;
  disposeBarNode(root ? (root.querySelector(".annbar") as HTMLElement | null) : null);
  try {
    host.remove();
  } catch {
    /* the document went first */
  }
}

export interface XOLayerOptions {
  /** The marked frame, in the PARENT's document — its rect is the overlay's.
   *  A GETTER, not a value: the mark moves between the frames the shell keeps
   *  mounted, and the overlay has to follow it without being rebuilt (rebuilding
   *  it would drop the host and flash a new one on every poll). */
  frame: () => HTMLIFrameElement | null;
  /** The parent document, likewise re-read: the host re-renders. Same origin —
   *  it marked the frame for us. */
  parentDoc: () => Document | null;
  bar: AnnBarHandlers;
  /** A click on the catcher, in OVERLAY-relative coordinates (the frame's own
   *  scroll/pan is unreadable, so there is no page coordinate to convert
   *  through — `ANN_XO_SCROLL` is the zero-scroll stand-in). */
  onPoint: (x: number, y: number) => void;
  /** Whether the mode is armed: disarmed, the overlay must not eat a single
   *  event — the reader is USING the framed app. */
  armed: () => boolean;
}

/**
 * T:6294 — A MARKED FRAME WE CANNOT ENTER (D349/D355). No layer injection, no
 * element anchors, no DOM clone. What is NOT walled off is the frame's BOX in
 * the host's own document, and a point note (D344) never needed more than a spot
 * and some words.
 *
 * Stateful, because the host is OUR node in the PARENT's document and has to be
 * found again (and removed) rather than rebuilt per render. The returned
 * `resolve` is called from every sync and every poll, which is what tracks the
 * splitter drag and a window resize with no observer of its own.
 */
export function createXOLayer(opts: XOLayerOptions): {
  resolve(): AnnLayerBindings | null;
  remove(): void;
} {
  let host: HTMLElement | null = null;
  const remove = () => {
    // THE TAB SHARE GOES WITH THE OVERLAY, which is T:6172-6175's first line
    // and carries its reason: "a target that stopped being cross-origin (or
    // went away) has shotPane's own path back, and holding a tab share open
    // past its use is a recording indicator with no purpose."
    //
    // BEFORE the `host` guard, exactly as T orders it: a target that never got
    // as far as building an overlay can still have raised the arm-time prompt
    // (T:7688 fires on the ARM, not on the first note), so `!host` is not the
    // same fact as "there is no share to give back". `releaseXOTarget` carries
    // native's own two guards — another mount's XO target, and a capture in
    // flight (D6) — which T never needed, having no reference count.
    releaseXOTarget();
    if (!host) return;
    // The bar's ResizeObserver FIRST, exactly as `removeLayer` does it: the
    // observer holds the node, not the other way round, so removing the host
    // leaves it running for ever. This was the one layer path that did not.
    const root = host.shadowRoot;
    disposeBarNode(root ? (root.querySelector(".annbar") as HTMLElement | null) : null);
    try {
      host.remove();
    } catch {
      /* the parent was torn down first */
    }
    host = null;
  };
  return {
    remove,
    resolve() {
      const pdoc = opts.parentDoc();
      const frame = opts.frame();
      if (!pdoc || !pdoc.body || !frame) return null;
      // A host the parent re-rendered away, or one without its shadow tree:
      // start again rather than trust it (same posture as `injectLayer`).
      if (host && (!host.isConnected || !host.shadowRoot)) remove();
      if (!host) {
        const el = pdoc.createElement("div");
        el.setAttribute(ANN_LAYER_MARK, "xo-layer");
        el.setAttribute(
          "style",
          "position: fixed; display: block; margin: 0;" +
            " padding: 0; border: 0; z-index: 2147483646; pointer-events: none;",
        );
        const root = el.attachShadow({ mode: "open" });
        const style = pdoc.createElement("style");
        style.textContent = ANN_LAYER_CSS + ANN_XO_CATCH_CSS;
        const catcher = pdoc.createElement("div");
        catcher.className = "catch";
        const hl = pdoc.createElement("div");
        hl.className = "hl";
        const pins = pdoc.createElement("div");
        pins.className = "pins";
        root.append(style, catcher, hl, pins, buildBarNode(pdoc, opts.bar));
        catcher.addEventListener("click", (e) => {
          if (!opts.armed()) return;
          e.preventDefault();
          e.stopPropagation();
          const r = el.getBoundingClientRect();
          const me = e as MouseEvent;
          opts.onPoint(Math.round(me.clientX - r.left), Math.round(me.clientY - r.top));
        });
        pdoc.body.appendChild(el);
        host = el;
      }
      const r = frame.getBoundingClientRect();
      host.style.left = r.left + "px";
      host.style.top = r.top + "px";
      host.style.width = r.width + "px";
      host.style.height = r.height + "px";
      // `display`, not `pointer-events`, so a hidden catcher can never intercept
      // a drag that started while armed (T:6332).
      const root = host.shadowRoot;
      const catcher = root && (root.querySelector(".catch") as HTMLElement | null);
      if (catcher) catcher.style.display = opts.armed() ? "" : "none";
      if (!root) return null;
      return {
        root,
        pins: root.querySelector(".pins"),
        hl: root.querySelector(".hl") as HTMLElement | null,
        bar: root.querySelector(".annbar") as HTMLElement | null,
        stage: host,
      };
    },
  };
}

// ── the ring ────────────────────────────────────────────────────────────────

/** T:8604 `annPlaceHl` — ONE placement path for both writers, the hover follow
 *  and the click's re-anchor. The click MUST place rather than trust the last
 *  hover: on the "move the popover" flow the ring was frozen on the PREVIOUS
 *  element for the whole trip over. */
export function placeHl(hl: HTMLElement | null, el: Element): void {
  if (!hl) return; // hosted: the layer went with an unmarked document
  const r = rectOf(el);
  hl.style.display = "block";
  hl.style.left = r.left + "px";
  hl.style.top = r.top + "px";
  hl.style.width = r.width + "px";
  hl.style.height = r.height + "px";
}

/** Nothing to hide is the same outcome as hidden (T:7690). */
export function hideHl(hl: HTMLElement | null): void {
  if (hl) hl.style.display = "none";
}

// ── the pins ────────────────────────────────────────────────────────────────

export interface PinPaint {
  pins: Element | null;
  /** `annStageEl` — `#leftview` split, the target's `documentElement` hosted,
   *  the overlay host XO. */
  stage: StageBox | null;
  /** Pins follow the MODE: shown on arm, hidden when off (T:6882). */
  armed: boolean;
  list: readonly Annotation[];
  /** T:6580 — pins only for notes created since the current arm. */
  roundStart: number;
  /** The target's document, for resolving element anchors. */
  doc: Document | null;
  xo: boolean;
  resolve(c: Annotation, doc: Document | null): Element | null;
  onPinClick(c: Annotation, left: number, top: number): void;
}

/**
 * T:6880's pin loop. Cleared and redrawn whole rather than diffed: the whole
 * target repaints on every scroll frame anyway, and a diff would be a second
 * model of what is on screen.
 *
 * A SENT note draws no pin (Akshil, 2026-09-04): it used to stay on the app as a
 * muted ✓ until the run finished, which meant the next round of commenting was
 * done over last round's marks. The note itself is kept until `resolveSent` —
 * the receipt and a failed send's un-marking still read it — it just stops being
 * a pin. Likewise a note from an EARLIER round: its chip is the whole of its
 * presence now.
 */
export function paintPins(p: PinPaint): void {
  const { pins } = p;
  if (pins) {
    pins.innerHTML = "";
    (pins as HTMLElement).style.display = p.armed ? "" : "none";
  }
  const host = p.stage;
  if (!pins || !host || !p.armed) return;
  const doc = p.doc;
  p.list.forEach((c, i) => {
    if (c.sent || (c.createdAt || 0) < p.roundStart) return;
    const spot = pinSpotOf(c, doc, p.xo, p.resolve);
    if (!spot) return;
    const at = pinAt(spot.x, spot.y, host);
    // Scrolled out of the framed viewport: no pin this frame — the chip stays.
    if (!at) return;
    // Created in the LAYER's own document: hosted, that is the app's, and a node
    // minted here would be adopted into it on append anyway — doing it by hand
    // keeps the two documents' nodes from being quietly swapped (T:6929).
    const pin = pins.ownerDocument.createElement("div");
    pin.className = "annpin";
    pin.style.left = at.left + "px";
    pin.style.top = at.top + "px";
    pin.textContent = labelFor(i);
    // A walkthrough's mark has no words yet, and `" — click to edit"` on its own
    // is a tooltip that says nothing about the note.
    pin.title = c.content ? c.content + " — click to edit" : "Click to edit";
    pin.onclick = () => p.onPinClick(c, at.left, at.top);
    pins.appendChild(pin);
  });
}

/**
 * Where a note MARKS, in framed-viewport pixels, or null for "not right now".
 * Shared by the pin painter and the chip-edit placement, because they are one
 * question asked from two places.
 *
 * A point's anchor IS the coordinate, converted back through whatever the
 * CURRENT scroll is; the XO overlay's coords are overlay-relative and there is
 * no window to read a scroll off, so the zero-scroll stand-in keeps the one
 * converter honest.
 */
export function pinSpotOf(
  c: Annotation,
  doc: Document | null,
  xo: boolean,
  resolve: (c: Annotation, doc: Document | null) => Element | null,
): { x: number; y: number } | null {
  if (c.kind === "point") {
    return pointXY(c, (doc && doc.defaultView) || (xo ? ANN_XO_SCROLL : null));
  }
  const el = doc ? resolve(c, doc) : null;
  if (!el) return null; // detached / frame not loaded: the chip still shows it
  return elementPinXY(c, contentBox(el), rectOf(el), !!intrinsicOf(el));
}

// ── the paint clock ─────────────────────────────────────────────────────────

/** T:6057 `annQueueRender` — scroll and mutation storms coalesce to ONE
 *  re-render per frame. One queue for every document, because the paint repaints
 *  the whole target either way and a per-document copy would just mean two rAFs
 *  doing one job after a mark move. */
export function createRenderQueue(
  render: () => void,
  raf: (cb: () => void) => void = (cb) => requestAnimationFrame(() => cb()),
): { queue: () => void; dispose: () => void } {
  let queued = false;
  // A GENERATION rather than a dead flag: a frame queued by a scroll or a
  // mutation must not paint into detached nodes after the unmount, and a queue
  // that could never be used again would not survive React re-running the
  // effect that disposes it (StrictMode's mount / unmount / mount).
  let gen = 0;
  return {
    queue() {
      if (queued) return;
      queued = true;
      const mine = gen;
      raf(() => {
        if (mine !== gen) return; // disposed between the queue and the frame
        queued = false;
        render();
      });
    },
    dispose() {
      gen += 1;
      queued = false;
    },
  };
}
