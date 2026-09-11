import { describe, expect, it } from "bun:test";

import { capabilityMeta, PARTS_ICON } from "./capabilityMeta";

describe("capabilityMeta", () => {
  it("gives the plain-language copy for each of the five known capabilities", () => {
    expect(capabilityMeta("text-generation").plain).toBe("Chat & writing");
    expect(capabilityMeta("text-generation").searchNoun).toBe("chat models");
    expect(capabilityMeta("text-to-image").plain).toBe("Make images");
    expect(capabilityMeta("text-to-image").searchNoun).toBe("image models");
    expect(capabilityMeta("automatic-speech-recognition").plain).toBe("Transcribe audio");
    expect(capabilityMeta("automatic-speech-recognition").searchNoun).toBe("transcription models");
    expect(capabilityMeta("embeddings").plain).toBe("Search & similarity");
    expect(capabilityMeta("embeddings").searchNoun).toBe("embedding models");
    expect(capabilityMeta("text-to-video").plain).toBe("Make video");
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

  it("falls back to the Hub's own label text for a capability engines.ts does recognise", () => {
    const meta = capabilityMeta("text-generation");
    // Sanity: plain-language copy differs from the terse engines.ts label,
    // proving this file is not just re-exporting that vocabulary.
    expect(meta.plain).not.toBe("Text generation");
  });

  it("exports a distinct icon for the non-capability Engine files bucket", () => {
    expect(PARTS_ICON).toContain("<svg");
    expect(PARTS_ICON).not.toBe(capabilityMeta("text-generation").icon);
  });
});
