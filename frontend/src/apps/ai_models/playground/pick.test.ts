import { expect, test } from "bun:test";

import { pickPlaygroundModel, playgroundModels } from "./pick";
import type { AiCatalogCapability, AiCatalogModel } from "@platform/lib/api";

// `pick.ts` imports nothing but a type, so there is no shim here and no dynamic
// import — the whole reason the rule was lifted out of the component (D425).

function model(id: string, over: Partial<AiCatalogModel> = {}): AiCatalogModel {
  return {
    id,
    label: id,
    size_gb: 1,
    note: null,
    source: "curated",
    downloaded: false,
    loaded: false,
    recommended: false,
    ...over,
  };
}

function row(
  capability: string,
  models: AiCatalogModel[],
  over: Partial<AiCatalogCapability> = {},
): AiCatalogCapability {
  return {
    capability,
    runner: "some-runner",
    runnerLabel: "Some Runner",
    runnerShortLabel: "Some Runner",
    runnerNote: null,
    available: true,
    reason: null,
    default: models[0]?.id ?? null,
    models,
    videoTraits: null,
    ...over,
  };
}

test("only recommended or owned models are offered", () => {
  const offered = playgroundModels(
    row("text-generation", [
      model("small", { recommended: true }),
      model("huge"),
      model("fetched", { downloaded: true }),
      model("mine", { source: "cached", downloaded: true }),
    ]),
  );
  expect(offered.map((m) => m.id)).toEqual(["small", "fetched", "mine"]);
});

test("a resident model is offered even while the disk scan is stale", () => {
  // `loaded` is live and `downloaded` is memoised, so this is a real state and
  // not a defensive one: hiding the model answering right now would be the
  // worst thing this filter could do.
  const offered = playgroundModels(
    row("text-generation", [model("busy", { loaded: true })]),
  );
  expect(offered.map((m) => m.id)).toEqual(["busy"]);
});

test("the catalog's order survives the filter", () => {
  const offered = playgroundModels(
    row("text-generation", [
      model("a", { recommended: true }),
      model("b"),
      model("c", { recommended: true }),
      model("d", { source: "cached", downloaded: true }),
    ]),
  );
  expect(offered.map((m) => m.id)).toEqual(["a", "c", "d"]);
});

test("?model= wins when it names an offered row", () => {
  const rows = [
    row("text-generation", [model("small", { recommended: true }), model("big", { recommended: true })]),
  ];
  expect(pickPlaygroundModel(rows, "big", null)?.model.id).toBe("big");
});

test("?model= naming a model this tab does not offer falls back silently", () => {
  const rows = [
    row("text-generation", [model("small", { recommended: true }), model("huge")]),
  ];
  const picked = pickPlaygroundModel(rows, "huge", null);
  expect(picked?.model.id).toBe("small");
});

test("the fallback is the catalog default when the default is offered", () => {
  const rows = [
    row(
      "automatic-speech-recognition",
      [model("tiny", { recommended: true }), model("turbo", { recommended: true })],
      { default: "tiny" },
    ),
  ];
  expect(pickPlaygroundModel(rows, null, null)?.model.id).toBe("tiny");
});

test("a default this tab does not offer falls back to the first offered row", () => {
  // The catalog's default is its SMALLEST entry and owes nothing to
  // `recommended` (catalog.py), so this is the ordinary case rather than a
  // misconfiguration: the smallest whisper is unrecommended, and the first
  // model the page can actually run stands in for it.
  const rows = [
    row(
      "automatic-speech-recognition",
      [model("tiny"), model("small", { recommended: true }), model("turbo", { recommended: true })],
      { default: "tiny" },
    ),
  ];
  expect(pickPlaygroundModel(rows, null, null)?.model.id).toBe("small");
});

test("?cap= steers the fallback to its capability, never past a ?model= hit", () => {
  const rows = [
    row("text-generation", [model("chat", { recommended: true })]),
    row("text-to-image", [model("flux", { recommended: true })]),
  ];
  expect(pickPlaygroundModel(rows, null, "text-to-image")?.model.id).toBe("flux");
  // An explicit model still wins over the task hint.
  expect(pickPlaygroundModel(rows, "chat", "text-to-image")?.model.id).toBe("chat");
  // An unknown cap falls through to the first usable capability.
  expect(pickPlaygroundModel(rows, null, "telepathy")?.model.id).toBe("chat");
});

test("an unavailable capability is never selected, however it is asked for", () => {
  const rows = [
    row("text-generation", [model("chat", { recommended: true })]),
    row("text-to-image", [model("flux", { recommended: true })], {
      available: false,
      reason: "needs Apple Silicon",
    }),
  ];
  expect(pickPlaygroundModel(rows, "flux", null)?.model.id).toBe("chat");
  expect(pickPlaygroundModel(rows, null, "text-to-image")?.model.id).toBe("chat");
});

test("a capability whose whole shortlist is filtered out is skipped, not selected", () => {
  const rows = [
    row("text-to-image", [model("huge"), model("bigger")]),
    row("text-generation", [model("chat", { recommended: true })]),
  ];
  const picked = pickPlaygroundModel(rows, null, null);
  expect(picked?.row.capability).toBe("text-generation");
  expect(picked?.model.id).toBe("chat");
});

test("nothing offered anywhere is null, not a crash", () => {
  expect(pickPlaygroundModel([], null, null)).toBe(null);
  expect(pickPlaygroundModel([row("text-generation", [model("huge")])], "huge", null)).toBe(null);
});
