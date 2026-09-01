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
  it("renders the refusal in the actions strip, beside the button it explains", () => {
    // The sentence already existed and was already good — `loadRefusal` writes
    // four of them for four different problems. It lived in the `title` of a
    // DISABLED button, which is the one kind of control a pointer user has no
    // reason to visit, so the page held the answer and showed a greyed word.
    //
    // It was then a `<p>` BELOW the strip, and at card width that wrapped to
    // three lines — taller than the model's own name, on the cards that can do
    // the least. Now it is inside the strip, which is also the assertion: the
    // opening tag has to fall between the strip's opening tag and the Load
    // button, or it is a paragraph again wherever it sits in the file.
    const strip = CARD.indexOf('<span className="cc-mdcard-actions">');
    const why = CARD.indexOf('<span className="am-card-why"');
    const load = CARD.indexOf("{live ? (");
    expect(strip).toBeGreaterThan(-1);
    expect(why).toBeGreaterThan(strip);
    expect(why).toBeLessThan(load);
    expect(CARD).not.toContain('<p className="am-card-refusal">');
    expect(CSS).toContain(".am-card-why {");
  });

  it("says the VERB where there is something to do, and never truncates it", () => {
    // `Set to MLX FLUX` beside a separate `Engines` link was a statement of
    // configuration followed by a noun (Akshil, 2026-08-25: "i don't know what
    // it means... just say switch engines, two to three words"). One control
    // now, and it is `flex: 0 0 auto` — the thing telling somebody what to do is
    // never the thing that ellipsises.
    expect(CARD).toContain(">\n                Switch engines\n              </a>");
    expect(block(CSS, ".am-card-fix")).toContain("flex: 0 0 auto");
    expect(block(CSS, ".am-card-fix")).toContain("white-space: nowrap");
    // Loud on purpose: it is the only refusal that can be acted on, sitting in a
    // strip of grey text and a greyed button.
    expect(block(CSS, ".am-card-fix")).toContain("color: var(--warning)");
    // …and level with that button, which baseline alignment did not manage
    // between a bordered pill and a bordered control.
    expect(block(CSS, ".am-card-why")).toContain("align-items: center");
    // Read as literal text rather than through `block()`: this selector appears
    // twice, and the first is a grouped `min-width: 0` rule it would find first.
    expect(CSS).toContain(".am-card .cc-mdcard-actions {\n  flex: 0 1 auto;\n  align-items: center;");
  });

  it("hints instantly rather than waiting out a native title", () => {
    // A `title` waits out the browser's own delay, which on a window's first
    // hover is seconds — the whole reason platform/lib/hints exists.
    expect(CARD).toContain("data-hint={refusal}");
    expect(CARD).not.toContain('className="am-card-why" title=');
    // The prose is still the disabled button's accessible name, for a reader
    // who never hovers anything.
    expect(CARD).toContain("`Load ${repo.id} — unavailable: ${refusal}`");
  });

  it("does not repeat the component tag beside the button", () => {
    // "Part of FLUX.2 klein 4B  [Load]" under a tag reading `part of FLUX.2
    // klein 4B`. The tag is the better copy: it carries the hover saying what
    // deleting that component costs.
    expect(CARD).toContain(") : repo.component ? (");
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
    expect(CARD).toContain("repo.engine && !repo.engine.available ? (");
    expect(CARD).toContain('href={tabHref("engines", "")}');
    // The other refusals — a component, a dataset, a format nothing reads — have
    // no destination that would help, and a link to somewhere unhelpful is worse
    // than none.
    expect(CARD).not.toContain('refusal.includes("Engines")');
  });
});

describe("the two tags left on a face are pills", () => {
  // Akshil reported the original as "diffusers and transformer embeddings don't
  // have a tag" (that engine is gone too — the Transformers embedding rows were
  // withdrawn for ONNX Runtime ones, which render through these same classes).
  // They always had one; it did not look like one. `-off` set the
  // ink and the dash and left `border-color` at the base class's
  // `var(--border)`, and a dashed hairline in the border token at 11px over the
  // card's wash is invisible.
  //
  // Three of the five states have since left the face entirely (2026-08-25):
  // the engine's identity is a row in the (i), and the REFUSAL it also carried
  // is the `Switch engines` link. What is left is the pair that explains the
  // button underneath them.
  const tinted = [".am-card-engine-partial", ".am-card-engine-component"];
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
    expect(block(CSS, ".am-card-engine-partial")).toContain("border-style: dashed");
    // …and the component tag is SOLID, because nothing is missing there — it was
    // never supposed to load. Same two channels, the other way round.
    expect(block(CSS, ".am-card-engine-component")).not.toContain("border-style: dashed");
  });

  it("takes the hue table with the tags that used it", () => {
    // `engineHue` mapped each engine FAMILY to one of the calendar's categorical
    // hues so a row could be swept for "all the MLX LM ones". There is no tag
    // left to sweep, and a table nobody reads is a decision nobody is making.
    const ENGINES = readFileSync(join(HERE, "../lib/engines.ts"), "utf8");
    // On the CODE, not the prose: both names survive in the headstone recording
    // why they went, and a pin that fired on a comment is a pin nobody can write
    // the history in front of.
    expect(ENGINES).not.toContain("export function engineHue");
    expect(ENGINES).not.toContain("const ENGINE_HUES");
    for (const dead of [".am-card-engine-family", ".am-card-engine-off", ".am-card-engine-none"]) {
      expect(CSS).not.toContain(dead + " {");
    }
  });
});

describe("Try is an outline, not a fill", () => {
  it("wears accent ink on a transparent ground with an accent border", () => {
    // It shipped filled for a day. What that argument left out is the card it
    // sits on: Load is right next to it, and Load is the control that costs
    // memory. A lime plate beside it made the cheap navigation the loudest mark
    // on the card and the consequential button the quiet one.
    // The border then softened again: a full-accent stroke repeated on every
    // card out-shouted the one filled state (Loaded) that is supposed to be
    // the loudest mark on the page, so it sits a third of the way between the
    // plain border and the accent. The INK stays full accent — that is the
    // "this goes somewhere else" signal.
    const body = block(CSS, ".am-card-try");
    expect(body).toContain("background: transparent");
    expect(body).toContain("color: var(--accent)");
    expect(body).toContain("border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border))");
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

// ---- the card's face, after the (i) (2026-08-24) -----------------------------
// The redesign's whole claim is about what a reader gets by SWEEPING a grid
// versus what they get having stopped at one card. None of that is a pure
// function, and a screenshot cannot hold it either — so the split is pinned in
// the source, the same way the mid-load states above are.
describe("what the card's face keeps, and what the (i) takes", () => {
  const INFO = readFileSync(join(HERE, "ModelInfo.tsx"), "utf8");

  it("shows the model half of the id, with the whole id in the subtitle", () => {
    // Six cards reading `mlx-community/…` spend their first third saying nothing
    // that tells them apart, and the name itself then ellipsises. The owner is
    // not lost — it leads the line directly below.
    // The head prefers the CURATED label when the catalog names the repo
    // (AI-2c: display names are curated, never derived at runtime), and the
    // mechanical strip survives only as the fallback for uncatalogued repos.
    expect(CARD).toContain("{label ?? modelName(repo.id)}");
    // The hint rides the ID, not the whole line: the line spans the card, and a
    // hint on it fired over the empty space between id and size.
    expect(CARD).toContain('<div className="am-card-sub">');
    expect(CARD).toContain('<span className="am-card-slug cc-mono" data-hint={repo.id}>{repo.id}</span>');
    // The FIGURE ends that line, pinned to the card's right edge, and is NOT
    // in the head (Akshil, 2026-08-25: "the size should not be after the model
    // name checkmark, it should be right below it"; 2026-08-27: "move the size
    // all the way to the right in the card"). The id leads, continuing the
    // name above it; `margin-left: auto` does the pinning.
    const head = CARD.slice(CARD.indexOf('<div className="cc-mdcard-head">'));
    expect(head.slice(0, head.indexOf('<div className="am-card-sub"'))).not.toContain(
      "{formatSize(repo.size)}",
    );
    const sub = CARD.slice(CARD.indexOf('<div className="am-card-sub"'));
    expect(sub.indexOf("am-card-slug")).toBeLessThan(sub.indexOf("{formatSize(repo.size)}"));
    expect(CSS).toMatch(/\.am-card-size\s*{[^}]*margin-left:\s*auto/);
    // And no separator between them — the middot was invisible at 11px on a
    // card wash.
    expect(CSS).not.toContain(".am-card-sub .am-card-size::after");
    // A bare id (`gpt2`, the Hub's legacy canonical models) is already the name.
    expect(CARD).toContain('const cut = id.lastIndexOf("/");');
    expect(CARD).toContain("return cut === -1 ? id : id.slice(cut + 1);");
    // …and the href is still the WHOLE id, whatever is drawn.
    expect(CARD).toContain("href={hubUrl(repo)}");
  });

  it("moves identity into the panel and leaves state on the face", () => {
    // Engine, parameters, quantization and format are read by somebody deciding
    // about ONE model; none is read by sweeping. The task label went entirely —
    // it repeated the section heading the card is filed under.
    for (const gone of ["am-card-task", "am-card-params", "am-card-quant", "am-card-library"]) {
      expect(CARD).not.toContain(gone);
    }
    for (const row of ["Engine", "Parameters", "Quantization", "Format"]) {
      expect(INFO).toContain(`label: "${row}"`);
    }
    // The two tags that survive are the two that explain the BUTTON below them.
    expect(CARD).toContain("{(repo.component || resumable(repo)) && (");
    expect(CARD).toContain("am-card-engine-component");
    expect(CARD).toContain("am-card-engine-partial");
  });

  it("cannot change the card's box when it opens", () => {
    // These cards sit in a horizontal carousel: anything opening INSIDE a card
    // would change that card's height and shove every card to the right of it
    // mid-scroll. "It shouldn't change card sizes or layouts, it is a popover."
    expect(block(CSS, ".am-info-panel")).toContain("position: fixed");
    expect(INFO).toContain("getBoundingClientRect()");
    // Every way the anchor can go stale is a close.
    expect(INFO).toContain('window.addEventListener("scroll", onClose, true)');
    expect(INFO).toContain('e.key === "Escape"');
    expect(INFO).toContain("if (!panel.current?.contains(e.target as Node)) onClose();");
  });

  it("keeps the panel one grey, and names the local door", () => {
    // The chips it replaced were five colours because each was competing to be
    // spotted in a grid. Nothing in the panel is being swept past.
    expect(INFO).not.toContain("engineHueStyle(");
    expect(CARD).not.toContain("engineHueStyle(");
    expect(INFO).not.toContain("am-card-engine");
    // Explore was an unlabelled glyph third in a row of four; it is a named
    // control now, at the same destination.
    expect(INFO).toContain('className="am-info-more"');
    expect(INFO).toContain("Know more");
    expect(INFO).toContain('urlForFsPath(repo.path, "?_mode=model_card")');
    expect(CARD).not.toContain('className="cc-iconbtn am-card-explore"');
  });

  it("has no chevron, no drawer and no revisions anywhere", () => {
    // A repo holding two commits is simply a bigger number, and the Delete
    // beside it is the same remedy it always was.
    //
    // Asserted on CODE, not on prose: every one of these words survives in the
    // headstone comments that record why they went, and a pin that fired on a
    // comment would be a pin nobody could write the history in front of.
    for (const gone of ["{expanded", "onToggle=", "<Revisions", "onDeleteRevision="]) {
      expect(CARD).not.toContain(gone);
    }
    expect(CARD).not.toContain('className="am-drawer-facts"');
    expect(CSS).not.toContain(".am-rev {");
    expect(CSS).not.toContain(".am-drawer {");
  });

  it("marks the curation's picks, and takes the answer from the page", () => {
    // A repo row has no opinion about its own membership in the shortlist; the
    // catalog does, and the page is what holds the catalog.
    expect(CARD).toContain("{curated && <CuratedMark />}");
    expect(CARD).toContain('aria-label="Curated by Fused"');
    // The badge marks membership of the curated shortlist, a WIDER set than the
    // catalog's `recommended` axis (one row per capability and engine, read only
    // by the Playground's sidebar) — the two strings a reader or a screen reader
    // actually receives must not claim that narrower axis. Scoped to those two
    // lines rather than the whole file, which is free to discuss the difference
    // in prose and does.
    for (const copy of [/data-hint="[^"]*"/, /aria-label="Curated[^"]*"/]) {
      const line = CARD.match(copy);
      expect(line).not.toBeNull();
      expect(line![0]).not.toContain("Recommended");
    }
    const LOCAL = readFileSync(join(HERE, "LocalTab.tsx"), "utf8");
    expect(LOCAL).toContain("curated={curated.has(r.id)}");
    // And takes it from the shared helper rather than building its own set: a
    // card's `r.id` is a REPO id, and the set has to be keyed to match or a
    // filename-keyed llama.cpp entry never marks its own disk card
    // (`curatedRepoIds`, aiModelGroups.ts).
    expect(LOCAL).toContain("const curated = curatedRepoIds(catalog);");
  });

  it("leads every Download with the same glyph", () => {
    // Three buttons draw it — a recommendation, a Hub result, and a partly
    // downloaded repo's "Continue downloading" — and the point of the glyph is
    // that those read as one act, so there is one copy of the path.
    expect(PROGRESS).toContain("export function DownloadGlyph()");
    const REC = readFileSync(join(HERE, "RecommendedCard.tsx"), "utf8");
    expect(REC.match(/<DownloadGlyph \/>/g)?.length).toBe(2);
    expect(CARD).toContain("<DownloadGlyph />");
  });

  it("declares the have/not-have surfaces per palette", () => {
    // A `color-mix` states a DIRECTION, and the two themes need different
    // distances along it: 14% of light mode's warm --fg-muted over --bg-alt is a
    // muddy plate, so a downloaded model read as a disabled card.
    expect(block(CSS, ".am-card-have")).toContain("background: var(--am-surface-have)");
    expect(block(CSS, ".am-card-none")).toContain("background: var(--am-surface-none)");
    const TOKENS = readFileSync(join(HERE, "../../../styles/tokens.css"), "utf8");
    expect(TOKENS.match(/--am-surface-have:/g)?.length).toBe(2);
    expect(TOKENS.match(/--am-surface-none:/g)?.length).toBe(2);
  });
});

// ---- one species of card (2026-08-25) ---------------------------------------
// A recommendation, a Hub result and a cached repo are one thing at three
// stages of its life, and the page draws them side by side in the same row —
// which is the one place a difference in SHAPE cannot be missed. Akshil, twice:
// "why do we have inconsistency between the downloaded card and the normal
// card... at least the name, the mlx-community thing and the size should be
// same. We can move the MLX LM tag onto the information icon."
describe("every card on the page has the same bones", () => {
  const REC = readFileSync(join(HERE, "RecommendedCard.tsx"), "utf8");

  it("name, then a caption line of size + repo id, in that order", () => {
    // The shared skeleton draws it once for both of the not-on-disk cards.
    expect(REC).toContain('<div className="am-card-sub">');
    expect(REC).toContain('<span className="am-card-slug cc-mono" data-hint={slug}>{slug}</span>');
    const sub = REC.slice(REC.indexOf('<div className="am-card-sub"'));
    expect(sub.indexOf("am-card-slug")).toBeLessThan(sub.indexOf("{size.text}"));
    // Both callers hand it the same two things the disk card shows.
    expect(REC.match(/slug=\{model\.id\}/g)?.length).toBe(2);
    // The Hub result shows the model half of the id up top, like the others.
    expect(REC).toContain("text: modelName(model.id),");
  });

  it("the engine tag is a row in the (i), not a chip on the face", () => {
    expect(REC).not.toContain("<EngineTag");
    expect(REC).not.toContain("engineHueStyle(");
    expect(REC.match(/<InfoButton name=\{model\.id\}/g)?.length).toBe(2);
    expect(REC).toContain('label: "Engine"');
  });

  it("…but the reason a Download is dead stays ON the face", () => {
    // The tag's `-off` state was doing a second job: the only thing explaining a
    // greyed Download. That half is not identity, so it stays — as the verb.
    expect(REC).toContain("function SwitchEngines(");
    expect(REC).toContain("{!runner?.available && <SwitchEngines runner={runner} />}");
    expect(REC).toContain("{!loadable && <SwitchEngines runner={runner} />}");
    expect(REC).toContain('className="am-card-fix"');
  });

  it("the partly-downloaded tag survives, because it is state", () => {
    expect(REC).toContain("{PARTIAL_TAG}");
  });
});
