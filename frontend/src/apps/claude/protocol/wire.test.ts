// The wire is the one thing here that outlives the app: a session written today
// is read back by `stripBlocks` / `paneShotIn` / `annotationsIn` years later, so
// every test below is a ROUND TRIP through the exact strings T writes
// (03-shots-attach-composer.md §D).
import { describe, expect, test } from "bun:test";

import {
  annotationsIn,
  APP_STATE_TAG,
  composeOutgoing,
  formatAnnotations,
  isMarkerOnly,
  MARKER_ANN,
  MARKER_FILE,
  MARKER_IMG,
  MARKER_JOIN,
  MARKER_SIGIL,
  MARKER_VIEW,
  MARKERS,
  markerWord,
  markerWords,
  paneShotBlock,
  paneShotIn,
  parseInbound,
  stripAnnBlock,
  stripAppStateBlock,
  stripBlocks,
  stripPaneBlock,
  type AnnotationWire,
  type PaneShotWire,
} from "./wire";

const appState = `<${APP_STATE_TAG}>\nA snapshot of the app the user is looking at.\n{"url":"/x"}\n</${APP_STATE_TAG}>`;

describe("composeOutgoing / stripBlocks are exact inverses", () => {
  test("blocks in reading order, message last", () => {
    const shots = paneShotBlock([{ kind: "pane", view: "/tmp/shot.png" }], "app");
    const notes = formatAnnotations([{ label: "A", tag: "button", content: "make this blue" }], "project");
    const out = composeOutgoing("please fix", [appState, shots, notes]);
    expect(out.indexOf(appState)).toBe(0);
    expect(out.indexOf(shots)).toBeGreaterThan(0);
    expect(out.indexOf(notes)).toBeGreaterThan(out.indexOf(shots));
    expect(out.endsWith("please fix")).toBe(true);
    expect(stripBlocks(out)).toBe("please fix");
  });

  test("every strip is position-independent (T:10466 — the order is a reading order)", () => {
    const shots = paneShotBlock([{ kind: "pane", view: "/tmp/a.png" }], "preview");
    const notes = formatAnnotations([{ label: "A", content: "hi" }], "file");
    // Deliberately the WRONG order, which is what broke stripAnnBlock once.
    const out = [notes, shots, appState, "words"].join("\n\n");
    expect(stripBlocks(out)).toBe("words");
  });

  test("empty blocks are dropped, not joined", () => {
    expect(composeOutgoing("hi", ["", null, undefined])).toBe("hi");
    expect(composeOutgoing("", [])).toBe("");
  });

  test("each stripper leaves nothing of its own block behind", () => {
    expect(stripAppStateBlock(appState + "\n\nhi")).toBe("hi");
    expect(stripPaneBlock(paneShotBlock([{ kind: "pane", view: null }], "app") + "\n\nhi")).toBe("hi");
    expect(stripAnnBlock(formatAnnotations([{ label: "A", content: "x" }], "file") + "\n\nhi")).toBe("hi");
  });
});

describe("markers name what a wordless send carried (T:10552)", () => {
  const wordless = (views: Parameters<typeof paneShotBlock>[0], notes?: AnnotationWire[]) =>
    stripBlocks(
      composeOutgoing("", [
        paneShotBlock(views, "app"),
        notes ? formatAnnotations(notes, "project") : "",
      ]),
    );

  test("annotations only", () => {
    expect(wordless([], [{ label: "A", content: "x" }])).toBe(MARKER_ANN);
  });

  test("a capture of this pane", () => {
    expect(wordless([{ kind: "pane", view: "/tmp/p.png" }])).toBe(MARKER_VIEW);
  });

  test("only brought-in images", () => {
    expect(
      wordless([
        { kind: "image", view: "/tmp/a.png", name: "a.png" },
        { kind: "image", view: "/tmp/b.png", name: "b.png" },
      ]),
    ).toBe(MARKER_IMG);
  });

  test("brought-in but not all images ⇒ files", () => {
    expect(
      wordless([
        { kind: "image", view: "/tmp/a.png", name: "a.png" },
        { kind: "file", view: "/tmp/b.csv", name: "b.csv", size: 12 },
      ]),
    ).toBe(MARKER_FILE);
  });

  test("a pre-`kind` session defaults to the pane case (T:10578)", () => {
    const legacy = `<pane-shot>\ncaption\n{"view":"/tmp/x.png","viewNote":"clipped"}\n</pane-shot>`;
    expect(stripBlocks(legacy)).toBe(MARKER_VIEW);
  });

  test("both ⇒ joined by \" + \"", () => {
    expect(wordless([{ kind: "pane", view: "/tmp/p.png" }], [{ label: "A", content: "x" }])).toBe(
      MARKER_ANN + MARKER_JOIN + MARKER_VIEW,
    );
  });

  test("isMarkerOnly recognises exactly what stripBlocks produces", () => {
    expect(isMarkerOnly(MARKER_ANN + MARKER_JOIN + MARKER_VIEW)).toBe(true);
    expect(isMarkerOnly(MARKER_FILE)).toBe(true);
    expect(isMarkerOnly("")).toBe(false);
    expect(isMarkerOnly("annotations and a word")).toBe(false);
  });

  test("the WORD is not the marker: a reader who types one is not a marker send", () => {
    // The emoji T put in front of each marker also made it a string no reader
    // could type. Without one, "files" — an ordinary thing to say to an agent —
    // matched, and the bubble drew an attachment icon in front of the reader's
    // own word (Bugbot, PR #1064). Marker-ness is the private sigil, never the
    // visible word.
    expect(isMarkerOnly("files")).toBe(false);
    expect(isMarkerOnly("images")).toBe(false);
    expect(isMarkerOnly("annotations")).toBe(false);
    expect(isMarkerOnly("pane screenshot")).toBe(false);
    expect(isMarkerOnly("files + images")).toBe(false);
    // The sigil is invisible, so what a marker SAYS is still the word alone.
    expect(markerWord(MARKER_FILE)).toBe("files");
    expect(markerWords(MARKER_ANN + MARKER_JOIN + MARKER_VIEW)).toBe(
      "annotations" + MARKER_JOIN + "pane screenshot",
    );
    // …and a string that never carried one comes back untouched.
    expect(markerWords("files")).toBe("files");
  });
});

describe("paneShotIn (T:10501)", () => {
  test("today's array", () => {
    const block = paneShotBlock(
      [
        { kind: "overview", view: "/tmp/o.png", viewNote: "cropped" },
        { kind: "file", view: "/tmp/x.csv", name: "x.csv", size: 9 },
      ],
      "app",
    );
    expect(paneShotIn(block)).toEqual([
      { kind: "overview", view: "/tmp/o.png", viewNote: "cropped" },
      { kind: "file", view: "/tmp/x.csv", name: "x.csv", size: 9 },
    ]);
  });

  test("a legacy bare object answers as a one-element list", () => {
    const legacy = `<pane-shot>\ncaption\n{"view":"/tmp/x.png"}\n</pane-shot>`;
    // A pre-`kind` payload has no `kind` at all, which is exactly what the
    // marker default relies on — so the shape is asserted as-is.
    expect(paneShotIn(legacy)).toEqual([{ view: "/tmp/x.png" } as PaneShotWire]);
  });

  test("unparseable payload is [] and never a throw", () => {
    expect(paneShotIn(`<pane-shot>\nnot json\n</pane-shot>`)).toEqual([]);
    expect(paneShotIn("no block at all")).toEqual([]);
  });

  test("the payload is the LAST line, so the caption's wording may change", () => {
    const block = `<pane-shot>\none\ntwo\nthree\n[{"kind":"pane","view":null}]\n</pane-shot>`;
    expect(paneShotIn(block)).toEqual([{ kind: "pane", view: null }]);
  });
});

describe("annotationsIn (T:11179) round-trips formatAnnotations", () => {
  test("element note with tag, digest, anchor, image fractions and a clock", () => {
    const note: AnnotationWire = {
      label: "A",
      tag: "button",
      text: "Save",
      anchorId: "save",
      iu: 0.4,
      iv: 0.6,
      t: 65,
      content: "make this blue",
    };
    const block = formatAnnotations([note], "file");
    // NOTE: one "%" only. T:10312 writes `"%×" + round(iv*100) + " of its
    // content box"` — the inventory's prose says "60%" but the template does
    // not, and this is a VERBATIM port of the template.
    expect(block).toContain("**A** — `<button>` — “Save” — `#save` — at 40%×60 of its content box  · 1:05");
    const back = annotationsIn(block);
    expect(back.length).toBe(1);
    expect(back[0].label).toBe("A");
    expect(back[0].tag).toBe("button");
    expect(back[0].t).toBe(65);
    expect(back[0].content).toBe("make this blue");
  });

  test("a point note keeps its coordinates", () => {
    const block = formatAnnotations(
      [{ label: "B", kind: "point", x: 12, y: -3, nearPath: "main > div", content: "here" }],
      "project",
    );
    expect(block).toContain("**B** — point (12, -3) inside `main > div`");
    const back = annotationsIn(block);
    expect(back[0]).toMatchObject({ label: "B", kind: "point", x: 12, y: -3, content: "here" });
  });

  test("a digest that quotes coordinates is NOT read as a point (Bugbot PR #783)", () => {
    // No `tag`, so the digest is FIRST — and arrives in curly quotes, which the
    // point pattern cannot start with.
    const block = formatAnnotations([{ label: "C", text: "point (1, 2)", content: "x" }], "file");
    const back = annotationsIn(block);
    expect(back[0].kind).toBeUndefined();
    expect(back[0].x).toBeUndefined();
  });

  test("blank lines inside a note collapse (the stanza boundary is reserved)", () => {
    const block = formatAnnotations([{ label: "A", tag: "p", content: "one\n\n\ntwo\nthree" }], "file");
    expect(annotationsIn(block)[0].content).toBe("one\ntwo\nthree");
  });

  test("a wordless spot round-trips to \"\", not to our own apology", () => {
    const block = formatAnnotations([{ label: "A", tag: "p", content: "" }], "file");
    expect(block).toContain("_(no words for this spot)_");
    expect(annotationsIn(block)[0].content).toBe("");
  });

  test("offscreen caveat rides with its own entry", () => {
    const block = formatAnnotations([{ label: "A", tag: "p", offscreen: "scrolled out", content: "x" }], "file");
    const back = annotationsIn(block)[0];
    expect(back.offscreen).toBe("scrolled out");
    expect(back.content).toBe("x");
  });

  test("timed notes sort ascending, untimed last, and the preamble says so", () => {
    const block = formatAnnotations(
      [
        { label: "A", tag: "p", content: "typed" },
        { label: "B", tag: "p", t: 5, content: "second" },
        { label: "C", tag: "p", t: 1, content: "first" },
      ],
      "project",
    );
    expect(block).toContain("spoken walkthrough");
    expect(annotationsIn(block).map((c) => c.label)).toEqual(["C", "B", "A"]);
  });

  test("the legacy tag-less shape is still readable (T:11189)", () => {
    const legacy =
      'The user annotated 1 thing.\n```json\n[{"label":"A","content":"x"}]\n```\nplease fix';
    expect(stripAnnBlock(legacy)).toBe("please fix");
    expect(annotationsIn(legacy)).toEqual([{ label: "A", content: "x" }]);
  });

  test("the singular/plural preamble matches the count", () => {
    expect(formatAnnotations([{ label: "A" }], "file")).toContain("annotated 1 thing in the left preview");
    expect(formatAnnotations([{ label: "A" }, { label: "B" }], "project")).toContain(
      "annotated 2 things in the running app",
    );
  });
});

describe("parseInbound", () => {
  test("hands back the bubble text plus everything a receipt needs", () => {
    const out = composeOutgoing("fix the header", [
      appState,
      paneShotBlock([{ kind: "overview", view: "/tmp/o.png" }], "app"),
      formatAnnotations([{ label: "A", tag: "h1", content: "this one" }], "project"),
    ]);
    const inbound = parseInbound(out);
    expect(inbound.text).toBe("fix the header");
    expect(inbound.appState).toContain('{"url":"/x"}');
    expect(inbound.paneShots).toEqual([{ kind: "overview", view: "/tmp/o.png" }]);
    expect(inbound.annotations?.[0].content).toBe("this one");
  });

  test("a plain message carries nothing", () => {
    expect(parseInbound("hello")).toEqual({ text: "hello" });
    expect(parseInbound(null)).toEqual({ text: "" });
  });
});

describe("paneShotBlock's noun (T:10412)", () => {
  test("a lone file is \"a file\", never \"1 a file\"", () => {
    expect(paneShotBlock([{ kind: "file", view: "/x", name: "x" }], "app")).toContain(
      "The user attached a file to this message",
    );
  });
  test("two pictures are counted", () => {
    expect(
      paneShotBlock(
        [
          { kind: "image", view: "/a", name: "a" },
          { kind: "image", view: "/b", name: "b" },
        ],
        "app",
      ),
    ).toContain("The user attached 2 pictures");
  });
  test("mixed has no honest singular noun", () => {
    expect(
      paneShotBlock(
        [
          { kind: "image", view: "/a", name: "a" },
          { kind: "file", view: "/b", name: "b" },
        ],
        "app",
      ),
    ).toContain("The user attached 2 attachments");
  });
  test("no views ⇒ no block", () => {
    expect(paneShotBlock([], "app")).toBe("");
  });
});

describe("a stanza with no descriptive bits does not round-trip — same as T", () => {
  // `annStanza` writes "**A** — " with an empty `bits.join(" — ")`, and
  // `annStanzaIn` trims the line before matching `/^\*\*(.+?)\*\* — /`, whose
  // trailing space is then gone. T has the exact same behaviour (T:10333 vs
  // T:11140), and a send is gated on words having landed, so the shape barely
  // occurs; pinned here so a "fix" to either half is a deliberate change.
  test("written, but read back as nothing", () => {
    const block = formatAnnotations([{ label: "A", content: "words" }], "file");
    expect(block).toContain("**A** — \nwords");
    expect(annotationsIn(block)).toEqual([]);
    // stripBlocks still peels it: the strip matches the TAG, not the stanza.
    expect(stripBlocks(block + "\n\nhi")).toBe("hi");
  });
});

// ---- ONE marker vocabulary (B-30's wire half) ------------------------------
//
// There are two spellings of this vocabulary in the tree and the LIVE Recent
// list renders the wrong one: `ui/list-rows.ts` declares its own
// `MARKER_JOIN = " · "` under a comment citing T:10527-10538 — where both T and
// this module say `" + "` — and invents `"picture"`/`"comments"` for
// `MARKER_VIEW`/`MARKER_ANN`, so a wordless send appears in Recent as
// `picture · comments` where T shows `🖼 pane screenshot + 📌 annotations`
// (lucide glyphs here, P2-7). `protocol/history.ts` is the copy that DOES import
// the real markers and run `markerWords` — and it is imported by nothing but its
// own test.
//
// `ui/list-rows.ts`, `ui/RecentRow.tsx` and `ui/Lists.tsx` are PR4's files, so
// the CONSUMER is PR4's fix (its own item, merged with audit C's G-8: delete
// list-rows' copies and re-export `history.ts`'s `sessionTitle`). What belongs
// here is the contract that fix imports against — nothing tested `list-rows`'s
// copy against this one, which is exactly the drift D146's "duplicated wire
// rules get tests" exists to catch.
describe("the marker vocabulary is this module's, and nobody else's", () => {
  test("the join is T:10538's `\" + \"`", () => {
    expect(MARKER_JOIN).toBe(" + ");
  });

  test("each marker's WORDS name the thing, not a category", () => {
    // The words T:10527-10537 uses. The emoji are replaced by lucide icons
    // (P2-7, pinned by `ui/no-emoji.test.ts`); the words are not.
    expect(MARKER_ANN).toContain("annotations");
    expect(MARKER_VIEW).toContain("pane screenshot");
    // …and NOT the inventions the Recent list renders today.
    expect(MARKERS.join(MARKER_JOIN)).not.toContain("picture");
    expect(MARKERS.join(MARKER_JOIN)).not.toContain("comments");
  });

  test("a send carrying BOTH blocks reads as T spells it", () => {
    // The row a wordless "screenshot + notes" send should produce, which is
    // what a consumer joining these must come out with.
    const both = [MARKER_ANN, MARKER_VIEW].join(MARKER_JOIN);
    expect(markerWords(both)).toBe("annotations + pane screenshot");
    // The sigil is STRIPPED by `markerWords` — a label is words, not wire.
    expect(markerWords(both)).not.toContain(MARKER_SIGIL);
  });

  test("`markerWords` is the one door from wire to label", () => {
    expect(markerWords(MARKER_VIEW)).toBe("pane screenshot");
    expect(markerWords("just words")).toBe("just words");
  });
});
