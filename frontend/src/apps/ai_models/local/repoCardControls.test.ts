// ---- what a model card OFFERS while it is busy --------------------------------
// Four claims about the Local tab's card that no pure function holds, and that a
// screenshot cannot hold either because they are about a state that lasts a
// second or two: what the primary button says mid-load, where the cancel lives,
// what the bar does when there is no total to divide by, and which refusals earn
// a visible sentence.
//
// Read out of the source, which is this repo's habit for exactly this kind of
// claim (see shell/tasks-lib.test.ts's "where the marks are drawn"). The states
// below are reachable only while a worker is coming up: on a warm model the load
// finishes inside one runtime poll, so a browser pass sees `Unload` and never the
// window these rules are about. That is precisely why they are pinned here.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const HERE = import.meta.dir;
const CARD = readFileSync(join(HERE, "RepoCard.tsx"), "utf8");
const PROGRESS = readFileSync(join(HERE, "../shared/ModelProgress.tsx"), "utf8");
const CANCEL = readFileSync(join(HERE, "../shared/CancelButton.tsx"), "utf8");
const CSS = readFileSync(join(HERE, "../../../styles/ai-models.css"), "utf8");

/** The body of one CSS rule, by its exact selector line. */
function block(css: string, selector: string): string {
  const at = css.indexOf(selector + " {");
  expect(at).toBeGreaterThan(-1);
  return css.slice(at, css.indexOf("}", at));
}

describe("the card mid-load", () => {
  it("says Loading… and does not offer Unload", () => {
    // It was `loaded ?`, which is any worker at all — including one still
    // starting. Two things were wrong with Unload there: nothing is loaded yet,
    // so the reader is offered the undo of a state the card is not in; and it
    // displaced the control this state actually needs, which is the stop.
    expect(CARD).toContain("{live ? (");
    expect(CARD).toContain(") : loading ? (");
    expect(CARD).toContain("Loading…");
    // `loading` is every not-yet-ready worker state the supervisor reports.
    expect(CARD).toContain(
      'const loading = !!loaded && !live && loaded.state !== "error";',
    );
  });

  it("keeps the stop OUT of the actions strip", () => {
    // Akshil, 2026-08-24: "I don't notice it and I cannot click it because it is
    // fast when I'm trying to load it." Both halves of that are the position. In
    // the strip it was rendered conditionally, so it grew and shrank the row on
    // every job transition — a 26px target that MOVES while being aimed at.
    const actions = CARD.slice(CARD.indexOf('<span className="cc-mdcard-actions">'));
    expect(actions).not.toContain("<CancelButton");
    // …and the component is not deleted: the recommended and search cards still
    // use it, and their actions row has no in-flight label to collide with.
    expect(CANCEL).toContain("export function CancelButton");
  });

  it("puts the stop on the progress row, download first", () => {
    // Two different calls to two different things, which is why the card decides
    // and not the progress row: a download is a job the manager cancels, a load
    // is a worker process, and what stops one of those is `unload`.
    expect(CARD).toContain("const stop = stoppableJob");
    expect(CARD).toContain("label: `Stop downloading ${repo.id}`");
    expect(CARD).toContain("label: `Stop loading ${repo.id}`, onStop: onUnload");
    expect(CARD).toContain("<RuntimeChip loaded={loaded} job={job} stop={stop} />");
    // The download arm keeps CancelButton's own eligibility rule verbatim — a job
    // its reporter never marked cancellable gets no control rather than a dead
    // one, and a cancel already asked for is not asked twice.
    expect(CARD).toContain(
      "job && isRunning(job) && job.cancellable && !job.cancel_requested && !job.stalled",
    );
  });

  it("draws an indeterminate bar rather than inventing a percentage", () => {
    // The track is now unconditional; only the fill differs. A load reports no
    // total, and the old rule drew no bar at all for it — which left a dot and a
    // word for however long the weights took, reading as a card that had stalled,
    // and changed the row's height between the two kinds of work.
    expect(PROGRESS).toContain("am-runtime-bar-indeterminate");
    expect(PROGRESS).toContain("{pct === null ? (");
    // Still no fabricated fraction: the measured fill is the only one with width.
    expect(PROGRESS).toContain('style={{ width: `${pct}%` }}');
    // It travels by transform, so the animation is composited and never a layout.
    const bar = block(CSS, ".am-runtime-bar-indeterminate");
    expect(bar).toContain("animation: am-runtime-slide");
    expect(CSS).toContain("@keyframes am-runtime-slide");
    expect(CSS).toContain("transform: translateX(");
  });
});

describe("why Load is dead, said on the card", () => {
  it("renders the refusal as a visible line, not only a hover", () => {
    // The sentence already existed and was already good — `loadRefusal` writes
    // four of them for four different problems. It lived in the `title` of a
    // DISABLED button, which is the one kind of control a pointer user has no
    // reason to visit, so the page held the answer and showed a greyed word.
    expect(CARD).toContain('<p className="am-card-refusal">');
    expect(CSS).toContain(".am-card-refusal {");
  });

  it("only where a disabled Load is the button it explains", () => {
    // `refusal` is non-null in more states than that: a partly-downloaded repo
    // gets `partialNote` from it, and that card's primary control is an ENABLED
    // "Continue downloading". A line reading "this download did not finish"
    // under a button offering to finish it explains a control that is not there.
    expect(CARD).toContain("{refusal && !live && !loading && !resumable(repo) && (");
  });

  it("links the remedy structurally, never by matching words", () => {
    // The server's reason for an unavailable engine already ends "switch it on
    // the Engines tab" (hub_cache), so the tab it names is one click away. The
    // decision is the engine's own `available` flag: reading the prose for
    // keywords would silently stop working the day it is reworded.
    expect(CARD).toContain("{repo.engine && !repo.engine.available && (");
    expect(CARD).toContain('href={tabHref("engines", "")}');
    // The other refusals — a component, a dataset, a format nothing reads — have
    // no destination that would help, and a link to somewhere unhelpful is worse
    // than none.
    expect(CARD).not.toContain('refusal.includes("Engines")');
  });
});

describe("the engine tag is a pill in every state", () => {
  // Akshil reported this as "diffusers and transformer embeddings don't have a
  // tag". They always had one; it did not look like one. `-off` set the ink and
  // the dash and left `border-color` at the base class's `var(--border)`, and a
  // dashed hairline in the border token at 11px over the card's wash is
  // invisible — while `-family` beside it tints its border AND carries a wash.
  // So the loadable tags read as pills and the unavailable ones read as loose
  // text, which is backwards: this is the state with something to say.
  const tinted = [
    ".am-card-engine-off",
    ".am-card-engine-partial",
    ".am-card-engine-none",
    ".am-card-engine-component",
  ];
  for (const cls of tinted) {
    it(`${cls} tints its border and its ground`, () => {
      const body = block(CSS, cls);
      expect(body).toContain("border-color: color-mix(");
      expect(body).toContain("background: color-mix(");
    });
  }

  it("keeps the DASH as the channel that says which kind", () => {
    // Colour is never the only signal: the dash survives greyscale, a monochrome
    // display and every form of colour blindness.
    expect(block(CSS, ".am-card-engine-off")).toContain("border-style: dashed");
    expect(block(CSS, ".am-card-engine-partial")).toContain("border-style: dashed");
    expect(block(CSS, ".am-card-engine-none")).toContain("border-style: dashed");
    // …and the component tag is SOLID, because nothing is missing there — it was
    // never supposed to load. Same two channels, the other way round.
    expect(block(CSS, ".am-card-engine-component")).not.toContain("border-style: dashed");
  });
});

describe("Try is an outline, not a fill", () => {
  it("wears accent ink on a transparent ground with an accent border", () => {
    // It shipped filled for a day. What that argument left out is the card it
    // sits on: Load is right next to it, and Load is the control that costs
    // memory. A lime plate beside it made the cheap navigation the loudest mark
    // on the card and the consequential button the quiet one.
    const body = block(CSS, ".am-card-try");
    expect(body).toContain("background: transparent");
    expect(body).toContain("color: var(--accent)");
    expect(body).toContain("border: 1px solid var(--accent)");
    expect(body).not.toContain("var(--on-accent)");
  });

  it("drops the forced focus ring with the fill that needed it", () => {
    // `.cc-iconbtn:focus-visible` draws a 2px --accent outline; on an --accent
    // PLATE that was an invisible focus ring and had to be forced to --fg. On a
    // transparent plate it is the highest-contrast ring available.
    expect(CSS).not.toContain("outline-color: var(--fg)");
  });

  it("hovers to a wash and never to a fill", () => {
    expect(CSS).toContain("background: color-mix(in srgb, var(--accent) 12%, transparent)");
  });
});
