// The composer seed's embeddings branch (SPEC §40).
//
// **Why this is worth a test at all.** The seed is the most authoritative thing
// in a spawned session's context — it is spliced into the prompt at submit time,
// invisible to the user, and read before any code is written. So a seed that
// names a parameter the route REFUSES does not produce a broken build; it
// produces a page written confidently around a call that 400s, and a user who
// blames the model. The two parameters are per-model on this capability, so the
// prose has to be too.
//
// Driven through `buildAppAnnotation`, the exported entry point, rather than the
// private assembler: what is pinned is what a session actually receives.
import { describe, expect, it } from "bun:test";
import { buildAppAnnotation } from "./appSeed";
import { type AiCatalogModel } from "@platform/lib/api";

function model(over: Partial<AiCatalogModel> = {}): AiCatalogModel {
  return {
    id: "org/thing",
    label: "Thing",
    nickname: "Thing",
    size_gb: 1,
    source: "curated",
    downloaded: true,
    loaded: false,
    recommended: false,
    ...over,
  } as AiCatalogModel;
}

const seed = (over: Partial<AiCatalogModel>) =>
  buildAppAnnotation(model(over), "embeddings").detail;

describe("a PROSE model's seed names kind and refuses paths", () => {
  const detail = seed({
    id: "nomic-ai/nomic-embed-text-v1.5",
    acceptsPaths: false,
    promptScheme: "nomic",
  });

  it("names the kind parameter and both its values", () => {
    expect(detail).toContain('kind: "query"');
    expect(detail).toContain('kind: "document"');
  });

  it("says what the default is, since leaving it out is a real choice", () => {
    // `embed_common.DEFAULT_KIND`. A session that does not know the default
    // cannot decide whether to pass the field at all.
    expect(detail).toContain("document");
    expect(detail).toContain("worse at retrieval");
  });

  it("states outright that paths is refused, rather than staying silent", () => {
    // Omission is not enough here: the capability's own docs describe both
    // halves, so a session left to infer will reach for `paths`.
    expect(detail).toContain("no vision tower");
    expect(detail).toContain("400");
  });

  it("does not tell the session to pass paths", () => {
    expect(detail).not.toContain("{ paths } instead of { texts }");
  });
});

describe("a DUAL encoder's seed names paths and refuses kind", () => {
  const detail = seed({
    id: "onnx-community/siglip2-base-patch16-384-ONNX",
    acceptsPaths: true,
    promptScheme: null,
  });

  it("names paths and the space the two towers share", () => {
    expect(detail).toContain("{ paths } instead of { texts }");
    expect(detail).toContain("same space");
  });

  it("says there is no kind to pass", () => {
    expect(detail).toContain("no retrieval prompt convention");
    expect(detail).toContain("no kind parameter");
  });

  it("does not offer kind as a value", () => {
    expect(detail).not.toContain('kind: "query"');
  });
});

describe("an older server's payload, with neither field", () => {
  const detail = seed({ id: "org/mystery" });

  it("reads absence as no rather than as yes", () => {
    // `=== true` / `?? null` on both flags: a payload with neither field is a
    // server that predates them, and the safe seed is the one that promises
    // less. Both negative halves, and neither positive one.
    expect(detail).toContain("no vision tower");
    expect(detail).toContain("no kind parameter");
    expect(detail).not.toContain("{ paths } instead of { texts }");
    expect(detail).not.toContain('kind: "query"');
  });
});

describe("the text-to-image seed's guidance", () => {
  // `defaults.guidance` follows the exact shape `defaults.steps` already has
  // here: a curated value is offered as the option's fallback, `readParam`
  // still wins when the user actually moved the Playground's slider, and
  // absence of both means the key is left out entirely rather than seeded as
  // a guess — the same "server-shaped extra, or nothing" contract as steps.
  const seedImage = (over: Partial<AiCatalogModel>) =>
    buildAppAnnotation(model(over), "text-to-image").detail;

  it("a model whose curated defaults declare a guidance seeds it", () => {
    const detail = seedImage({
      id: "segmind/tiny-sd",
      defaults: { steps: 16, guidance: 7.5 },
    });
    expect(detail).toContain("guidance: 7.5");
  });

  it("a model that declares none, and whose URL carries none either, omits the key", () => {
    const detail = seedImage({ id: "black-forest-labs/FLUX.2-klein-4B" });
    expect(detail).not.toContain("guidance:");
  });
});

describe("what every embeddings seed says regardless", () => {
  const detail = seed({ id: "org/thing", acceptsPaths: true, promptScheme: "e5" });

  it("names the call, the model id and the unit-length guarantee", () => {
    expect(detail).toContain("fused.ai.embed({ texts");
    expect(detail).toContain('"org/thing"');
    expect(detail).toContain("unit-length");
  });

  it("still points at the skill, which is the authoritative contract", () => {
    // Task 18 updates that file; a seed that named the API without pointing at
    // it invites the session to improvise the rest.
    expect(detail).toContain("load the `fused-render-ai` skill");
  });
});
