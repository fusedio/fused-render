// The Local tab's bucketing, driven directly. The page renders these groups;
// what can actually be WRONG is which bucket a repo lands in, and the wrong
// answer is invisible on a screenshot — a mystery repo and a model this machine
// cannot load look identical once they are both wearing a dead-end tag.
import { describe, expect, it } from "bun:test";
import type { AiCatalogCapability, AiCatalogModel, AiModelRepo } from "@platform/lib/api";
import {
  PARTIAL_TAG,
  UNRECOGNISED,
  type DiskCard,
  diskCards,
  emptyShell,
  jobFraction,
  partialFraction,
  resultDisk,
  runnersByCapability,
  groupRepos,
  loadRefusal,
  mergeSections,
  noEngineReason,
  partialNote,
  resumable,
} from "@apps/ai_models/lib/aiModelGroups";

function repo(over: Partial<AiModelRepo> & { id: string }): AiModelRepo {
  return {
    // Defaults to `size`, which is true of every repo whose download finished —
    // the two only diverge mid-fetch, where a part file is preallocated to its
    // full length (D440), so a test about that case sets it explicitly.
    fetchedBytes: over.size ?? 1000,
    dir: "models--" + over.id.replace("/", "--"),
    kind: "model",
    path: "/cache/" + over.id,
    size: 1000,
    files: 3,
    mtime: null,
    lastUsed: null,
    added: null,
    task: null,
    taskTag: null,
    taskSource: null,
    taskHelp: null,
    library: null,
    params: null,
    paramsEstimated: false,
    quantization: null,
    capability: null,
    engine: null,
    component: null,
    revisions: 1,
    refs: [],
    partial: false,
    ...over,
  };
}

function engine(over: Partial<NonNullable<AiModelRepo["engine"]>> = {}) {
  return {
    code: "mlx-lm",
    label: "MLX LM (Apple Silicon)",
    shortLabel: "MLX LM",
    familyLabel: "MLX LM",
    available: true,
    reason: null,
    ...over,
  };
}

function component(over: Partial<NonNullable<AiModelRepo["component"]>> = {}) {
  return {
    id: "unsloth/FLUX.2-klein-4B-GGUF",
    of: "black-forest-labs/FLUX.2-klein-4B",
    owner: "FLUX.2 klein 4B",
    part: "quantized transformer",
    what: "The quantized transformer the FLUX.2 klein recipe loads.",
    file: "flux2-klein-Q4_K_M.gguf",
    ...over,
  };
}

// The live cache this design was measured against, trimmed to the four shapes
// that decide a bucket. Named after the real repos so a failure names a row
// somebody can go and look at.
const QWEN_LOADABLE = repo({
  id: "mlx-community/Qwen3.5-9B-OptiQ-4bit",
  size: 8_200_000_000,
  capability: "text-generation",
  engine: engine(),
});
const QWEN_NO_ENGINE = repo({
  id: "mlx-community/Qwen3-8B-4bit",
  size: 4_600_000_000,
  capability: "text-generation",
  engine: null,
});
const FLUX_MLX = repo({
  id: "mlx-community/FLUX.2-Klein-4B-4bit",
  size: 4_600_000_000,
  capability: "text-to-image",
  engine: engine({ code: "mflux-image", shortLabel: "MLX FLUX", label: "MLX FLUX (Apple Silicon)" }),
});
const WHISPER = repo({
  id: "mlx-community/whisper-large-v3-turbo",
  size: 1_500_000_000,
  capability: "automatic-speech-recognition",
  engine: engine({ code: "mlx-whisper", shortLabel: "MLX Whisper", label: "MLX Whisper (Apple Silicon)" }),
});
const GGUF_COMPONENT = repo({
  id: "unsloth/FLUX.2-klein-4B-GGUF",
  size: 2_400_000_000,
  component: component(),
});
const SILERO_COMPONENT = repo({
  id: "onnx-community/silero-vad",
  size: 2_200_000,
  component: component({
    id: "onnx-community/silero-vad",
    of: null,
    owner: "Whisper transcription",
    part: "speech detector",
  }),
});
// The orphan: no capability, no engine, no component. Nothing in this app has
// any idea what it is.
const WESPEAKER = repo({
  id: "Wespeaker/wespeaker-voxceleb-resnet34-LM",
  size: 27_000_000,
});

const ALL = [QWEN_LOADABLE, QWEN_NO_ENGINE, FLUX_MLX, GGUF_COMPONENT, WHISPER, SILERO_COMPONENT, WESPEAKER];

describe("the four buckets", () => {
  it("splits models from what an engine fetched", () => {
    const g = groupRepos(ALL);
    expect(g.components.repos.map((r) => r.id)).toEqual([
      "unsloth/FLUX.2-klein-4B-GGUF",
      "onnx-community/silero-vad",
    ]);
    const inModels = g.models.groups.flatMap((s) => s.repos.map((r) => r.id));
    expect(inModels).not.toContain("unsloth/FLUX.2-klein-4B-GGUF");
    expect(inModels).not.toContain("onnx-community/silero-vad");
  });

  it("sub-groups models by capability, in the reading order", () => {
    const g = groupRepos(ALL);
    expect(g.models.groups.map((s) => s.label)).toEqual([
      "Text generation",
      "Image generation",
      "Speech to text",
      "Unrecognised",
    ]);
  });

  // The whole reason this change exists. Bucketing on `engine === null` — the
  // easy wrong implementation — puts Qwen3-8B-4bit beside the Wespeaker orphan,
  // which is precisely the confusion the page is trying to remove.
  it("keeps a model with no engine under its capability, not under Unrecognised", () => {
    const g = groupRepos(ALL);
    const text = g.models.groups.find((s) => s.key === "text-generation");
    expect(text?.repos.map((r) => r.id)).toEqual([
      "mlx-community/Qwen3.5-9B-OptiQ-4bit",
      "mlx-community/Qwen3-8B-4bit",
    ]);
    const orphans = g.models.groups.find((s) => s.key === UNRECOGNISED);
    expect(orphans?.repos.map((r) => r.id)).toEqual([
      "Wespeaker/wespeaker-voxceleb-resnet34-LM",
    ]);
  });

  it("takes a capability the map has no label for at its own name, in server order", () => {
    const g = groupRepos([
      repo({ id: "acme/embedder", capability: "feature-extraction" }),
      repo({ id: "acme/ranker", capability: "text-ranking" }),
    ]);
    expect(g.models.groups.map((s) => s.key)).toEqual(["feature-extraction", "text-ranking"]);
    expect(g.models.groups.map((s) => s.label)).toEqual(["feature-extraction", "text-ranking"]);
  });

  it("renders nothing for a subgroup with no members", () => {
    const g = groupRepos([WHISPER]);
    expect(g.models.groups.map((s) => s.key)).toEqual(["automatic-speech-recognition"]);
    expect(g.components.repos).toEqual([]);
  });

  it("has no groups at all for an empty cache", () => {
    const g = groupRepos([]);
    expect(g.models.groups).toEqual([]);
    expect(g.models.size).toBe(0);
    expect(g.components.repos).toEqual([]);
    expect(g.components.size).toBe(0);
  });
});

describe("what each section costs", () => {
  it("sums every repo it holds, and the two sections sum to the whole cache", () => {
    const g = groupRepos(ALL);
    for (const sub of g.models.groups) {
      expect(sub.size).toBe(sub.repos.reduce((n, r) => n + r.size, 0));
    }
    expect(g.models.size).toBe(g.models.groups.reduce((n, s) => n + s.size, 0));
    expect(g.components.size).toBe(GGUF_COMPONENT.size + SILERO_COMPONENT.size);
    expect(g.models.size + g.components.size).toBe(ALL.reduce((n, r) => n + r.size, 0));
  });
});

describe("the order inside a subgroup is the listing's own", () => {
  it("keeps size-descending-then-id as the server sorted it", () => {
    const big = repo({ id: "a/big", capability: "text-generation", size: 900 });
    const small = repo({ id: "z/small", capability: "text-generation", size: 100 });
    const g = groupRepos([big, small]);
    expect(g.models.groups[0].repos.map((r) => r.id)).toEqual(["a/big", "z/small"]);
  });
});

// -- the merged row: disk, then what to get -----------------------------------
// The Local tab's capability rows are two payloads joined, and every way that
// join can be wrong is invisible on a screenshot. A recommended card for a model
// already on disk is ONE model drawn twice, and it looks exactly like two
// models. A capability whose section vanished because nothing is downloaded for
// it is the empty page this whole change exists to remove, and it looks exactly
// like a capability the app does not serve.

function curated(id: string, over: Partial<AiCatalogModel> = {}): AiCatalogModel {
  return {
    id,
    label: id.split("/")[1] ?? id,
    size_gb: 4,
    note: `Why you would pick ${id}.`,
    source: "curated",
    // Both flags are on the payload and this module reads NEITHER — the page's
    // own walk is the single source of truth for "downloaded" (see
    // `mergeSections`). Seeded WRONG on purpose in the tests below, so a reader
    // that started trusting them would fail here rather than in a release.
    downloaded: false,
    loaded: false,
    // Ditto: `recommended` is the PLAYGROUND's filter (D425) and this page
    // recommends the whole curated shortlist, so a seed of false here is the
    // right kind of wrong — a reader that started filtering the Local tab on it
    // would find these rows missing.
    recommended: false,
    ...over,
  };
}

function capability(
  cap: string,
  models: AiCatalogModel[],
  over: Partial<AiCatalogCapability> = {},
): AiCatalogCapability {
  return {
    capability: cap,
    runner: "mlx-lm",
    runnerLabel: "MLX LM (Apple Silicon)",
    runnerShortLabel: "MLX LM",
    runnerNote: null,
    available: true,
    reason: null,
    default: "mlx-lm",
    models,
    ...over,
  };
}

/** What the page hands `mergeSections`: the map of models that already have a
 *  disk card. The module's OWN function, not a re-implementation — the two
 *  readings of "on disk" are the thing under test in the last describe below,
 *  and a fixture with its own idea of them would hide exactly that. */
function disked(repos: AiModelRepo[]): Map<string, DiskCard> {
  return diskCards(repos);
}

/** The page's `loadedById`, membership only — a resident worker per id. */
function resident(...ids: string[]): Map<string, unknown> {
  return new Map(ids.map((id) => [id, { model: id, state: "ready" }]));
}

const CATALOG: AiCatalogCapability[] = [
  capability("text-generation", [
    curated("mlx-community/Qwen3.5-9B-OptiQ-4bit", { downloaded: false }),
    curated("mlx-community/Llama-3.2-3B-Instruct-4bit"),
    // Not curation's pick: a repo the catalog found on THIS disk (D323). The
    // Local tab already draws every one of those as a disk card, so a
    // recommended card for one would be the same model twice in one row.
    curated("somebody/found-on-disk", { source: "cached" }),
  ]),
  capability("text-to-image", [curated("mlx-community/FLUX.2-Klein-4B-4bit")], {
    runner: "mflux-image",
    runnerShortLabel: "MLX FLUX",
  }),
  capability("automatic-speech-recognition", [curated("mlx-community/whisper-tiny")], {
    runnerShortLabel: "MLX Whisper",
  }),
];

const sectionsOf = (repos: AiModelRepo[], loaded = resident()) =>
  mergeSections(groupRepos(repos).models.groups, CATALOG, loaded, disked(repos));

describe("a capability's row is disk then recommended", () => {
  // The one order the whole row rests on. Loaded is the only state that costs
  // something continuously, so it leads; recommended is the only half that is
  // not on this machine, so it trails; and the disk rows in between run most
  // recently used first, falling back to the server's size sort when atime has
  // nothing to say.
  it("puts what is loaded first, then the rest of the disk, then recommendations", () => {
    const sections = sectionsOf(ALL, resident("mlx-community/Qwen3-8B-4bit"));
    const text = sections.find((s) => s.key === "text-generation");
    // Qwen3-8B-4bit is the SMALLER of the two and the server sorted it second;
    // being resident is what moves it.
    expect(text?.disk.map((r) => r.id)).toEqual([
      "mlx-community/Qwen3-8B-4bit",
      "mlx-community/Qwen3.5-9B-OptiQ-4bit",
    ]);
    expect(text?.recommended.map((m) => m.id)).toEqual([
      "mlx-community/Llama-3.2-3B-Instruct-4bit",
    ]);
  });

  it("keeps the listing's order when nothing is resident and atime is silent", () => {
    const text = sectionsOf(ALL).find((s) => s.key === "text-generation");
    expect(text?.disk.map((r) => r.id)).toEqual([
      "mlx-community/Qwen3.5-9B-OptiQ-4bit",
      "mlx-community/Qwen3-8B-4bit",
    ]);
  });

  // Behind the resident card the row is MRU: a horizontal row is read a few
  // cards deep, so the front holds what the user actually reaches for.
  it("orders the unloaded disk rows by last use, newest first", () => {
    const stale = repo({
      id: "a/stale",
      capability: "text-generation",
      size: 900,
      lastUsed: 1_000,
    });
    const fresh = repo({
      id: "z/fresh",
      capability: "text-generation",
      size: 100,
      lastUsed: 2_000,
    });
    const text = sectionsOf([stale, fresh]).find((s) => s.key === "text-generation");
    // The server sorted a/stale first (bigger); recency is what flips them.
    expect(text?.disk.map((r) => r.id)).toEqual(["z/fresh", "a/stale"]);
  });

  it("sorts a null lastUsed after every dated row, in the listing's order", () => {
    const dated = repo({
      id: "a/dated",
      capability: "text-generation",
      size: 100,
      lastUsed: 1_000,
    });
    const neverBig = repo({ id: "b/never-big", capability: "text-generation", size: 900 });
    const neverSmall = repo({ id: "c/never-small", capability: "text-generation", size: 500 });
    const text = sectionsOf([neverBig, neverSmall, dated]).find(
      (s) => s.key === "text-generation",
    );
    expect(text?.disk.map((r) => r.id)).toEqual(["a/dated", "b/never-big", "c/never-small"]);
  });

  // Residency still beats recency: the model costing memory RIGHT NOW leads
  // even when another card was touched more recently.
  it("keeps the resident card ahead of a more recently used one", () => {
    const recent = repo({
      id: "a/recent",
      capability: "text-generation",
      size: 900,
      lastUsed: 2_000,
    });
    const held = repo({
      id: "z/held",
      capability: "text-generation",
      size: 100,
      lastUsed: 1_000,
    });
    const text = sectionsOf([recent, held], resident("z/held")).find(
      (s) => s.key === "text-generation",
    );
    expect(text?.disk.map((r) => r.id)).toEqual(["z/held", "a/recent"]);
  });

  // The dedupe, and it is the reason the recommended half is filtered against
  // the page's walk rather than against the catalog's own `downloaded` flag.
  it("does not recommend a model this disk already has", () => {
    const text = sectionsOf(ALL).find((s) => s.key === "text-generation");
    expect(text?.recommended.map((m) => m.id)).not.toContain(
      "mlx-community/Qwen3.5-9B-OptiQ-4bit",
    );
    expect(text?.disk.map((r) => r.id)).toContain("mlx-community/Qwen3.5-9B-OptiQ-4bit");
  });

  // The catalog carries the repos it found on this disk as well as the curation
  // (D323). Those are disk cards here by definition, so recommending one would
  // be one model in a row twice.
  it("recommends the curated half only", () => {
    const ids = sectionsOf([]).flatMap((s) => s.recommended.map((m) => m.id));
    expect(ids).not.toContain("somebody/found-on-disk");
  });

  // Nothing is claimed while the walk is still running: a recommendation IS the
  // claim that this machine does not have the model.
  it("recommends nothing until the disk walk has answered", () => {
    const sections = mergeSections(groupRepos(ALL).models.groups, CATALOG, resident(), null);
    expect(sections.flatMap((s) => s.recommended)).toEqual([]);
    // …and the disk half is untouched by that: what is here is here.
    expect(sections.map((s) => s.key)).toEqual([
      "text-generation",
      "text-to-image",
      "automatic-speech-recognition",
      UNRECOGNISED,
    ]);
  });
});

describe("which rows exist at all", () => {
  // The whole point on a fresh machine, and the extension of D265's empty-state
  // fix: a capability with nothing downloaded still gets a row, because the row
  // is what says what to download.
  it("renders a capability with no disk models but something to recommend", () => {
    const sections = sectionsOf([]);
    expect(sections.map((s) => s.key)).toEqual([
      "text-generation",
      "text-to-image",
      "automatic-speech-recognition",
    ]);
    expect(sections.every((s) => s.disk.length === 0)).toBe(true);
    expect(sections.every((s) => s.recommended.length > 0)).toBe(true);
  });

  it("renders nothing at all when the catalog recommends nothing and the disk is empty", () => {
    expect(mergeSections([], [], resident(), new Map())).toEqual([]);
    expect(mergeSections([], null, resident(), new Map())).toEqual([]);
  });

  // A capability the catalog knows about and has no shortlist for is not a
  // heading: an empty row is worse than a missing one, and the Engines tab is
  // where a capability with no models is explained.
  it("drops a capability whose whole shortlist is already downloaded", () => {
    const sections = mergeSections(
      [],
      [capability("text-generation", [curated("a/one")])],
      resident(),
      new Map([["a/one", "/cache/a/one"]]),
    );
    expect(sections).toEqual([]);
  });

  // A capability only the DISK knows about keeps its place — after the three the
  // app serves, before Unrecognised — and gets no recommendations, because
  // there is no catalog entry to take them from.
  it("sorts a capability the catalog has never heard of before Unrecognised", () => {
    const sections = sectionsOf([
      WESPEAKER,
      repo({ id: "acme/ranker", capability: "text-ranking" }),
      WHISPER,
    ]);
    expect(sections.map((s) => s.key)).toEqual([
      "text-generation",
      "text-to-image",
      "automatic-speech-recognition",
      "text-ranking",
      UNRECOGNISED,
    ]);
    const ranking = sections.find((s) => s.key === "text-ranking");
    expect(ranking?.recommended).toEqual([]);
    expect(ranking?.runner).toBeNull();
    // Text generation and image generation are here on the strength of their
    // recommendations alone — that is the row the fresh-machine case needs.
    expect(sections[0].disk).toEqual([]);
    expect(sections[0].recommended.length).toBeGreaterThan(0);
  });

  // Last, past every capability known and unknown, and never recommended into:
  // "we do not know what this is" has no shortlist by definition.
  it("keeps Unrecognised last and empty of recommendations", () => {
    const sections = sectionsOf(ALL);
    expect(sections[sections.length - 1].key).toBe(UNRECOGNISED);
    expect(sections[sections.length - 1].recommended).toEqual([]);
    expect(sections[sections.length - 1].note).toBe(noEngineReason(WESPEAKER));
  });

  // The engine-fetched cards are their own section below and have nothing to do
  // with this join — nothing recommends a component, because nobody chooses one.
  it("leaves the components section alone", () => {
    const g = groupRepos(ALL);
    const sections = mergeSections(g.models.groups, CATALOG, resident(), disked(ALL));
    expect(g.components.repos.map((r) => r.id)).toEqual([
      "unsloth/FLUX.2-klein-4B-GGUF",
      "onnx-community/silero-vad",
    ]);
    const drawn = sections.flatMap((s) => s.disk.map((r) => r.id));
    expect(drawn).not.toContain("unsloth/FLUX.2-klein-4B-GGUF");
    expect(drawn).not.toContain("onnx-community/silero-vad");
  });
});

describe("what a merged row says it costs, and which engine loads it", () => {
  // D249/D251: the figure beside a heading is a claim about THIS DISK. A
  // recommended model is not on it, so it cannot be in the number — otherwise
  // the one arithmetic on the page that can be checked against the cache stops
  // agreeing with it.
  it("counts disk bytes only", () => {
    const sections = sectionsOf(ALL);
    const text = sections.find((s) => s.key === "text-generation");
    expect(text?.size).toBe(QWEN_LOADABLE.size + QWEN_NO_ENGINE.size);
    expect(sectionsOf([]).every((s) => s.size === 0)).toBe(true);
  });

  it("carries the catalog's runner so a recommended card can wear its engine tag", () => {
    const sections = sectionsOf([]);
    expect(sections.map((s) => s.runner?.shortLabel)).toEqual([
      "MLX LM",
      "MLX FLUX",
      "MLX Whisper",
    ]);
    expect(sections.every((s) => s.runner?.available)).toBe(true);
  });

  // An unavailable capability is still a row with the reason on it: hiding one
  // leaves somebody hunting for a feature that never was.
  it("passes an unavailable capability's own reason through untouched", () => {
    const sections = mergeSections(
      [],
      [
        capability("text-to-image", [curated("a/flux")], {
          available: false,
          reason: "needs Apple Silicon — MLX runs on Metal only",
        }),
      ],
      resident(),
      new Map(),
    );
    expect(sections[0].recommended.map((m) => m.id)).toEqual(["a/flux"]);
    expect(sections[0].runner?.available).toBe(false);
    expect(sections[0].runner?.reason).toBe("needs Apple Silicon — MLX runs on Metal only");
  });

  it("labels a recommended-only capability the way every other heading is labelled", () => {
    expect(sectionsOf([]).map((s) => s.label)).toEqual([
      "Text generation",
      "Image generation",
      "Speech to text",
    ]);
  });
});

// Every card offers Load. What differs is whether it is live and, when it is
// not, WHICH of the reasons applies — the three are different problems with
// different fixes, and a single "cannot load" would send all three nowhere.
describe("why Load is refused", () => {
  it("is null for a repo an available engine reads", () => {
    expect(loadRefusal(QWEN_LOADABLE)).toBeNull();
  });

  it("names the format for a repo no engine reads", () => {
    expect(loadRefusal(QWEN_NO_ENGINE)).toContain("format");
  });

  // The refusal has to agree with the heading the card is sitting under. Both
  // repos have `engine: null`, and one sentence for both told the reader that
  // the Wespeaker orphan had a format problem somebody had diagnosed — under a
  // heading saying its model type is not supported at all.
  it("does not blame the format for a repo it cannot identify at all", () => {
    const why = loadRefusal(WESPEAKER) ?? "";
    expect(why).toContain("not supported");
    expect(why).not.toContain("format");
  });

  it("names the owner for a component, and does not call it a model", () => {
    const why = loadRefusal(GGUF_COMPONENT);
    expect(why).toContain("FLUX.2 klein 4B");
    expect(why).toContain("not a model");
    expect(why).toContain("quantized transformer");
  });

  // The registry's own sentence, quoted rather than paraphrased: it is the only
  // copy of "which preference is in the way", and the page cannot synthesise it.
  it("quotes the registry's reason when the engine exists but is not in force", () => {
    const off = repo({
      id: "black-forest-labs/FLUX.2-klein-4B",
      capability: "text-to-image",
      engine: engine({
        code: "diffusers-image",
        shortLabel: "Diffusers",
        available: false,
        reason: "text-to-image is set to MLX FLUX, which does not read this format",
      }),
    });
    expect(loadRefusal(off)).toContain("MLX FLUX, which does not read this format");
  });

  it("still refuses when the registry gave no reason", () => {
    const off = repo({
      id: "a/b",
      capability: "text-generation",
      engine: engine({ available: false, reason: null }),
    });
    expect(loadRefusal(off)).toBeTruthy();
  });

  it("refuses a dataset without pretending it has an engine problem", () => {
    expect(loadRefusal(repo({ id: "squad", kind: "dataset" }))).toContain("dataset");
  });
});

// The `no engine` tag is worn by two different cards, and the page says three
// things about each: the heading over the group, the sentence on the disabled
// Load button, and the tag's own hover. They answer the same question, so they
// have to give the same answer — the hover was left blaming the weight format
// after the other two stopped, which is the exact misreading the Unrecognised
// group was added to remove.
describe("the three surfaces on a no-engine card agree", () => {
  it("blames the format only where a format is the obstacle", () => {
    expect(noEngineReason(QWEN_NO_ENGINE)).toContain("weight format");
    expect(noEngineReason(WESPEAKER)).not.toContain("format");
    expect(noEngineReason(WESPEAKER)).toContain("not supported");
  });

  // Not a paraphrase of the hover: the same string, so the two cannot drift.
  it("gives the Load button the hover's sentence, plus what loading adds", () => {
    expect(loadRefusal(QWEN_NO_ENGINE)).toBe(noEngineReason(QWEN_NO_ENGINE));
    expect(loadRefusal(WESPEAKER)?.startsWith(noEngineReason(WESPEAKER))).toBe(true);
    expect(loadRefusal(WESPEAKER)).toContain("nothing to load it as");
  });

  it("says what the Unrecognised heading over the card says", () => {
    const note = groupRepos([WESPEAKER]).models.groups[0].note ?? "";
    expect(note).toBe(noEngineReason(WESPEAKER));
  });

  // A repo with a capability keeps its format problem, which is a real
  // diagnosis with a real fix: another engine, or another copy of the weights.
  // Flattening both cards onto one sentence would lose it.
  it("does not tell a Qwen checkpoint that its model type is not supported", () => {
    expect(noEngineReason(QWEN_NO_ENGINE)).not.toContain("not supported");
  });

  // The server classifies the TASK now and writes the sentence beside the
  // classification, so a card can say which unsupported thing it is looking at.
  // "The model type is not supported" was true of a TTS model, a video pipeline
  // and a repo carrying a tag nobody has heard of, and told a reader nothing
  // about which one they had downloaded.
  it("prefers the server's own sentence for a task nothing here runs", () => {
    const tts = repo({
      id: "org/voice",
      task: "text to speech",
      taskTag: "text-to-speech",
      support: "no-runner",
      supportReason: "Speech synthesis is a separate capability from transcription.",
    });
    expect(noEngineReason(tts)).toBe(
      "Speech synthesis is a separate capability from transcription.",
    );
  });

  // …and falls back where there is nothing to say: an older server sends no
  // `support` at all, and an unidentifiable repo has earned no explanation.
  it("keeps the flat note when the server offers no reason", () => {
    const mystery = repo({ id: "org/mystery", support: "unknown", supportReason: "" });
    expect(noEngineReason(mystery)).toBe(noEngineReason(WESPEAKER));
    expect(noEngineReason(repo({ id: "org/old-server" }))).toBe(noEngineReason(WESPEAKER));
  });
});

// A download that stopped halfway (D424). The page reads "on disk" TWICE, and
// the two readings are not the same question: "this machine HAS the model" is
// what a ✓ and a settled Download click mean, while "this model already has a
// card here" is what suppresses a recommendation. One map answering both is the
// bug — a cancelled first download claimed the ✓, so the recommendation with its
// working Download button vanished and what replaced it could not be loaded.
describe("a download that never finished", () => {
  // The reported case: a curated model whose first pull was cancelled.
  const WHISPER_PARTIAL = repo({
    id: "mlx-community/whisper-tiny",
    size: 12_000_000,
    capability: "automatic-speech-recognition",
    // Half a snapshot: a revision exists, the weights do not, so no engine reads
    // it — which is exactly what made the old card claim a format problem.
    engine: null,
    revisions: 1,
    partial: true,
  });

  it("still counts as a card on the page, so nothing recommends it twice", () => {
    // NOT because it is downloaded — the server drops it from `cached_models()`
    // and hub search calls it `partial`. Because it is a CARD, and one model must
    // not appear twice in one row.
    expect(diskCards([WHISPER_PARTIAL]).get(WHISPER_PARTIAL.id)).toEqual({
      state: "partial",
      path: WHISPER_PARTIAL.path,
    });
    // Neither materialised nor stalled — the shape the map still has to exclude,
    // because counting a bare folder is what flipped a suggestion to
    // "downloaded" seconds after Download was pressed.
    const bare = repo({ id: "org/nothing-here", revisions: 0, partial: false });
    expect([...diskCards([bare]).keys()]).toEqual([]);
  });

  it("keeps its disk card and drops the recommendation for the same model", () => {
    const speech = sectionsOf([WHISPER_PARTIAL]).find(
      (s) => s.key === "automatic-speech-recognition",
    );
    expect(speech?.disk.map((r) => r.id)).toEqual(["mlx-community/whisper-tiny"]);
    // The curated row for the very same id — one model, one card, whatever
    // stage of its life it is at.
    expect(speech?.recommended.map((m) => m.id)).toEqual([]);
  });

  it("gives the recommendation back once the partial repo is deleted", () => {
    // The second way out: the trash discards the bytes, and the model is a
    // suggestion again — with a Download that starts clean.
    const speech = sectionsOf([]).find((s) => s.key === "automatic-speech-recognition");
    expect(speech?.disk).toEqual([]);
    expect(speech?.recommended.map((m) => m.id)).toEqual(["mlx-community/whisper-tiny"]);
  });

  it("never explains itself as a format problem", () => {
    // The old card's two true-about-the-format, false-about-the-download
    // sentences. `partial` outranks both.
    expect(loadRefusal(WHISPER_PARTIAL)).toBe(partialNote(WHISPER_PARTIAL));
    expect(loadRefusal(WHISPER_PARTIAL)).not.toContain("weight format");
    expect(loadRefusal(WHISPER_PARTIAL)).not.toContain("not supported");
  });

  it("says both ways out, because neither is obvious from the tag", () => {
    const note = partialNote(WHISPER_PARTIAL);
    expect(note).toContain("did not finish");
    expect(note).toContain("bytes already here");
    expect(note).toContain("trash");
    expect(PARTIAL_TAG).toBe("partly downloaded");
  });

  it("is the card's state for a model, and never for a component", () => {
    // The one card that keeps its own reading: an engine's half-fetched part is
    // the engine's own problem to re-finish on its next bring-up (AI-7e), and
    // "part of MLX Whisper" stays the more useful sentence in front of a delete
    // than an offer to resume a download nobody started.
    const halfComponent = repo({
      id: "onnx-community/silero-vad",
      component: component(),
      partial: true,
    });
    expect(resumable(WHISPER_PARTIAL)).toBe(true);
    expect(resumable(halfComponent)).toBe(false);
    expect(loadRefusal(halfComponent)).toContain("not a model");
    // A dataset in the cache with no snapshot is the same rule from the other
    // side: it is a dataset first, and a stalled fetch second.
    const halfDataset = repo({ id: "squad", kind: "dataset", partial: true });
    expect(resumable(halfDataset)).toBe(false);
    expect(loadRefusal(halfDataset)).toContain("dataset");
  });
});

// What a HUB SEARCH RESULT reports about this disk (D426). The join is the
// feature: huggingface.co cannot tell you that the model you are reading about
// is already in your cache, and this is the reading that lets the page say so —
// from its OWN listing, never from the `local` field on the search reply, which
// is frozen at the moment of the search.
describe("the disk verdict on a search result", () => {
  const HERE = repo({ id: "org/have", path: "/c/models--org--have", revisions: 2 });
  const HALF = repo({ id: "org/half", path: "/c/models--org--half", revisions: 1, partial: true });
  const cards = diskCards([HERE, HALF]);

  it("says nothing at all while the walk has not answered", () => {
    // `null` is "no idea yet", not "you don't have it". Both the ✓ and the
    // Download button are CLAIMS, and a card must make neither for the length of
    // the first walk — which is how a model already on disk showed a Download.
    expect(resultDisk("org/have", null)).toEqual({ state: "unknown", path: null });
  });

  it("reports a finished download, with somewhere to open it", () => {
    expect(resultDisk("org/have", cards)).toEqual({
      state: "downloaded",
      path: "/c/models--org--have",
    });
  });

  it("reports a stopped one as PARTIAL, and offers nowhere to open", () => {
    // The state that has to survive the trip from the carousels to the results
    // grid: a repo with bytes and no materialised snapshot is not a model
    // anybody can load, so there is no revision for a model card to describe
    // and linking there would hand someone a view that cannot load. Its card
    // offers a Download that RESUMES instead (D424).
    expect(resultDisk("org/half", cards)).toEqual({ state: "partial", path: null });
  });

  it("reports a model nobody has as absent — the one state with a Download", () => {
    expect(resultDisk("org/never-heard-of-it", cards)).toEqual({
      state: "absent",
      path: null,
    });
  });

  it("never hands out an empty path as if it were a location", () => {
    // Explore builds a URL out of this, and a link to nowhere is worse than no
    // link: it looks like an answer and lands on an error.
    const nowhere = diskCards([repo({ id: "a/b", path: "", revisions: 1 })]);
    expect(resultDisk("a/b", nowhere)).toEqual({ state: "downloaded", path: null });
  });

  it("is the SAME map the recommendations are filtered against", () => {
    // One definition of on-disk per page. Two would be two moments they were
    // true — the bug being that a model downloaded from the search results kept
    // its Download button until somebody typed again.
    for (const id of ["org/have", "org/half"]) {
      expect(cards.has(id)).toBe(true);
      expect(resultDisk(id, cards).state).not.toBe("absent");
    }
  });
});

// Which engine serves each capability — one table, read by the recommended cards
// through their section and by a search result through its `capability`.
describe("runnersByCapability", () => {
  const OFF = { available: false, reason: "no Apple Silicon here" };

  it("answers with what the catalog said, per capability", () => {
    const runners = runnersByCapability([
      capability("text-generation", []),
      capability("text-to-image", [], OFF),
    ]);
    expect(runners.get("text-generation")).toEqual({
      shortLabel: "MLX LM",
      available: true,
      reason: null,
    });
    expect(runners.get("text-to-image")).toEqual({
      shortLabel: "MLX LM",
      available: false,
      reason: "no Apple Silicon here",
    });
  });

  it("has no opinion where the catalog has none", () => {
    // A capability only this disk knows about, and the whole catalog being
    // unread. Neither is a runner, and neither may be invented: the tag it
    // would draw is a claim about what can be loaded here, and a Download
    // offered under it is a claim that the bytes would be usable.
    expect(runnersByCapability([]).get("text-generation")).toBe(undefined);
    expect(runnersByCapability(null).size).toBe(0);
  });

  it("resolves ONE runner for a capability the catalog listed twice", () => {
    const twice = runnersByCapability([
      capability("text-generation", []),
      capability("text-generation", [], OFF),
    ]);
    expect(twice.size).toBe(1);
    expect(twice.get("text-generation")?.available).toBe(true);
  });

  it("is the same answer mergeSections gives a section", () => {
    // Not a second table: a recommended card and a Hub result for the same
    // capability sit on the same page and must name the same engine.
    const cat = [capability("text-generation", [curated("mlx-community/Qwen3-4B")])];
    const section = mergeSections(
      groupRepos([repo({ id: "org/m", capability: "text-generation", revisions: 1 })]).models
        .groups,
      cat,
      new Map(),
      new Map(),
    )[0];
    expect(section.runner).toEqual(runnersByCapability(cat).get("text-generation") ?? null);
  });
});

describe("emptyShell", () => {
  it("is the folder a fetch left with no snapshot in it", () => {
    // The state from the field: one 40-byte refs/main and nothing else. It has
    // to read as "nothing to resume", because the card swaps its primary
    // control for a Delete on the strength of it.
    expect(emptyShell(repo({ id: "mlx-community/Kimi", partial: true, revisions: 0, size: 40 })))
      .toBe(true);
  });

  it("is not a partial download that has bytes on disk", () => {
    // A snapshot exists, so a resume has something to pick up (D275) — and the
    // card must keep offering it.
    expect(emptyShell(repo({ id: "mlx-community/Kimi", partial: true, revisions: 1 }))).toBe(false);
  });

  it("is not a complete repo, however few revisions it has", () => {
    expect(emptyShell(repo({ id: "org/done", partial: false, revisions: 1 }))).toBe(false);
  });
});

describe("partialFraction", () => {
  const half = repo({ id: "org/half", partial: true, size: 2_000 });

  it("reports the live job once it passes what is on disk", () => {
    // 300 of 1000 fetched this run, against 2000 bytes on disk measured against
    // the job's own total — the disk reading is capped at 95%, so it wins here,
    // which is the monotonic rule below doing its job.
    expect(
      partialFraction(half, { done: 300, total: 1_000, unit: "bytes" }, 8_000),
    ).toBe(0.95);
    // With the disk behind the job, the job is what shows.
    const barely = repo({ id: "org/barely", partial: true, size: 50 });
    expect(
      partialFraction(barely, { done: 300, total: 1_000, unit: "bytes" }, 8_000),
    ).toBeCloseTo(0.3);
  });

  it("NEVER moves the boundary backwards when a download resumes", () => {
    // The reported bug: a repo showing ~90% (bytes on disk over its total) had
    // its fill collapse the moment Download was pressed, because the new job's
    // `done` starts from what THIS run has moved rather than from what is here.
    // The two readings are both lower bounds, so the larger is the true one.
    const nearly = repo({ id: "org/nearly", partial: true, size: 900 });
    const idle = partialFraction(nearly, undefined, 1_000);
    const resuming = partialFraction(
      nearly,
      { done: 20, total: 1_000, unit: "bytes" },
      1_000,
    );
    expect(idle).toBeCloseTo(0.9);
    expect(resuming).toBe(idle);
  });

  it("prefers the JOB's total over the curated estimate as the denominator", () => {
    // `size_gb` is a round number covering every repo a model touches, so one
    // repo's share reads low against it; the job knows the size of this
    // download. 500 of 1000 on disk is half, not an eighth.
    expect(
      partialFraction(
        repo({ id: "org/x", partial: true, size: 500 }),
        { done: 1, total: 1_000, unit: "bytes" },
        8_000,
      ),
    ).toBeCloseTo(0.5);
  });

  it("falls back to bytes on disk over the curated estimate", () => {
    expect(partialFraction(half, undefined, 10_000)).toBeCloseTo(0.2);
  });

  it("is the same 2-95% clamp whichever reading answered", () => {
    // A job past its own total (a resumed fetch double-counting) must not draw a
    // finished card, and neither must a disk reading past the estimate.
    expect(
      partialFraction(half, { done: 1_200, total: 1_000, unit: "bytes" }, 1_000),
    ).toBe(0.95);
  });

  it("ignores a job that is not counting bytes", () => {
    // A venv build reports no total, and a load reports a different unit;
    // neither is a fraction of this download.
    expect(partialFraction(half, { done: null, total: null }, 10_000)).toBeCloseTo(0.2);
    expect(
      partialFraction(half, { done: 1, total: 4, unit: "steps" }, 10_000),
    ).toBeCloseTo(0.2);
  });

  it("says nothing rather than guess a denominator", () => {
    // The card draws a flat wash for null. A made-up total would draw a
    // precise-looking boundary over a guess.
    expect(partialFraction(half, undefined, null)).toBeNull();
    expect(partialFraction(half, undefined, undefined)).toBeNull();
    // Zero would be an Infinity the clamp below would happily render as 95%.
    expect(partialFraction(half, undefined, 0)).toBeNull();
  });

  it("never draws as empty or as finished", () => {
    // The 40-byte shell is the floor case: 0.002% of a 4GB model, which is
    // visually nothing — and a state drawn as nothing is not drawn.
    const shell = repo({ id: "org/shell", partial: true, size: 40, revisions: 0 });
    expect(partialFraction(shell, undefined, 4 * 1024 ** 3)).toBe(0.02);
    // And the ceiling: an estimate smaller than the bytes already here (a
    // multi-repo download's curated total is for ALL of them) must not read as
    // a finished download, because this repo by definition is not one.
    expect(partialFraction(half, undefined, 1_000)).toBe(0.95);
  });
});

describe("jobFraction", () => {
  it("answers for a card with nothing on this disk yet", () => {
    // The recommended and search-result cards have no folder to measure: the
    // job row is their only account of themselves, and this is what fills them.
    expect(jobFraction({ done: 250, total: 1_000, unit: "bytes" })).toBeCloseTo(0.25);
  });

  it("says nothing for a stage that reports no byte total", () => {
    // A venv build and a weight load have no denominator, and a boundary drawn
    // at an invented one reads as stalled work rather than as live work.
    expect(jobFraction(undefined)).toBeNull();
    expect(jobFraction({ done: 2, total: null })).toBeNull();
    expect(jobFraction({ done: null, total: 100, unit: "bytes" })).toBeNull();
    expect(jobFraction({ done: 1, total: 4, unit: "steps" })).toBeNull();
  });

  it("never draws as empty or as finished", () => {
    expect(jobFraction({ done: 0, total: 1_000, unit: "bytes" })).toBe(0.02);
    expect(jobFraction({ done: 1_000, total: 1_000, unit: "bytes" })).toBe(0.95);
  });
});

describe("partialFraction over a PREALLOCATED part file", () => {
  it("reads the bytes that arrived, not the blocks reserved for them", () => {
    // The reported bug, with the real numbers off the reporter's disk: a 1.61GB
    // whisper download 243MB in. `size` is already the full 1.61GB because the
    // fetcher preallocates, so the old reading drew a card 95% full over a fetch
    // 15% of the way through — and disagreed with the job row beside it.
    const pulling = repo({
      id: "mlx-community/whisper-large-v3-turbo",
      partial: true,
      size: 1_613_977_612,
      fetchedBytes: 243_000_000,
    });
    expect(partialFraction(pulling, undefined, 1_613_977_612)).toBeCloseTo(0.15, 2);
  });

  it("agrees with the live job now that both count durable bytes", () => {
    // Which is what makes taking the larger of the two sound: they are finally
    // answers to one question, so the max is a monotonic guard rather than a
    // choice between two different measurements.
    const pulling = repo({
      id: "org/half",
      partial: true,
      size: 1_000_000,
      fetchedBytes: 250_000,
    });
    const idle = partialFraction(pulling, undefined, 1_000_000);
    const live = partialFraction(
      pulling,
      { done: 250_000, total: 1_000_000, unit: "bytes" },
      1_000_000,
    );
    expect(idle).toBeCloseTo(0.25);
    expect(live).toBeCloseTo(0.25);
  });
});
