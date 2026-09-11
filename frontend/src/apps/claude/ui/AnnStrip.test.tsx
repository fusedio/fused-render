// The strip's own three rules, as they read from outside: which seats are on
// screen, what each one is CALLED, and what each one DRAWS.
//
// The drawing is the point of most of this file. T:296 hides the Comment seat's
// spare glyph and spare word with no `.on` override anywhere in the sheet, and
// T:7689 says why: "a label that changes width makes the whole right-anchored
// row shuffle on every toggle, and one mode wearing two names reads as two
// features". Since P2-1 moved this strip into the header row beside the ⋮, a
// re-widthing seat also walks the menu's right edge. So the seat's SPOKEN name
// changes across five states and its rendered text does not change at all —
// which is exactly the pair of assertions below.
//
// react-test-renderer, the RecControls.test.tsx pattern: no DOM, so the tests
// read rendered props rather than computed styles.
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

// DYNAMIC, after the shim: `AnnStrip` reaches `pane/paneUrl`, which reaches
// `platform/lib/router`, which reads `location` at module init — and static
// imports are hoisted above `installDomShim()`.
const { AnnStrip } = await import("./AnnStrip");
const { annIdleTitleFor } = await import("../pane/paneUrl");
type AnnMode = import("../ann").AnnMode;

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement): ReturnType<typeof create> {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

type Json = ReactTestRendererJSON;
type Props = Record<string, unknown>;

function nodes(node: Json | Json[] | null, type: string): Props[] {
  const out: Props[] = [];
  const walk = (n: Json | string | null) => {
    if (!n || typeof n === "string") return;
    if (n.type === type) out.push(n.props as Props);
    for (const kid of n.children || []) walk(kid as Json | string);
  };
  for (const n of Array.isArray(node) ? node : [node]) walk(n);
  return out;
}

/** Only the text a reader can actually SEE: `.c-cmt-done` and `.c-done-word`
 *  are in the markup on purpose (T bakes both faces in and lets the stylesheet
 *  pick), so a naive text walk would report the hidden word too. The stylesheet
 *  is not in play under react-test-renderer, so the two classes T hides
 *  unconditionally are skipped here by name. */
const HIDDEN = ["c-cmt-done", "c-done-word"];
function visibleText(node: Json | Json[] | null): string {
  const parts: string[] = [];
  const walk = (n: Json | string | null) => {
    if (!n) return;
    if (typeof n === "string") {
      parts.push(n);
      return;
    }
    const cls = String((n.props as Props | undefined)?.className ?? "");
    if (HIDDEN.some((h) => cls.split(/\s+/).includes(h))) return;
    for (const kid of n.children || []) walk(kid as Json | string);
  };
  for (const n of Array.isArray(node) ? node : [node]) walk(n);
  return parts.join("|");
}

function draw(props: Partial<React.ComponentProps<typeof AnnStrip>> = {}) {
  const r = mount(
    <AnnStrip
      paneNoun="preview"
      shown
      capturing={false}
      onScreenshot={() => {}}
      onComment={() => {}}
      {...props}
    />,
  );
  const json = r.toJSON() as Json | Json[] | null;
  const buttons = nodes(json, "button");
  const seat = (cls: string) =>
    buttons.find((b) => String(b.className ?? "").split(/\s+/).includes(cls));
  return { json, buttons, comment: seat("c-annbtn"), camera: seat("c-viewshot") };
}

// ---- the drawing does not move (T:296, 7689) ------------------------------

const FACES: Array<[string, AnnMode, boolean]> = [
  ["at rest", "off", false],
  ["armed for comments", "comment", true],
  ["while a walkthrough records", "recording", true],
  ["through the settle", "settling", true],
  ["through the transcription", "transcribing", true],
];

for (const [what, mode, armed] of FACES) {
  test(`the Comment seat's visible face is unchanged ${what}`, () => {
    const { json, comment } = draw({ mode, armed });
    expect(comment).toBeDefined();
    // ONE glyph and ONE word, in every state: the bubble and "Comment". The
    // check and "Done" are in the markup and never shown (T:296).
    expect(visibleText(json)).toContain("Comment");
    expect(visibleText(json)).not.toContain("Done");
    // The spare nodes ARE still rendered — this is T's mechanism, a stylesheet
    // picking from baked-in markup, not a JS glyph swap (T:3992).
    const spare = nodes(json, "span").filter((s) =>
      String(s.className ?? "").includes("c-done-word"),
    );
    expect(spare).toHaveLength(1);
  });
}

test("the SPOKEN name changes with the state even though the drawing does not", () => {
  // T:7695 `annBtnName` — the accessible name is where "one mode at a time"
  // and the way out are actually said.
  const names = FACES.map(([, mode, armed]) => draw({ mode, armed }).comment!["aria-label"]);
  expect(new Set(names).size).toBe(FACES.length - 1); // settling and transcribing share one
  expect(names[0]).toBe("Comment on the preview");
  expect(names[1]).toBe("Done — send the notes and finish commenting");
});

test("the idle tooltip is the shared helper, kind-correct for the noun (T:7505)", () => {
  expect(draw({ paneNoun: "preview" }).comment!.title).toBe(annIdleTitleFor("preview"));
  expect(draw({ paneNoun: "app" }).comment!.title).toBe(annIdleTitleFor("app"));
  expect(draw({ paneNoun: "app" }).comment!.title).toContain("Comment on the app,");
});

// ---- which seats are on screen -------------------------------------------

test("the narrow chat view's two absences are independent flags", () => {
  // T:3822 takes Comment, T:3823 takes the camera, and Annotate stays in both.
  expect(draw({ commentShown: false }).comment).toBeUndefined();
  expect(draw({ commentShown: false }).camera).toBeDefined();
  expect(draw({ cameraShown: false }).camera).toBeUndefined();
  expect(draw({ cameraShown: false }).comment).toBeDefined();
  const both = draw({ cameraShown: false, commentShown: false });
  expect(both.comment).toBeUndefined();
  expect(both.camera).toBeUndefined();
  // The walkthrough seat survives either cut.
  expect(both.buttons.some((b) => String(b.className ?? "").includes("c-annrec"))).toBe(true);
});

test("no pane at all: the seats are HIDDEN, not disabled (T:238-241)", () => {
  expect(draw({ shown: false }).json).toBeNull();
  expect(draw({ capable: false }).json).toBeNull();
});

// ---- the Esc'd settle (T:7656-7660) --------------------------------------

test("an Esc'd transcription gives the Comment seat back", () => {
  // `mode` still reads "transcribing" — the recorder is genuinely still
  // settling — but `armed` is false, because Esc took the reader out of the
  // mode (`escape` → `set(false)`). T:7656-7660 writes `annBtn.disabled =
  // false` on every transition precisely so the epoch-guarded re-arm stays
  // reachable: "a seat left disabled until annRecEnd's finally would block
  // exactly the re-arm the epoch guard is written to protect."
  const left = draw({ mode: "transcribing", armed: false }).comment!;
  expect(left.disabled).toBe(false);
  expect(left["aria-disabled"]).toBe("false");
  expect(left["aria-pressed"]).toBe("false");
  expect(left.className).not.toContain("on");
  // And it is the IDLE seat again — the settle's status belongs to the Annotate
  // seat, which is the walkthrough's own.
  expect(left["aria-label"]).toBe("Comment on the preview");

  // Still ARMED through the same phase: inert, and wearing the mode. This is
  // the half Bugbot #1074 fixed — a live seat here ran `done()` and
  // auto-submitted the walkthrough's wordless marks mid-transcription.
  const owned = draw({ mode: "transcribing", armed: true }).comment!;
  expect(owned.disabled).toBe(true);
  expect(owned["aria-disabled"]).toBe("true");
  expect(owned["aria-pressed"]).toBe("true");
});

test("`armed` defaults to `mode !== \"off\"` for a host that hands us only a mode", () => {
  expect(draw({ mode: "comment" }).comment!["aria-pressed"]).toBe("true");
  expect(draw({ mode: "off" }).comment!["aria-pressed"]).toBe("false");
});

test("A PENDING SCHEDULED MESSAGE TAKES ALL THREE SEATS, disabled and not hidden (P4R1-2)", () => {
  // All three end in the composer — the picture lands as a chip above the box,
  // a comment round and a walkthrough both send their notes through it — and
  // the block has that box shut. A live seat here gathers work with nowhere to
  // go (Akshil, 2026-09-10).
  const shut = draw({ blocked: true });
  expect(shut.camera!.disabled).toBe(true);
  expect(shut.camera!["aria-disabled"]).toBe("true");
  expect(shut.comment!.disabled).toBe(true);
  expect(shut.comment!["aria-disabled"]).toBe("true");
  // DISABLED, NOT HIDDEN. "Absent beats dead" is the rule for a seat with no
  // PANE to act on — a permanent fact about the host. A block is a wait, and a
  // row that loses three buttons and grows them back moves under the reader's
  // hand.
  expect(shut.buttons.length).toBe(draw({}).buttons.length);
  // ...and the seats say nothing new: the banner directly below the strip
  // carries the reason, and a second wording of it here would be two answers to
  // one question.
  expect(shut.camera!.title).toBe(draw({}).camera!.title);
  expect(shut.comment!["aria-label"]).toBe(draw({}).comment!["aria-label"]);
  // Nothing blocking leaves every seat exactly as PR3 has it.
  const open = draw({});
  expect(open.camera!.disabled).toBe(false);
  expect(open.comment!.disabled).toBe(false);
});
