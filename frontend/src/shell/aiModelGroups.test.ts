// The Local tab's bucketing, driven directly. The page renders these groups;
// what can actually be WRONG is which bucket a repo lands in, and the wrong
// answer is invisible on a screenshot — a mystery repo and a model this machine
// cannot load look identical once they are both wearing a dead-end tag.
import { describe, expect, it } from "bun:test";
import type { AiModelRepo } from "@platform/lib/api";
import { UNRECOGNISED, groupRepos, loadRefusal, noEngineReason } from "@shell/aiModelGroups";

function repo(over: Partial<AiModelRepo> & { id: string }): AiModelRepo {
  return {
    dir: "models--" + over.id.replace("/", "--"),
    kind: "model",
    path: "/cache/" + over.id,
    size: 1000,
    files: 3,
    mtime: null,
    lastUsed: null,
    added: null,
    task: null,
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
    ...over,
  };
}

function engine(over: Partial<NonNullable<AiModelRepo["engine"]>> = {}) {
  return {
    code: "mlx-lm",
    label: "MLX LM (Apple Silicon)",
    shortLabel: "MLX LM",
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
  // heading saying nothing here recognises it at all.
  it("does not blame the format for a repo it cannot identify at all", () => {
    const why = loadRefusal(WESPEAKER) ?? "";
    expect(why).toContain("Nothing here recognises");
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
    expect(noEngineReason(WESPEAKER)).toContain("Nothing here recognises");
  });

  // Not a paraphrase of the hover: the same string, so the two cannot drift.
  it("gives the Load button the hover's sentence, plus what loading adds", () => {
    expect(loadRefusal(QWEN_NO_ENGINE)).toBe(noEngineReason(QWEN_NO_ENGINE));
    expect(loadRefusal(WESPEAKER)?.startsWith(noEngineReason(WESPEAKER))).toBe(true);
    expect(loadRefusal(WESPEAKER)).toContain("nothing to load it as");
  });

  it("says what the Unrecognised heading over the card says", () => {
    const note = groupRepos([WESPEAKER]).models.groups[0].note ?? "";
    for (const phrase of ["not a model this app can load", "part of one"]) {
      expect(note).toContain(phrase);
      expect(noEngineReason(WESPEAKER)).toContain(phrase);
    }
  });

  // A repo with a capability keeps its format problem, which is a real
  // diagnosis with a real fix: another engine, or another copy of the weights.
  // Flattening both cards onto one sentence would lose it.
  it("does not tell a Qwen checkpoint that nothing recognises it", () => {
    expect(noEngineReason(QWEN_NO_ENGINE)).not.toContain("Nothing here recognises");
  });
});
