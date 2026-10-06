import { describe, expect, test } from "bun:test";
import { BRANDS, FACE_COLORS, FACE_SHAPES, faceOf, fkey, moodOf, pickableBrands } from "./face";

describe("faceOf", () => {
  test("a brand face keeps any hex colour", () => {
    expect(faceOf({ id: "b1", face: { icon: "linkedin", color: "#123456" } })).toMatchObject({ icon: "linkedin", color: "#123456" });
    expect(faceOf({ id: "b1", face: { icon: "linkedin", color: "#ABCDEF" } }).color).toBe("#ABCDEF");
  });
  test("a brand face without a usable colour takes the brand's own", () => {
    expect(faceOf({ id: "b1", face: { icon: "youtube" } }).color).toBe(BRANDS.youtube.color);
    expect(faceOf({ id: "b1", face: { icon: "youtube", color: "red" } }).color).toBe(BRANDS.youtube.color);
    expect(faceOf({ id: "b1", face: { icon: "youtube", color: "#12345" } }).color).toBe(BRANDS.youtube.color);
  });
  test("a brand face also takes a palette colour", () => {
    expect(faceOf({ id: "b1", face: { icon: "gmail", color: FACE_COLORS[3] } }).color).toBe(FACE_COLORS[3]);
  });
  test("a blob rejects a non-palette colour and falls back to the hashed palette one", () => {
    const f = faceOf({ id: "b1", face: { shape: "cloud", color: "#123456" } });
    expect(f.icon).toBe("");
    expect(f.shape).toBe("cloud");
    expect(FACE_COLORS).toContain(f.color);
    expect(f.color).not.toBe("#123456");
    expect(f.color).toBe(faceOf({ id: "b1" }).color);
  });
  test("an unknown icon is dropped (blob rules apply)", () => {
    const f = faceOf({ id: "b1", face: { icon: "nope", color: "#123456" } });
    expect(f.icon).toBe("");
    expect(FACE_COLORS).toContain(f.color);
    expect(faceOf({ id: "b1", face: { icon: "constructor" } }).icon).toBe("");
  });
  test("a brand face still resolves a shape (the picker falls back to it)", () => {
    expect(Object.keys(FACE_SHAPES)).toContain(faceOf({ id: "b1", face: { icon: "x" } }).shape);
  });
});

describe("fkey", () => {
  test("changes with the icon", () => {
    const base = { id: "b1", status: "idle" as const, browser: { running: true } };
    const blob = fkey({ ...base, face: { shape: "cloud", color: FACE_COLORS[2] } });
    const brand = fkey({ ...base, face: { shape: "cloud", color: FACE_COLORS[2], icon: "github" } });
    const other = fkey({ ...base, face: { shape: "cloud", color: FACE_COLORS[2], icon: "slack" } });
    expect(blob).not.toBe(brand);
    expect(brand).not.toBe(other);
    expect(brand.endsWith(":github")).toBe(true);
  });
});

describe("BRANDS", () => {
  test("every brand has a name, a hex colour and glyph markup", () => {
    for (const [k, b] of Object.entries(BRANDS)) {
      expect(b.name.length).toBeGreaterThan(0);
      expect(b.color).toMatch(/^#[0-9a-f]{6}$/i);
      const g = b.glyph(b.color);
      expect(g.startsWith("<")).toBe(true);
      expect(g.includes("${")).toBe(false);
      expect(k).toMatch(/^[a-z]+$/);
    }
  });
  test("glyphs that cut out shapes use the disc colour", () => {
    expect(BRANDS.youtube.glyph("#abcdef")).toContain('fill="#abcdef"');
  });
});

describe("moodOf", () => {
  test("a fresh bot (never had a browser) is awake while it greets", () => {
    expect(moodOf({ id: "b1", status: "idle", browser: { running: false } as never })).toBe("idle");
    expect(moodOf({ id: "b1", status: "idle" })).toBe("idle");
  });
  test("a bot whose browser was up and went down is asleep", () => {
    expect(moodOf({ id: "b1", status: "idle", shot: "/api/bots/b1/shot", browser: { running: false } as never })).toBe("paused");
  });
  test("an idle bot with its browser up is awake", () => {
    expect(moodOf({ id: "b1", status: "idle", shot: "/api/bots/b1/shot", browser: { running: true } as never })).toBe("idle");
  });
  test("other statuses map straight through", () => {
    expect(moodOf({ id: "b1", status: "running" })).toBe("running");
    expect(moodOf({ id: "b1", status: "error", shot: "/x" })).toBe("error");
  });
});

describe("the locked Claude mark", () => {
  test("renders like any brand but the picker does not offer it", () => {
    expect(BRANDS.claude.locked).toBe(true);
    expect(faceOf({ id: "s1", face: { icon: "claude" } })).toEqual({ shape: expect.any(String), color: "#262624", icon: "claude" });
    expect(pickableBrands()).not.toContain("claude");
    // the stored colour never repaints the disc: a Super Bot made when the disc was orange still shows the mark
    expect(faceOf({ id: "s1", face: { icon: "claude", color: "#d97757" } }).color).toBe(BRANDS.claude.color);
    expect(faceOf({ id: "s1", face: { icon: "claude", color: "#2f7ae5" } }).color).toBe(BRANDS.claude.color);
    expect(pickableBrands()).toContain("youtube");
    expect(pickableBrands().length).toBe(Object.keys(BRANDS).length - 1);
  });
});
