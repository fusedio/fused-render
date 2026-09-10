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
