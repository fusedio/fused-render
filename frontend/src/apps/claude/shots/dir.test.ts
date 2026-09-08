// Where a shot goes and what it is called: the cached `shots_dir` round trip,
// the forward-slash join, the stamp's shape and the MIME→extension map.
import { afterAll, describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

// `bun test` runs every file in ONE process and these globals are shared, so
// whatever this suite stubbed is put back the moment it is done — a leaked
// `fetch` or `URL.createObjectURL` breaks whoever runs next (the idiom
// platform/ui/appdoctor-lib.test.ts sets out).
const G = globalThis as Record<string, unknown>;
const BEFORE = {
  fetch: G.fetch,
  createImageBitmap: G.createImageBitmap,
  Image: G.Image,
  createObjectURL: URL.createObjectURL,
  revokeObjectURL: URL.revokeObjectURL,
};
afterAll(() => {
  G.fetch = BEFORE.fetch;
  G.createImageBitmap = BEFORE.createImageBitmap;
  G.Image = BEFORE.Image;
  Object.assign(URL, {
    createObjectURL: BEFORE.createObjectURL,
    revokeObjectURL: BEFORE.revokeObjectURL,
  });
});

const {
  SHOT_MIME_EXT,
  resetShotsDirForTests,
  shotBase,
  shotDirOf,
  shotFileExt,
  shotJoin,
  shotStamp,
  shotsDir,
  shotsDirSeen,
} = await import("./dir");

/** One `/api/run` answer per call, so the cache's own behaviour is measurable. */
function stubRun(answers: unknown[]): { calls: number } {
  const state = { calls: 0 };
  Object.assign(globalThis, {
    fetch: () => {
      const body = answers[Math.min(state.calls, answers.length - 1)];
      state.calls++;
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      } as unknown as Response);
    },
  });
  return state;
}

describe("shotStamp", () => {
  test("14 digits of UTC, a dash, 8 hex (T:10041)", () => {
    expect(shotStamp()).toMatch(/^\d{14}-[0-9a-f]{8}$/);
  });

  test("two stamps in the same second do not collide", () => {
    expect(shotStamp()).not.toBe(shotStamp());
  });
});

describe("shotJoin", () => {
  test("always '/', whatever the dir ended with (T:9039)", () => {
    expect(shotJoin("/tmp/shots", "a.png")).toBe("/tmp/shots/a.png");
    expect(shotJoin("/tmp/shots/", "a.png")).toBe("/tmp/shots/a.png");
    expect(shotJoin("C:\\shots\\", "a.png")).toBe("C:\\shots/a.png");
  });
});

describe("shotFileExt", () => {
  test("the MIME map (T:11375)", () => {
    expect(SHOT_MIME_EXT["image/jpeg"]).toBe(".jpg");
    for (const [type, ext] of Object.entries(SHOT_MIME_EXT)) {
      expect(shotFileExt(type, "whatever.bin")).toBe(ext);
    }
  });

  test("an unknown type falls back to the name's own extension, lowercased", () => {
    expect(shotFileExt("image/heic", "IMG_4031.HEIC")).toBe(".heic");
    expect(shotFileExt("", "notes.CSV")).toBe(".csv");
  });

  test("a nameless blob of an unknown type is a .png", () => {
    expect(shotFileExt("", "")).toBe(".png");
    expect(shotFileExt(undefined, undefined)).toBe(".png");
    // A leading dot is not an extension.
    expect(shotFileExt("", ".bashrc")).toBe(".png");
  });
});

describe("shotBase / shotDirOf", () => {
  test("either separator, one forward-slash spelling out (T:11651, 11658)", () => {
    expect(shotBase("/a/b/c.png")).toBe("c.png");
    expect(shotBase("C:\\a\\b\\c.png")).toBe("c.png");
    expect(shotDirOf("/a/b/c.png")).toBe("/a/b");
    expect(shotDirOf("C:\\a\\b\\c.png")).toBe("C:/a/b");
    expect(shotDirOf("c.png")).toBe("c.png");
  });
});

describe("shotsDir", () => {
  test("one round trip per agent dir, and readable synchronously after (T:9019)", async () => {
    resetShotsDirForTests();
    const run = stubRun([{ ok: true, result: { dir: "/tmp/shots" } }]);
    expect(shotsDirSeen("/tpl")).toBe("");
    expect(await shotsDir("/tpl")).toBe("/tmp/shots");
    expect(await shotsDir("/tpl")).toBe("/tmp/shots");
    expect(run.calls).toBe(1);
    expect(shotsDirSeen("/tpl")).toBe("/tmp/shots");
  });

  test("a failure is NOT cached: the next attach tries again (T:9034)", async () => {
    resetShotsDirForTests();
    const run = stubRun([
      { ok: true, result: { error: "nope" } },
      { ok: true, result: { dir: "/tmp/shots" } },
    ]);
    await expect(shotsDir("/tpl")).rejects.toThrow("nope");
    expect(shotsDirSeen("/tpl")).toBe("");
    expect(await shotsDir("/tpl")).toBe("/tmp/shots");
    expect(run.calls).toBe(2);
  });
});
