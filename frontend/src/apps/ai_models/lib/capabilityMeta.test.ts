import { describe, expect, it } from "bun:test";

import { capabilityMeta, PARTS_ICON } from "./capabilityMeta";

describe("capabilityMeta", () => {
  it("gives the standard capability name plus its own plain-language blurb noun for each of the five known capabilities", () => {
    // Item 11 (fix round 3): `plain` is now the same standard capability
    // name `engines.ts`'s `capabilityLabel` gives every other reader of that
    // table (nav, Engines tab) — not a separate friendly register — per the
    // user's "use the standard capability names like text generation etc".
    expect(capabilityMeta("text-generation").plain).toBe("Text generation");
    expect(capabilityMeta("text-generation").searchNoun).toBe("chat models");
    expect(capabilityMeta("text-to-image").plain).toBe("Image generation");
    expect(capabilityMeta("text-to-image").searchNoun).toBe("image models");
    expect(capabilityMeta("automatic-speech-recognition").plain).toBe("Speech to text");
    expect(capabilityMeta("automatic-speech-recognition").searchNoun).toBe("transcription models");
    // Amendment: embeddings keeps "Search & similarity" (user asked to keep
    // this one name) while every other capability uses its standard name.
    expect(capabilityMeta("embeddings").plain).toBe("Search & similarity");
    expect(capabilityMeta("embeddings").searchNoun).toBe("embedding models");
    expect(capabilityMeta("text-to-video").plain).toBe("Video generation");
    expect(capabilityMeta("text-to-video").searchNoun).toBe("video models");
  });

  it("gives every known capability a non-empty blurb and an icon", () => {
    for (const key of [
      "text-generation",
      "text-to-image",
      "automatic-speech-recognition",
      "embeddings",
      "text-to-video",
    ]) {
      const meta = capabilityMeta(key);
      expect(meta.blurb.length).toBeGreaterThan(0);
      expect(meta.icon).toContain("<svg");
    }
  });

  it("falls back to the terse Hub label for an unrecognised capability instead of throwing", () => {
    const meta = capabilityMeta("some-future-capability");
    expect(meta.plain).toBe("some-future-capability");
    expect(meta.blurb).toBe("");
    expect(meta.searchNoun).toBe("models");
    expect(meta.icon).toContain("<svg");
  });

  it("agrees with engines.ts's own capability label for a capability it recognises", () => {
    // Item 11 retired the divergence this test used to assert: `plain` is
    // now built FROM `capabilityLabel`, deliberately, so the two agree.
    const meta = capabilityMeta("text-generation");
    expect(meta.plain).toBe("Text generation");
  });

  it("exports a distinct icon for the non-capability Engine files bucket", () => {
    expect(PARTS_ICON).toContain("<svg");
    expect(PARTS_ICON).not.toBe(capabilityMeta("text-generation").icon);
  });
});
