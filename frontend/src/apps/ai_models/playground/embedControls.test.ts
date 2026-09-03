// The two per-model controls on the embeddings stage (SPEC §40), pinned
// against the SOURCE the way `local/repoCardControls.test.ts` pins the Local
// card's own conditions.
//
// **Why source text rather than a render.** What is being pinned is not what
// the controls look like — it is that each one is drawn off the SERVER's own
// flag and off nothing else. `entry.acceptsPaths` is computed from the cached
// checkpoint's `model_type`, and `entry.promptScheme` from the curated scheme
// table; the route refuses the corresponding parameter on exactly the same
// facts. A control drawn off anything a reader could reason about locally — the
// repo id looking like a SigLIP, the capability being embeddings, the model
// being downloaded — is a control whose request comes back 400 for a model
// nobody anticipated. So these tests read the conditions themselves.
//
// The client half is pinned too: `kind` must be OMITTED rather than sent as a
// default when the model has no scheme, since the route 400s it.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const HERE = new URL(".", import.meta.url).pathname;
const STAGE = readFileSync(join(HERE, "EmbedStage.tsx"), "utf8");
const CLIENT = readFileSync(join(HERE, "client.ts"), "utf8");
const TAB = readFileSync(join(HERE, "PlaygroundTab.tsx"), "utf8");

describe("the images mode is drawn off acceptsPaths and nothing else", () => {
  it("reads the server's flag with === true", () => {
    // `=== true`, not truthy: an older server sends no `acceptsPaths` at all,
    // and `undefined` has to read as "no images mode" rather than as one whose
    // every request 400s. The same rule `imageInput.canEdit` states.
    expect(STAGE).toContain("const ranksPictures = entry.acceptsPaths === true;");
  });

  it("gates the mode switch on it", () => {
    expect(STAGE).toContain("{ranksPictures && (");
  });

  it("cannot enter the mode without it, whatever the reader clicked", () => {
    // The reader's choice and the model's capability are two facts, and the
    // mode is their AND. Without this a reader who picked Pictures and then
    // switched to a prose model would sit in a mode whose every search 400s —
    // `usableBase`'s argument about an attachment surviving a model switch.
    expect(STAGE).toContain("const pictureMode = ranksPictures && wantPictures;");
  });

  it("does not decide it from the repo id or the capability", () => {
    expect(STAGE).not.toContain('includes("siglip")');
    expect(STAGE).not.toContain('includes("clip")');
    expect(STAGE).not.toMatch(/capability\s*===\s*"embeddings"/);
  });
});

describe("the retrieval-kind toggle is drawn off promptScheme and nothing else", () => {
  it("reads the server's answer, treating null as no toggle", () => {
    expect(STAGE).toContain("const scheme = entry.promptScheme ?? null;");
  });

  it("draws the toggle only with a scheme, and never in the images mode", () => {
    // Two conditions, both load-bearing. No scheme: the route 400s the field.
    // Images mode: there is no text corpus for a query/passage distinction to
    // be about, and a dual encoder has no scheme anyway — so the toggle would
    // be a control the reader can move that changes nothing.
    expect(STAGE).toContain("{scheme && !pictureMode && (");
  });

  it("omits kind from the request when there is no scheme", () => {
    // `scheme ? kind : undefined` — the state exists either way (a default has
    // to be something), so the CALL is where the omission has to happen.
    expect(STAGE).toContain("scheme ? kind : undefined");
  });

  it("never sends a kind on the pictures call", () => {
    // A model that reports `acceptsPaths` is a dual encoder and has no scheme,
    // so the toggle is not drawn — but the phrase call in `runPictures` is a
    // TEXTS call and could have passed one by copy-paste.
    const runPictures = STAGE.slice(STAGE.indexOf("const runPictures"));
    expect(runPictures).toContain("embedTexts(model, [asked])");
  });
});

describe("the client sends kind only when it is given one", () => {
  it("builds the body conditionally rather than defaulting", () => {
    expect(CLIENT).toContain(
      "JSON.stringify(kind ? { model, texts, kind } : { model, texts })",
    );
  });

  it("types kind as the closed set the route accepts", () => {
    // `formats.TEXT_EMBED_KINDS`. A `string` here would let a typo reach the
    // route, which refuses it — correctly, and one round trip later than the
    // compiler would have.
    expect(CLIENT).toContain('kind?: "query" | "document"');
  });

  it("has a paths call that shares the reply reader", () => {
    // One reader for both halves: the 409-means-LOADING fork is what a copy
    // would drift on, and losing the job id loses the download the caller
    // would have shown.
    expect(CLIENT).toContain("export async function embedPaths(");
    expect(CLIENT).toContain("async function readEmbedReply(res: Response)");
    const paths = CLIENT.slice(CLIENT.indexOf("export async function embedPaths("));
    expect(paths).toContain("return readEmbedReply(res);");
  });
});

describe("the capability dispatch is untouched", () => {
  it("still routes embeddings to one stage", () => {
    // The two modes are a property of the MODEL, not of the capability — a
    // second capability key, a second static home card or a second branch here
    // would all promise an image search on a machine whose resolved model is a
    // prose encoder.
    expect(TAB).toContain('selected.row.capability === "embeddings" ? (');
    expect(TAB).not.toContain('"embed-text"');
  });

  it("hands the stage the whole entry, since that is where the flags are", () => {
    const stage = TAB.slice(TAB.indexOf("<EmbedStage"));
    expect(stage.slice(0, 400)).toContain("entry={selected.model}");
  });
});

describe("the displayed scores say which model produced them", () => {
  // Same source-text approach as everything above, and for a sharper version of
  // the same reason: what is being pinned is that the label reads RECORDED
  // state, not the live selection. A render test could show the right string
  // while reading the wrong variable, because on first run the two agree.

  it("records the model at the run instead of rendering the live selection", () => {
    // `model` is the sidebar's choice and changes the moment the reader picks
    // another one, while the results on screen are still the previous model's.
    // A label reading `model` would therefore name something that did not
    // compute the scores beneath it.
    expect(STAGE).toContain("const [vectorModel, setVectorModel]");
    // D632: the model that ran is `response.modelId` on the result frame.
    expect(STAGE).toContain("setVectorModel(result.response?.modelId ?? model)");
    expect(STAGE).toContain("setVectorModel(images.response?.modelId ?? model)");
    // The label renders the recorded value, never the prop.
    expect(STAGE).toContain("{vectorModel}");
    expect(STAGE).not.toContain("provenance={model}");
  });

  it("prefers the SERVER's answer over the requested id", () => {
    // A bare call takes the capability's default, so the request's own `model`
    // is not always what ran. `result.response.modelId` first, the prop only
    // as the older-server fallback.
    const setters = STAGE.match(/setVectorModel\([^)]*\)/g) ?? [];
    expect(setters.length).toBe(2);
    for (const setter of setters) expect(setter).toContain(".modelId ??");
  });

  it("both result surfaces carry it, texts and pictures alike", () => {
    // Two answer blocks, two provenance labels — the picture mode is where a
    // model switch is most tempting, since the corpus stays put.
    const labels = STAGE.match(/provenance=\{vectorModel\}/g) ?? [];
    expect(labels.length).toBe(2);
  });
});

describe("a picture's display name survives a native path", () => {
  it("splits on both separators, not just the POSIX one", () => {
    // `pickFile` returns a NATIVE path, so on Windows `C:\\photos\\cat.png` has no
    // "/" to find and the whole drive path became the "name" — in the corpus
    // list and in every ranked row.
    expect(STAGE).toContain("function basename(");
    expect(STAGE).toContain('path.lastIndexOf("\\\\")');
    // …and no caller left on the old one-separator split.
    expect(STAGE).not.toContain('path.split("/").pop()');
  });
});
