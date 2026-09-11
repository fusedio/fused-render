// The attachment rules: what a thing IS, what it is called, what rides the wire,
// what the receipt says, which directories the send has to grant — and the one
// remaining refusal (T:11366-11750, 7067-7120, 16519, 10855, 10903).
import { afterAll, beforeEach, describe, expect, test } from "bun:test";
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
  attachFile,
  attachPaths,
  dragHasAttachment,
  failLabel,
  filesFromPaste,
  formatLabel,
  isImage,
  kindFor,
  pathsFromDrop,
  probePruned,
  prunedLabel,
  readDirs,
  receiptFor,
  receiptsFromWire,
  revoke,
  saveExt,
  shotAlt,
  shotNoun,
  sizeLabel,
  toWire,
} = await import("./attach");
const { resetShotsDirForTests } = await import("./dir");
const { resetWebpLatchForTests, setCanvasFactory } = await import("./encode");
const { SHOT_PATH_TYPE } = await import("./types");
type Attachment = import("./types").Attachment;

// ── the fakes ───────────────────────────────────────────────────────────────

let uploads: { path: string; bytes: number }[] = [];
let runs: Record<string, unknown>[] = [];
let convert: Record<string, unknown> | null = null;
let heads: string[] = [];
let headOk = true;
let objectUrls = 0;
let revoked: string[] = [];

/** One `fetch` for every route this module reaches: the upload, `/api/run` and
 *  the receipt's HEAD probe. */
function installFetch(): void {
  Object.assign(globalThis, {
    fetch: (url: string | URL | Request, init?: RequestInit) => {
      const href = String(url);
      if (href === "/api/fs/upload") {
        const form = init?.body as FormData;
        const path = String(form.get("path"));
        const file = form.get("file") as Blob;
        uploads.push({ path, bytes: file.size });
        return json({ path, name: path, is_dir: false, size: file.size, mtime: 0 });
      }
      if (href === "/api/run") {
        const body = JSON.parse(String(init?.body)) as { params: Record<string, unknown> };
        runs.push(body.params);
        if (body.params.action === "shots_dir") return json({ ok: true, result: { dir: "/shots" } });
        if (body.params.action === "image_to_png") {
          return json({ ok: true, result: convert ?? { error: "no" } });
        }
        return json({ ok: true, result: {} });
      }
      if (init?.method === "HEAD") {
        heads.push(href);
        return Promise.resolve({ ok: headOk, status: headOk ? 200 : 404 } as Response);
      }
      throw new Error("unexpected fetch: " + href);
    },
  });
}

function json(body: unknown): Promise<Response> {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
}

/** A canvas that always answers a small PNG: the shrink path's ladder is
 *  encode.test.ts's subject, so here it only has to SUCCEED. */
function installCanvas(): void {
  setCanvasFactory(
    () =>
      ({
        width: 0,
        height: 0,
        getContext: () => ({ drawImage: () => {} }),
        toBlob: (cb: (b: Blob | null) => void) =>
          cb(new Blob([new Uint8Array(1024)], { type: "image/png" })),
      }) as unknown as HTMLCanvasElement,
  );
}

function installUrls(): void {
  Object.assign(URL, {
    createObjectURL: () => "blob:fake/" + ++objectUrls,
    revokeObjectURL: (u: string) => revoked.push(u),
  });
}

/** A decodable picture, whatever the bytes are. */
function installBitmap(width = 4000, height = 3000): void {
  Object.assign(globalThis, {
    createImageBitmap: () => Promise.resolve({ width, height, close: () => {} }),
  });
}

function undecodable(): void {
  Object.assign(globalThis, {
    createImageBitmap: () => Promise.reject(new Error("format")),
    Image: undefined,
  });
}

function fileOf(name: string, type: string, size: number): File {
  // `size` is what the branch reads and a real allocation of 4 MiB per test is
  // waste, so the length is faked over one byte of content.
  const f = new File([new Uint8Array(1)], name, { type });
  Object.defineProperty(f, "size", { value: size });
  return f;
}

beforeEach(() => {
  uploads = [];
  runs = [];
  heads = [];
  revoked = [];
  convert = null;
  headOk = true;
  objectUrls = 0;
  resetShotsDirForTests();
  resetWebpLatchForTests();
  installFetch();
  installCanvas();
  installUrls();
  installBitmap();
});

// ── kinds ───────────────────────────────────────────────────────────────────

describe("kindFor / isImage", () => {
  test("type first, extension as the fallback for a typeless drag (T:11392)", () => {
    expect(kindFor({ name: "x.bin", type: "image/png" })).toBe("image");
    expect(kindFor({ name: "shot.PNG", type: "" })).toBe("image");
    expect(kindFor({ name: "IMG.jpeg", type: "" })).toBe("image");
    expect(kindFor({ name: "notes.csv", type: "text/csv" })).toBe("file");
    expect(kindFor({ name: "Makefile", type: "" })).toBe("file");
    expect(isImage(null)).toBe(false);
  });

  // Bugbot, PR #1064. Safari drops the MIME on a drag, and HEIC is the one
  // format on this machine that NEEDS the server's converter — classified a
  // `file` it never reached the decode that asks for it.
  test("a typeless HEIC/HEIF/TIFF is a picture, not a file (Bugbot, PR #1064)", () => {
    expect(kindFor({ name: "IMG_4031.HEIC", type: "" })).toBe("image");
    expect(kindFor({ name: "IMG_4031.heic", type: "" })).toBe("image");
    expect(kindFor({ name: "scan.heif", type: "" })).toBe("image");
    expect(kindFor({ name: "scan.tif", type: "" })).toBe("image");
    expect(kindFor({ name: "scan.TIFF", type: "" })).toBe("image");
    // …and the neighbours a loose regex would swallow are still files.
    expect(kindFor({ name: "notes.heicx", type: "" })).toBe("file");
    expect(kindFor({ name: "a.tiffany", type: "" })).toBe("file");
  });
});

describe("saveExt", () => {
  test("a picture's name follows its bytes; a file keeps its own, or none (T:11403)", () => {
    expect(saveExt({ name: "", type: "image/png" })).toBe(".png");
    expect(saveExt({ name: "a.jpg", type: "image/jpeg" })).toBe(".jpg");
    expect(saveExt({ name: "dump.PARQUET", type: "" })).toBe(".parquet");
    expect(saveExt({ name: "Makefile", type: "" })).toBe("");
  });
});

describe("formatLabel", () => {
  test("the word a person says (T:11416)", () => {
    expect(formatLabel("IMG_4031.HEIC")).toBe("HEIC");
    expect(formatLabel("a.jpg")).toBe("JPEG");
    expect(formatLabel("a.tif")).toBe("TIFF");
    expect(formatLabel("a.zip")).toBe("ZIP");
    expect(formatLabel("Makefile")).toBe("that format");
  });
});

describe("sizeLabel", () => {
  test("undefined is an answer, not a zero (T:7092)", () => {
    expect(sizeLabel(undefined)).toBe("");
    expect(sizeLabel(512)).toBe("512 B");
    expect(sizeLabel(2048)).toBe("2 KB");
    expect(sizeLabel(3 * 1024 * 1024)).toBe("3.0 MB");
  });
});

// ── the wire ────────────────────────────────────────────────────────────────

describe("toWire", () => {
  test("a pane shot carries kind and view, and never a name (T:16519)", () => {
    const att: Attachment = { id: "1", kind: "pane", view: "/shots/a-view.webp", name: "nope", thumb: "blob:x", seat: "pane" };
    expect(toWire([att])).toEqual([{ kind: "pane", view: "/shots/a-view.webp" }]);
  });

  test("an image carries name, and size only when it has one (T:16532)", () => {
    expect(toWire([{ id: "1", kind: "image", view: "/shots/a.png", name: "a.png", thumb: "blob:x" }])).toEqual([
      { kind: "image", view: "/shots/a.png", name: "a.png" },
    ]);
    expect(toWire([{ id: "1", kind: "image", view: "/shots/a.heic", name: "a.heic", size: 99 }])).toEqual([
      { kind: "image", view: "/shots/a.heic", name: "a.heic", size: 99 },
    ]);
  });

  test("a file carries name and size; a brought-in path has no size (T:16539)", () => {
    expect(toWire([{ id: "1", kind: "file", view: "/w/a.csv", name: "a.csv", brought: true }])).toEqual([
      { kind: "file", view: "/w/a.csv", name: "a.csv" },
    ]);
  });

  test("viewNote and why both ride, and a refusal keeps view null (T:16526)", () => {
    expect(
      toWire([{ id: "1", kind: "image", view: null, name: "a.png", viewNote: "not attached: …", why: "could not be saved" }]),
    ).toEqual([{ kind: "image", view: null, viewNote: "not attached: …", why: "could not be saved", name: "a.png" }]);
  });

  test("the overview is unshifted FIRST — it is the picture to read (T:16545)", () => {
    const wire = toWire([
      { id: "1", kind: "image", view: "/shots/a.png", name: "a.png" },
      { id: "2", kind: "overview", view: "/shots/o-overview.webp", viewNote: "…" },
    ]);
    expect(wire.map((w) => w.kind)).toEqual(["overview", "image"]);
    expect(wire[0]).toEqual({ kind: "overview", view: "/shots/o-overview.webp", viewNote: "…" });
  });
});

// ── receipts ────────────────────────────────────────────────────────────────

describe("receiptFor / failLabel", () => {
  test("the sent labels, verbatim (T:10849)", () => {
    const r = (att: Partial<Attachment>) =>
      receiptFor({ id: "1", kind: "pane", view: "/shots/a", ...att } as Attachment).label;
    expect(r({})).toBe("screenshot attached");
    expect(r({ kind: "overview" })).toBe("annotated overview attached");
    expect(r({ kind: "image", name: "a.png" })).toBe("image attached: a.png");
    expect(r({ kind: "image" })).toBe("image attached");
    expect(r({ kind: "file", name: "a.csv" })).toBe("file attached: a.csv");
  });

  test("a refusal names its cause ON the row (T:7112)", () => {
    expect(failLabel({ kind: "image", why: "could not be saved" })).toBe("no image — could not be saved");
    expect(failLabel({ kind: "file" })).toBe("no file");
    expect(failLabel({ kind: "overview" })).toBe("no overview screenshot");
    expect(failLabel({ kind: "pane" })).toBe("no pane screenshot");
    expect(receiptFor({ id: "1", kind: "pane", view: null, why: "x" } as Attachment).label).toBe(
      "no pane screenshot — x",
    );
  });

  test("a FILE never gets a src — an <img> at a .csv is a broken glyph (T:10820)", () => {
    const r = receiptFor({ id: "1", kind: "file", view: "/w/a.csv", thumb: "blob:x" } as Attachment);
    expect(r.src).toBeUndefined();
    expect(r.thumb).toBeUndefined();
  });
});

describe("receiptsFromWire", () => {
  test("a drawable picture gets src; a bare one gets a probe (T:10903)", () => {
    const [pane, file, undec] = receiptsFromWire([
      { kind: "pane", view: "/shots/a-view.webp" },
      { kind: "file", view: "/w/a.csv", name: "a.csv", size: 10 },
      { kind: "image", view: "/shots/a.heic", name: "a.heic", size: 99 },
    ]);
    expect(pane.src).toBe("/api/fs/raw?path=%2Fshots%2Fa-view.webp");
    expect(pane.probe).toBeUndefined();
    expect(file.probe).toBe("/api/fs/raw?path=%2Fw%2Fa.csv");
    expect(file.src).toBeUndefined();
    // An image carrying a size has no pixels this browser can draw either.
    expect(undec.probe).toBe("/api/fs/raw?path=%2Fshots%2Fa.heic");
    expect(undec.src).toBeUndefined();
  });

  test("a refused entry probes nothing", () => {
    const [r] = receiptsFromWire([{ kind: "image", view: null, why: "could not be saved" }]);
    expect(r.probe).toBeUndefined();
    expect(r.label).toBe("no image — could not be saved");
  });
});

describe("probePruned", () => {
  test("a 404 on the HEAD is the pruner (T:10875)", async () => {
    const [r] = receiptsFromWire([{ kind: "file", view: "/w/a.csv", size: 1 }]);
    headOk = false;
    expect(await probePruned(r)).toBe(true);
    expect(heads).toEqual(["/api/fs/raw?path=%2Fw%2Fa.csv"]);
    headOk = true;
    expect(await probePruned(r)).toBe(false);
  });

  test("no probe means no request", async () => {
    expect(await probePruned({ kind: "pane", label: "x", view: "/a" })).toBe(false);
    expect(heads).toEqual([]);
  });

  test("the words (T:10883)", () => {
    expect(prunedLabel("image")).toBe(" — image pruned");
    expect(prunedLabel("file")).toBe(" — file pruned");
  });
});

// ── nouns ───────────────────────────────────────────────────────────────────

describe("shotNoun / shotAlt", () => {
  test("one source for the chip, the viewer and the alt text (T:7067)", () => {
    expect(shotNoun({ kind: "pane" }, "app")).toBe("app screenshot");
    expect(shotNoun({ kind: "overview" })).toBe("annotated overview");
    expect(shotNoun({ kind: "image" })).toBe("pasted image");
    expect(shotNoun({ kind: "file" })).toBe("attached file");
    expect(shotNoun({ kind: "file", name: "a.csv" })).toBe("a.csv");
    expect(shotAlt({ kind: "pane" }, "app")).toBe("screenshot of the whole app pane");
    expect(shotAlt({ kind: "overview" }, "app")).toBe("overview of the app pane with a badge at each comment");
    expect(shotAlt({ kind: "image", name: "a.png" })).toBe("image attached to this message: a.png");
  });
});

// ── real-path drags ─────────────────────────────────────────────────────────

describe("attachPaths", () => {
  test("no upload, no size, an image still gets a rawUrl thumb (T:11680)", () => {
    const out = attachPaths("/tpl", ["/w/a.png", "/w/b.csv"]);
    expect(uploads).toEqual([]);
    expect(out[0]).toMatchObject({
      kind: "image",
      view: "/w/a.png",
      name: "a.png",
      brought: true,
      thumb: "/api/fs/raw?path=%2Fw%2Fa.png",
    });
    expect(out[0].size).toBeUndefined();
    expect(out[1]).toMatchObject({ kind: "file", view: "/w/b.csv", brought: true });
    expect(out[1].thumb).toBeUndefined();
  });
});

describe("readDirs", () => {
  test("deduped, and the shots dir is excluded — it is already granted (T:11698)", () => {
    const atts = [
      { view: "/shots/a-view.webp" },
      { view: "/w/one/a.csv" },
      { view: "/w/one/b.csv" },
      { view: "/w/two/c.csv" },
      { view: null },
    ];
    expect(readDirs(atts, "/shots")).toEqual(["/w/one", "/w/two"]);
    // With no dir known yet, one redundant Read rule is the worst it costs.
    expect(readDirs(atts, "")).toEqual(["/shots", "/w/one", "/w/two"]);
  });
});

// ── DataTransfer reading ────────────────────────────────────────────────────

describe("dragHasAttachment / pathsFromDrop / filesFromPaste", () => {
  const dt = (types: string[], data?: string, throws = false): DataTransfer =>
    ({
      types,
      getData: () => {
        if (throws) throw new Error("no");
        return data ?? "";
      },
      files: [],
    }) as unknown as DataTransfer;

  test("either payload answers yes (T:11742)", () => {
    expect(dragHasAttachment(dt(["Files"]))).toBe(true);
    expect(dragHasAttachment(dt([SHOT_PATH_TYPE]))).toBe(true);
    expect(dragHasAttachment(dt(["text/plain"]))).toBe(false);
    expect(dragHasAttachment(null)).toBe(false);
  });

  test("newline-joined, trimmed, blanks dropped (T:11665)", () => {
    expect(pathsFromDrop(dt([SHOT_PATH_TYPE], "/w/a.csv\n  /w/b.csv  \n\n"))).toEqual([
      "/w/a.csv",
      "/w/b.csv",
    ]);
    expect(pathsFromDrop(dt(["Files"], "/w/a.csv"))).toEqual([]);
    expect(pathsFromDrop(dt([SHOT_PATH_TYPE], "", true))).toEqual([]);
  });

  test("a paste of only words falls through untouched (T:11719)", () => {
    expect(filesFromPaste({ clipboardData: dt(["text/plain"]) })).toEqual([]);
    expect(filesFromPaste({})).toEqual([]);
  });
});

// ── revoke ──────────────────────────────────────────────────────────────────

describe("revoke", () => {
  test("idempotent, and only ever an object URL (T:10645)", () => {
    const att: Attachment = { id: "1", kind: "pane", view: "/a", thumb: "blob:one" };
    revoke(att);
    revoke(att);
    revoke(null);
    expect(revoked).toEqual(["blob:one"]);
    expect(att.thumb).toBe("");
    // A rawUrl thumb is not an object URL: the same call is safe and does nothing.
    const brought: Attachment = { id: "2", kind: "image", view: "/w/a.png", thumb: "/api/fs/raw?path=x" };
    revoke(brought);
    expect(revoked).toEqual(["blob:one"]);
  });
});

// ── the one refusal, and NO CAP OF ANY KIND (P2-6) ─────────────────────────

describe("attachFile", () => {
  test("a small picture goes up whole, with a thumb and no size (T:11597)", async () => {
    const att = await attachFile("/tpl", fileOf("a.png", "image/png", 1000));
    expect(uploads.length).toBe(1);
    expect(uploads[0].path).toMatch(/^\/shots\/\d{14}-[0-9a-f]{8}\.png$/);
    expect(att.thumb).toBe("blob:fake/1");
    expect(att.size).toBeUndefined();
    expect(att.viewNote).toBeUndefined();
  });

  test("A HUGE PICTURE GOES UP UNTOUCHED — no cap, no downscale (P2-6)", async () => {
    // T re-encoded anything over 4 MiB before the upload (T:11355) on the
    // argument that the number is about the agent's read of the pixels. Akshil,
    // 2026-09-09: "any and every file, size and type must not matter" — so the
    // bytes someone attached are the bytes that go up, whatever the size, and
    // nothing claims to have changed them.
    Object.assign(globalThis, {
      createImageBitmap: () => Promise.resolve({ width: 8000, height: 6000, close: () => {} }),
    });
    const att = await attachFile("/tpl", fileOf("huge.png", "image/png", 80 * 1024 * 1024));
    expect(uploads.length).toBe(1);
    // The FILE'S own extension, because the file itself is what was copied.
    expect(uploads[0].path).toMatch(/\.png$/);
    expect(uploads[0].bytes).toBe(1);
    expect(att.view).toBe(uploads[0].path);
    expect(att.viewNote).toBeUndefined();
    expect(att.why).toBeUndefined();
    // Still drawable, so still a thumb and no size (T:11597).
    expect(att.thumb).toBe("blob:fake/1");
    expect(att.size).toBeUndefined();
  });

  test("A 60 MB ZIP is attached like anything else — no type gate either (P2-6)", async () => {
    const att = await attachFile("/tpl", fileOf("dump.zip", "application/zip", 60 * 1024 * 1024));
    expect(att.kind).toBe("file");
    expect(att.view).toBe(uploads[0].path);
    expect(uploads[0].path).toMatch(/\.zip$/);
    expect(att.why).toBeUndefined();
    expect(att.viewNote).toBeUndefined();
    // No pixels to look at, so the chip gets a size instead of a thumb.
    expect(att.size).toBe(60 * 1024 * 1024);
    expect(att.thumb).toBeUndefined();
  });

  test("THE DECODE IS STILL RELEASED — it happened only to learn `pic`", async () => {
    // `shotPixels` hands back a full-size decode (a bitmap, or an `<img>` over an
    // object URL) and the only question asked of it is whether a 22px view would
    // show anything. Nothing is drawn from it any more, so there is nothing left
    // that can throw — but the handle still has to be let go of, or a full-size
    // decode is pinned for the life of the page.
    let closed = 0;
    Object.assign(globalThis, {
      createImageBitmap: () =>
        Promise.resolve({
          width: 4000,
          height: 3000,
          close: () => {
            closed++;
          },
        }),
    });
    const att = await attachFile("/tpl", fileOf("a.png", "image/png", 9 * 1024 * 1024));
    expect(closed).toBe(1);
    expect(att.view).toBe(uploads[0].path);
  });

  test("and the <img> road's object URL is revoked the same way", async () => {
    // The other half of `shotPixels`: no `createImageBitmap`, so the decode is an
    // `<img>` over an object URL and `free()` is a `revokeObjectURL`.
    Object.assign(globalThis, {
      createImageBitmap: undefined,
      Image: class {
        naturalWidth = 4000;
        naturalHeight = 3000;
        src = "";
        decode(): Promise<void> {
          return Promise.resolve();
        }
      },
    });
    const att = await attachFile("/tpl", fileOf("a.png", "image/png", 9 * 1024 * 1024));
    expect(revoked).toEqual(["blob:fake/1"]);
    expect(att.view).toBe(uploads[0].path);
    expect(uploads[0].bytes).toBe(1);
  });

  test("an upload that FAILS is still the one refusal — a chip, never a throw", async () => {
    Object.assign(globalThis, { fetch: () => Promise.reject(new Error("readonly")) });
    const att = await attachFile("/tpl", fileOf("a.png", "image/png", 1000));
    expect(att.view).toBeNull();
    expect(att.why).toBe("could not be saved");
    expect(att.viewNote).toBe("not attached: it could not be saved (readonly)");
  });

  test("an undecodable picture is converted server-side, and becomes drawable (T:11574)", async () => {
    undecodable();
    convert = { path: "/shots/a.png", width: 800, height: 600, source_w: 4032, source_h: 3024 };
    const att = await attachFile("/tpl", fileOf("IMG.HEIC", "image/heic", 5000));
    expect(runs.some((r) => r.action === "image_to_png")).toBe(true);
    expect(att.view).toBe("/shots/a.png");
    expect(att.thumb).toBe("/api/fs/raw?path=%2Fshots%2Fa.png");
    expect(att.size).toBeUndefined();
    expect(att.viewNote).toBe(
      "converted from HEIC to PNG on the server (4032×3024 → 800×600) because neither the browser nor the agent can read that format",
    );
  });

  test("a TYPELESS HEIC takes the same road — decode, fail, convert (Bugbot, PR #1064)", async () => {
    // The Safari drag: no MIME at all, so only the extension says "picture".
    undecodable();
    convert = { path: "/shots/a.png", width: 800, height: 600, source_w: 4032, source_h: 3024 };
    const att = await attachFile("/tpl", fileOf("IMG_4031.HEIC", "", 5000));
    expect(att.kind).toBe("image");
    // The copy on disk keeps the extension the bytes actually are…
    expect(uploads[0].path).toMatch(/\.heic$/);
    // …and the server was asked, which is the whole point.
    expect(runs.some((r) => r.action === "image_to_png")).toBe(true);
    expect(att.view).toBe("/shots/a.png");
    expect(att.viewNote).toBe(
      "converted from HEIC to PNG on the server (4032×3024 → 800×600) because neither the browser nor the agent can read that format",
    );
  });

  test("a conversion that also fails says the true thing, with bytes and a size (T:11588)", async () => {
    undecodable();
    convert = null;
    const att = await attachFile("/tpl", fileOf("IMG.HEIC", "image/heic", 5000));
    expect(att.kind).toBe("image");
    expect(att.size).toBe(5000);
    expect(att.thumb).toBeUndefined();
    expect(att.viewNote).toBe(
      "attached as bytes: this browser cannot decode IMG.HEIC and the server could not convert it either, so the agent probably cannot read this format — say so rather than guessing at the pixels",
    );
  });

  test("a file rides whole, with a size and no thumb, and no size refusal (D615)", async () => {
    const att = await attachFile("/tpl", fileOf("dump.parquet", "", 40 * 1024 * 1024));
    expect(att.kind).toBe("file");
    expect(att.size).toBe(40 * 1024 * 1024);
    expect(att.thumb).toBeUndefined();
    expect(uploads[0].path).toMatch(/\.parquet$/);
  });

  test("THE ONE REFUSAL: an upload that failed (T:11611)", async () => {
    Object.assign(globalThis, {
      fetch: () => Promise.reject(new Error("readonly")),
    });
    const att = await attachFile("/tpl", fileOf("a.png", "image/png", 10));
    expect(att.view).toBeNull();
    expect(att.why).toBe("could not be saved");
    expect(att.viewNote).toBe("not attached: it could not be saved (readonly)");
  });
});
