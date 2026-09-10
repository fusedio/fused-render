// THE CSS-ONLY PARITY ITEMS, read off the stylesheets (P3-02, P3-06, P3-32).
//
// Three of the PR3 items are pure CSS and had no test at all — the whole fix is
// a rule that exists, a rule that does NOT, and one number. `styles/refusal.
// test.ts` proves the pattern is cheap: the suite runs under
// `react-test-renderer` with no CSSOM, so `getComputedStyle` has nothing to
// answer with, but the SHEET can be read and that is where every one of these
// regressions actually happened (the markup was right the whole time).
//
// Same reader as `refusal.test.ts`, over `ann.css` and `chat.css`. The visual
// half is checked in the browser against :1777.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

/** Comments out, so a selector quoted in prose is not mistaken for a rule —
 *  both sheets argue their numbers at length and name plenty of selectors. */
const strip = (raw: string): string => raw.replace(/\/\*[\s\S]*?\*\//g, "");
const ANN = strip(readFileSync(new URL("./ann.css", import.meta.url), "utf8"));
const CHAT = strip(readFileSync(new URL("./chat.css", import.meta.url), "utf8"));
const COMPOSER = strip(readFileSync(new URL("./composer.css", import.meta.url), "utf8"));

/** Every rule whose selector list carries `selector` as a WHOLE selector,
 *  whitespace flattened. Exact rather than substring, for `refusal.test.ts`'s
 *  reason: nearly every selector here is a prefix of a longer one in the same
 *  sheet, so a substring match silently reads a different rule's values. */
function rules(css: string, selector: string): string[] {
  const found: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css))) {
    const parts = m[1]!.split(",").map((x) => x.replace(/\s+/g, " ").trim());
    if (parts.includes(selector)) found.push(m[2]!.replace(/\s+/g, " ").trim());
  }
  return found;
}

function one(css: string, selector: string): string {
  const found = rules(css, selector);
  expect(found.length, "no single rule for " + selector).toBe(1);
  return found[0]!;
}

// ── P3-02 the armed Comment seat keeps ONE face (T:296, T:7689) ─────────────

test("no rule swaps the Comment seat's word or glyph while armed", () => {
  // T's whole rule is `#annbtn .cmt-stop, #annbtn .cmt-done, #annbtn .done-word
  // { display: none }` with no `.on` override anywhere in the sheet, and T:7689
  // says why: "a label that changes width makes the whole right-anchored row
  // shuffle on every toggle, and one mode wearing two names reads as two
  // features". P2-1 put this strip beside the kebab, so the shuffle also walked
  // the ⋮ right edge. Four rules were doing the swap; they are gone, and this
  // is what keeps them gone — the markup already baked both faces in, so a
  // single re-added rule brings the whole regression back.
  for (const sel of [
    ".chat-root .c-anncta .c-annbtn.on .c-cmt-done",
    ".chat-root .c-anncta .c-annbtn.on .c-done-word",
    ".chat-root .c-annbtn.on .c-cmt-done",
    ".chat-root .c-annbtn.on .c-done-word",
  ]) {
    expect(rules(ANN, sel), sel + " is back").toHaveLength(0);
  }
  // Nothing at all keys the seat's LABEL off `.on`: no `.c-annbtn.on` rule may
  // mention `display`, or a spare face could be shown by another road.
  for (const rule of rules(ANN, ".chat-root .c-anncta:has(.c-annrec.on) .c-annbtn.on")) {
    expect(rule).not.toContain("display");
  }

  // The one rule that IS there hides both spare faces unconditionally...
  const spare = one(ANN, ".chat-root .c-anncta .c-annbtn .c-done-word");
  expect(spare).toContain("display: none");
  expect(rules(ANN, ".chat-root .c-anncta .c-annbtn .c-cmt-done")).toHaveLength(1);
  // ...and the accent fill is the mode's one drawn signal (the spoken name
  // still swaps, in `ui/AnnStrip.tsx` — T:7695 `annBtnName`).
  const armed = one(ANN, ".chat-root .c-anncta button.on");
  expect(armed).toContain("var(--c-accent)");
});

// ── P3-06 `← Chats` in the narrow preview (T:3882-3884) ────────────────────

test("Back is hidden in the narrow PREVIEW view, by a rule and not a gate", () => {
  // T:3882-3884's own reason: "the chat list it returns to is off screen in
  // this view, so the button would navigate to something the layout cannot
  // show." A rule rather than a render gate deliberately — `useFitStrip`
  // measures this row and must read one stable node set per layout.
  const back = one(CHAT, ".chat-root.narrow.view-preview .c-back");
  expect(back).toContain("display: none");
  // And ONLY in that view: the narrow chat view is where Back is the way out.
  expect(rules(CHAT, ".chat-root.narrow .c-back")).toHaveLength(0);
  expect(rules(CHAT, ".chat-root .c-back")).toHaveLength(0);
});

// ── P3-32 the picker glides the last 10px (T:353-356) ──────────────────────

test("the picker's slide is T's 10px, in and out", () => {
  // T:353-356: "its seat's WIDTH lands instantly — nothing else may move — and
  // the glyphs glide the last 10px into place." The number had drifted; both
  // keyframes carry it, and the pair is asserted together because an entry and
  // an exit that disagree read as a bounce.
  const frames = (name: string): string => {
    const m = new RegExp("@keyframes\\s+" + name + "\\s*\\{([\\s\\S]*?)\\n\\}").exec(ANN);
    expect(m, "no @keyframes " + name).not.toBeNull();
    return m![1]!.replace(/\s+/g, " ");
  };
  expect(frames("c-annkitin")).toContain("translateX(10px)");
  expect(frames("c-annkitin")).toContain("opacity: 0");
  expect(frames("c-annkitout")).toContain("translateX(10px)");
  // The durations and the fill T argues at the same site. `.chat-root #anntool`
  // carries several rules (the layout, the reduced-motion opt-out), so the
  // animation is looked for across them rather than in one.
  expect(rules(ANN, ".chat-root #anntool").join(" ")).toContain("c-annkitin 0.18s ease");
  expect(rules(ANN, ".chat-root #anntool.out").join(" ")).toContain(
    "c-annkitout 0.15s ease forwards",
  );
  // …and the reduced-motion opt-out is still there (T's own).
  expect(ANN).toContain("prefers-reduced-motion");
});

// ── the kebab's right-hand anchor (T:200, T:526, T:533) ────────────────────

test("the kebab owns the row's auto margin, and gives it back to the CTA group", () => {
  // Bugbot, PR #1074. `.c-anncta { margin-left: auto }` replaced a slack
  // SPACER element (FIX-6B), which was right for the dressed strip and wrong
  // for the mounts where `AnnStrip` returns null altogether — a folder listing
  // with no annotate target still draws `← Chats` and `⋮`, and with no spacer
  // and no CTA group there was no auto margin left in the row at all, so the
  // menu sat against the LEFT edge. T ports the pair, not just the half:
  // `body.nopane #kebab { margin-left: auto }` (T:526) with
  // `#anncta:has(#annbtn:not([hidden])) ~ #kebab { margin-left: 0 }` (T:200,
  // and its `body.nopane` twin at T:533), because two auto margins split the
  // slack and park the pair mid-strip.
  expect(one(COMPOSER, ".c-kebab")).toContain("margin-left: auto");
  expect(one(COMPOSER, ".c-anncta ~ .c-kebab")).toContain("margin-left: 0");
  // The group's own auto margin is the one the revocation defers to, so the two
  // sheets have to keep agreeing about which element holds it.
  expect(rules(CHAT, ".chat-root .c-anncta").join(" ")).toContain("margin-left: auto");
  // And the spacer is a `flex: 1` item, never a second auto-margin owner: a
  // flex ITEM keeps its 12px gap even at zero width, which is the +10.34px
  // FIX-6B measured on the strip it was taken out of.
  expect(one(COMPOSER, ".c-hdr-slack")).not.toContain("margin-left");
});

// ── visual pass 3, the stylesheet half (FIX-19 … FIX-27) ───────────────────

const TRANSCRIPT = strip(readFileSync(new URL("./transcript.css", import.meta.url), "utf8"));

test("FIX-19 — one global rule gives every code and pre T's SF Mono stack", () => {
  // T:116, `code, pre, .mono { font-family: "SF Mono", Menlo, Monaco,
  // "Cascadia Mono", monospace }`, declared once beside the prose stack. This
  // port never had it and no `pre`/`code` rule in transcript.css sets a family,
  // so the UA's generic `monospace` won on every inline path, command echo,
  // card payload and fenced block: three inline `code` and nine `pre` measured.
  const stack = '"SF Mono", Menlo, Monaco, "Cascadia Mono", monospace';
  for (const sel of [".chat-root code", ".chat-root pre", ".chat-root .mono"]) {
    expect(one(CHAT, sel), sel).toContain("font-family: " + stack);
  }
  // …and it reaches the PORTALED surfaces too, whose what-was-sent body is
  // itself one big `pre` (the FIX-17 scope pair).
  for (const sel of [".c-tokens code", ".c-tokens pre", ".c-tokens .mono"]) {
    expect(one(CHAT, sel), sel).toContain("font-family: " + stack);
  }
});

test("FIX-20 — the scroller's gutter is reserved, so the lock cannot reflow the column", () => {
  // `.is-locked` swaps `overflow-y: hidden` in while a tall card is pinned,
  // which DROPS the 8px scrollbar gutter: the log's content box went 372 → 380
  // and every block widened with it (cards 332 → 340, option rows 302 → 310, a
  // right-aligned receipt x 205.8 → 213.8). Legacy's `#logwrap` is
  // `overflow-y: auto` in every state and never moves. `scrollbar-gutter:
  // stable` reserves the space either way — a hidden scroll container is still
  // a scroll container — so R3-4's single-scroller lock stays and costs nothing.
  expect(one(TRANSCRIPT, ".chat-root .chat-logwrap")).toContain("scrollbar-gutter: stable");
  // The lock itself is deliberately still there (owner R3-4: "remove the 70%
  // cap — it causes double scroll").
  expect(one(TRANSCRIPT, ".chat-root .chat-logwrap.is-locked")).toContain("overflow-y: hidden");
  // And T's tail room is T's: `#log` is `padding: 24px 20px 12px` in every
  // state, so the pinned variant may not quietly take 4px of it back.
  expect(one(TRANSCRIPT, ".chat-root .chat-log:has(.chat-tailpin.is-pinned)")).toContain(
    "padding-bottom: 12px",
  );
});

test("FIX-21 — the sticky pin's ground is the column's, and the fade shares its stop", () => {
  // At `--c-bg` this painted rgb(25,26,30) over a rgb(30,32,37) column in dark
  // and a pure-white band across an off-white column in light — the most
  // visible single defect of the pass. The column is `--c-panel` (T:1254-1284's
  // `#chat`, and `.c-chat` since FIX-12b).
  // Two rules carry this selector (the layout, and the reduced-motion opt-out).
  const pin = rules(TRANSCRIPT, ".chat-root .chat-tailpin.is-pinned").join(" ");
  expect(pin).toContain("background: var(--c-panel)");
  expect(pin).not.toContain("background: var(--c-bg)");
  // The `::before` fade's far stop moves with it, or the gradient dissolves
  // into a colour the column never paints.
  const fade = one(TRANSCRIPT, ".chat-root .chat-tailpin.is-pinned::before");
  expect(fade).toContain("var(--c-panel)");
  expect(fade).not.toContain("var(--c-bg)");
});

test("FIX-22 — the card wears T's surface and T's shadow, with no extra ring", () => {
  // T:2033-2046: `background: var(--surface)` — measured rgb(38,40,47) dark and
  // rgb(244,244,246) light — and `box-shadow: 0 4px 16px var(--shadow)`, one
  // layer. Native had the card at #2e313a/#f1f2f5 plus a `0 0 0 1px` outer ring
  // T draws in neither theme. The token is repointed rather than the rule, so
  // the two `inset 0 0 0 3px var(--c-card-bg)` rings that fake a notch out of
  // the card follow it.
  expect(CHAT).toContain("--c-card-bg: #26282f");
  expect(CHAT).toContain("--c-card-bg: #f4f4f6");
  const perm = one(TRANSCRIPT, ".chat-root .perm");
  expect(perm).toContain("background: var(--c-card-bg)");
  expect(perm).toContain("box-shadow: 0 4px 16px var(--c-shadow)");
  expect(perm).not.toContain("0 0 0 1px");
});

test("FIX-23 — the copy pill takes its height from the column, as T's does", () => {
  // T:3149-3163 sets padding, font and colours and NO `line-height`, so the
  // pill inherits the reading line-height and measures 25px. The 1.2 that was
  // here made it 21px — same x, same 46.5 width, and this one line was the
  // whole difference.
  const btn = one(TRANSCRIPT, ".chat-root .copybtn");
  expect(btn).not.toContain("line-height");
  expect(btn).toContain("padding: 3px 9px");
  expect(btn).toContain("font-size: 11px");
});

test("FIX-24 — a plan card's fenced code wraps; only a reply's clips", () => {
  // `.chat-root .perm .plan-body pre` used to ride `.chat-root .assistant pre`,
  // and at 0,3,1 it beat `.chat-root .perm pre` (0,2,1): the plan card got
  // `white-space: pre` at 13px and clipped — scrollWidth 380/990/529 in a 288px
  // box. T has no such selector at all, so T:2092's `.perm pre` governs there.
  expect(rules(TRANSCRIPT, ".chat-root .perm .plan-body pre")).toHaveLength(0);
  const reply = one(TRANSCRIPT, ".chat-root .assistant pre");
  expect(reply).toContain("white-space: pre");
  const card = rules(TRANSCRIPT, ".chat-root .perm pre").join(" ");
  expect(card).toContain("white-space: pre-wrap");
  expect(card).toContain("font-size: 12px");
});

test("FIX-25 — the actual-size viewer has a real scroller on both axes", () => {
  const COMPOSER_CSS = COMPOSER;
  // Legacy's `#shotview-box.zoom` IS the scroller. Here the box stayed
  // `overflow: visible` and so did every ancestor, so a natural-size capture's
  // bottom edge computed 130px past the dialog's and was simply clipped:
  // `scrollHeight === clientHeight` on the box and no scroll position anywhere.
  const box = one(COMPOSER_CSS, ".c-shotview-box[data-zoom]");
  expect(box).toContain("overflow: auto");
  expect(box).toContain("max-height:");
  // The box is a column flex container, so a shrinkable image would be squeezed
  // back to the port and the scroller would have nothing to scroll.
  const img = one(COMPOSER_CSS, ".c-shotview-box[data-zoom] .c-shotview-img");
  expect(img).toContain("flex: 0 0 auto");
  expect(img).toContain("max-height: none");
  // The box is where `ShotViewer` resets the offset on un-zoom, so the scroller
  // has to be this element and not the modal body.
  expect(box).not.toContain("overflow: visible");
});

test("FIX-27 — the receipt keeps T's 4px under its bubble", () => {
  // T sets `margin: 4px 0 0` on `.annsum`; this set none, and because the
  // receipt is the last thing in a user turn that 4px was the whole gap between
  // it and the reply beneath.
  expect(one(TRANSCRIPT, ".chat-root .annsum")).toContain("margin: 4px 0 0");
});

test("FIX-17 — one palette serves the chat AND every portaled surface", () => {
  // `.c-overlay` used to restate a hand-picked SUBSET of the palette in
  // composer.css. A subset in a second place is a palette that drifts: it never
  // got FIX-14's letter-spacing removal and would not have got FIX-22's card
  // surface, and every token it omitted fell through to nothing on a popup that
  // grew a use for it. It now rides the one list.
  expect(CHAT).toContain(".c-overlay {");
  expect(COMPOSER).not.toContain("--c-surface-2: #2d3038");
  // …including the FIX-17 shell bridge, which is what stopped the delete dialog
  // painting the SHELL's danger ink.
  const bridge = rules(CHAT, ".c-overlay").join(" ");
  expect(bridge).toContain("--error: var(--c-error)");
  expect(bridge).toContain("--c-card-bg");
});
