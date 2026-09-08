// optionLabel / storedOptionLabel: how a catalog OPTION is said, on the select
// itself and for a value read back off disk. See the functions' own comments
// in PreferencesSection.tsx for the "why" — this locks the one case that used
// to read wrong: a model id wearing the CLI's `[1m]` context qualifier
// (`claude-fable-5-1[1m]`) used to fall all the way to "(not in catalog)"
// because the catalog only lists the BARE id, never the qualifier permutation.
import { describe, expect, it } from "bun:test";
import type { PrefEntry } from "./api";
import { optionLabel, storedOptionLabel } from "./pref-option-label";

const MODEL_ENTRY: PrefEntry = {
  key: "model",
  label: "Model",
  group: "Model & reasoning",
  control: "select",
  options: ["default", "claude-fable-5-1", "fable", "opus", "opus[1m]", "sonnet", "sonnet[1m]", "haiku"],
  optionLabels: { "claude-fable-5-1": "Fable 5.1" },
};

describe("optionLabel", () => {
  it("uses the curated label when the entry has one", () => {
    expect(optionLabel(MODEL_ENTRY, "claude-fable-5-1")).toBe("Fable 5.1");
  });

  it("falls back to the option's own spelling otherwise — sparse by design", () => {
    expect(optionLabel(MODEL_ENTRY, "opus")).toBe("opus");
    expect(optionLabel(MODEL_ENTRY, "opus[1m]")).toBe("opus[1m]");
  });
});

describe("storedOptionLabel", () => {
  it("labels a listed value exactly like the select's own option", () => {
    expect(storedOptionLabel(MODEL_ENTRY, "claude-fable-5-1")).toBe("Fable 5.1");
    expect(storedOptionLabel(MODEL_ENTRY, "opus")).toBe("opus");
  });

  it("resolves the CLI's [1m] context qualifier to the same catalog entry, suffix kept", () => {
    // This is the bug: `claude-fable-5-1[1m]` used to render as
    // "claude-fable-5-1[1m] (not in catalog)" even though it is the same model
    // as the listed `claude-fable-5-1`, just in its 1M-context form.
    expect(storedOptionLabel(MODEL_ENTRY, "claude-fable-5-1[1m]")).toBe("Fable 5.1 [1m]");
  });

  it("a directly-listed qualifier permutation (opus[1m]) is just a listed option", () => {
    expect(storedOptionLabel(MODEL_ENTRY, "opus[1m]")).toBe("opus[1m]");
  });

  it("keeps the bare spelling qualified when the qualified form isn't listed but the base is", () => {
    // The catalog does not enumerate every model's `[1m]` permutation — only a
    // couple (`opus[1m]`, `sonnet[1m]`) are listed outright. A model missing
    // its own listed variant (a fresh entry, or one refresh_catalog hasn't
    // caught up on) still resolves through the base id.
    expect(storedOptionLabel(MODEL_ENTRY, "fable[1m]")).toBe("fable [1m]");
  });

  it("still reports an unrecognised value rather than contradicting the file", () => {
    expect(storedOptionLabel(MODEL_ENTRY, "claude-something-7")).toBe("claude-something-7 (not in catalog)");
    // A qualifier on a base id the catalog does NOT list is still unrecognised —
    // only a LISTED base earns the qualifier's special-case reading.
    expect(storedOptionLabel(MODEL_ENTRY, "claude-something-7[1m]")).toBe(
      "claude-something-7[1m] (not in catalog)",
    );
  });
});
