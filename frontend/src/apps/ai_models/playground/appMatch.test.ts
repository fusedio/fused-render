import { describe, expect, test } from "bun:test";
import { matchPlaygroundApps, type ShowcaseAppMeta } from "./appMatch";

const chat: ShowcaseAppMeta = {
  slug: "local-chat",
  name: "Local Chat",
  ai_capabilities: ["text-generation"],
  ai_model_param: "chatModel",
};
const image: ShowcaseAppMeta = {
  slug: "local-image",
  ai_capabilities: ["text-to-image"],
  ai_model_param: "imageModel",
};
const whisperOnly: ShowcaseAppMeta = {
  slug: "whisper-notes",
  ai_models: ["whisper-large-v3.gguf"],
  ai_model_param: "asrModel",
};
const plain: ShowcaseAppMeta = { slug: "disk-usage" };

describe("matchPlaygroundApps", () => {
  test("capability match lists the app, unbadged", () => {
    const offers = matchPlaygroundApps([chat, image, plain], "text-generation", "any/model");
    expect(offers).toEqual([{ app: chat, recommended: false }]);
  });

  test("apps without AI metadata are never offered", () => {
    expect(matchPlaygroundApps([plain], "text-generation", "m")).toEqual([]);
  });

  test("ai_models hit is offered as recommended", () => {
    const offers = matchPlaygroundApps(
      [whisperOnly],
      "automatic-speech-recognition",
      "whisper-large-v3.gguf",
    );
    expect(offers).toEqual([{ app: whisperOnly, recommended: true }]);
  });

  test("ai_models-only app hides when the model misses, even on its capability", () => {
    // The field says "specific models only" — a capability-wide listing would
    // un-say it.
    expect(
      matchPlaygroundApps([whisperOnly], "automatic-speech-recognition", "other.gguf"),
    ).toEqual([]);
  });

  test("both fields: model hit wins as recommended, miss falls back to capability", () => {
    const dual: ShowcaseAppMeta = {
      slug: "dual",
      ai_capabilities: ["text-generation"],
      ai_models: ["special/model"],
      ai_model_param: "chatModel",
    };
    expect(matchPlaygroundApps([dual], "text-generation", "special/model")).toEqual([
      { app: dual, recommended: true },
    ]);
    expect(matchPlaygroundApps([dual], "text-generation", "other/model")).toEqual([
      { app: dual, recommended: false },
    ]);
  });

  test("recommended offers come first, catalog order kept within each half", () => {
    const a: ShowcaseAppMeta = { slug: "a", ai_capabilities: ["c"], ai_model_param: "p" };
    const b: ShowcaseAppMeta = { slug: "b", ai_models: ["m"], ai_model_param: "p" };
    const c: ShowcaseAppMeta = { slug: "c", ai_capabilities: ["c"], ai_model_param: "p" };
    expect(matchPlaygroundApps([a, b, c], "c", "m").map((o) => o.app.slug)).toEqual([
      "b",
      "a",
      "c",
    ]);
  });

  test("no usable param, or the shell-reserved `model`, skips the app", () => {
    const noParam: ShowcaseAppMeta = { slug: "x", ai_capabilities: ["c"] };
    const reserved: ShowcaseAppMeta = { slug: "y", ai_capabilities: ["c"], ai_model_param: "model" };
    const blank: ShowcaseAppMeta = { slug: "z", ai_capabilities: ["c"], ai_model_param: "  " };
    expect(matchPlaygroundApps([noParam, reserved, blank], "c", "m")).toEqual([]);
  });
});
