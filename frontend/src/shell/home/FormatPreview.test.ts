import { describe, expect, test } from "bun:test";
import { previewKind } from "./FormatPreview";
import { SOURCES, type WidgetSource } from "./layout";

describe("previewKind", () => {
  test("cards pick a glyph by source", () => {
    expect(previewKind("apps", "cards")).toBe("cards-apps");
    expect(previewKind("playground", "cards")).toBe("cards-playground");
    expect(previewKind("sessions", "cards")).toBe("cards-rows");
    expect(previewKind("recents", "cards")).toBe("cards-rows");
  });
  test("count varies by source", () => {
    expect(previewKind("tasks", "count")).toBe("count-tasks");
    expect(previewKind("bots", "count")).toBe("count-bots");
    expect(previewKind("index", "count")).toBe("count-index");
  });
  test("an app's live format draws the browser frame", () => {
    expect(previewKind("app", "live")).toBe("app-live");
  });
  test("every allowed (source, format) pair has a kind", () => {
    for (const s of Object.keys(SOURCES) as WidgetSource[]) {
      for (const f of SOURCES[s].formats) expect(previewKind(s, f)).toBeTruthy();
    }
  });
});
