// ---- attachments on the New task card ------------------------------------------
// The pure halves (buildSchedulePayload, attachmentKindOf, taskPreviewSrcFor) are
// tested as the functions they are; the wiring that no pure function holds — what
// a paste intercepts, when the upload is awaited, where an Edit's attachments come
// from — is pinned to the source, this repo's habit for exactly that kind of claim
// (see new-task-form.test.ts and repoCardControls.test.ts).
//
// ANY FILE, NO CAPS (D618): the count cap, the byte cap, the image-only MIME gate
// and the ＋ picker are all gone, and each absence is asserted AS an absence —
// the only way a cap that comes back is caught.
import { beforeAll, describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// Same stubs as new-task-form.test.ts, and REQUIRED before the dynamic import:
// the modal pulls in the router, which reads `location` at module init — and a
// bare import here poisons the shared module registry for every other file.
const g = globalThis as unknown as Record<string, unknown>;
g.location ??= { pathname: "/tasks", search: "", hash: "", href: "http://x/tasks", origin: "http://x" };
g.history ??= { replaceState() {}, pushState() {}, state: null };
g.window ??= globalThis;
g.document ??= { addEventListener() {}, removeEventListener() {}, querySelector: () => null };

let buildSchedulePayload: typeof import("./NewJobModal").buildSchedulePayload;
let restoredAttachments: typeof import("./NewJobModal").restoredAttachments;
let attachmentKindOf: typeof import("./NewJobModal").attachmentKindOf;
let taskPreviewSrcFor: typeof import("./NewJobModal").taskPreviewSrcFor;

beforeAll(async () => {
  const mod = await import("./NewJobModal");
  buildSchedulePayload = mod.buildSchedulePayload;
  restoredAttachments = mod.restoredAttachments;
  attachmentKindOf = mod.attachmentKindOf;
  taskPreviewSrcFor = mod.taskPreviewSrcFor;
});

const FORM = {
  target: "/tmp/x",
  message: "body",
  title: "Name",
  when: "2026-08-26T10:00",
  rule: null,
  repeat: "none",
  legacyCron: "",
  permission: "auto",
  sessionId: "",
  newTaskEachRun: false,
};

const STAT = (templates: { mode: string; path: string | null; conditional?: boolean }[],
              extra: Record<string, unknown> = {}) =>
  ({ path: "/s/a.csv", name: "a.csv", is_dir: false, size: 1, mtime: 1,
     templates, ...extra }) as unknown as Parameters<typeof taskPreviewSrcFor>[0];

describe("what goes on the wire", () => {
  it("carries every uploaded path in attach order — MORE than the four the old cap allowed, and any file type", () => {
    // The cap is gone on both sides (schedule._images lost its count check).
    const paths = ["a.png", "b.csv", "c.pdf", "d.log", "e.parquet", "f.zip"]
      .map((n) => "/home/.fused-render/task-shots/" + n);
    expect(buildSchedulePayload({ ...FORM, images: paths }).images).toEqual(paths);
  });

  it("leaves both keys off the wire entirely when nothing is attached", () => {
    const empty = buildSchedulePayload({ ...FORM, images: [], attachments: [] });
    expect("images" in empty).toBe(false);
    expect("attachments" in empty).toBe(false);
    expect("images" in buildSchedulePayload(FORM)).toBe(false);
    expect("attachments" in buildSchedulePayload(FORM)).toBe(false);
  });
});

describe("names and kinds ride along with the paths (D619)", () => {
  const RICH = [
    { path: "/h/task-shots/20260828-a1.pdf", name: "Q3 report.pdf",
      kind: "file" as const },
    { path: "/h/task-shots/20260828-b2.png", name: "chart.png",
      kind: "image" as const },
  ];

  it("sends `attachments` beside `images`, same order", () => {
    // THE BUG D619 closes: the fired run writes the chat's own <pane-shot>
    // block, whose receipt rows show a thumbnail or 📄 plus the file's NAME.
    // A minted path (`20260828-a1.pdf`) cannot fill that row, so the two facts
    // only the browser knows have to travel.
    const body = buildSchedulePayload({
      ...FORM, images: RICH.map((a) => a.path), attachments: RICH,
    });
    expect(body.attachments).toEqual(RICH);
    // …BESIDE, never instead of: every existing reader of an entry knows only
    // `images`, and the server's Read grant is derived from it.
    expect(body.images).toEqual(RICH.map((a) => a.path));
  });
});

describe("an edit reopens on the names the entry stored", () => {
  it("prefers the stored attachments over the bare paths", () => {
    const chips = restoredAttachments({
      images: ["/h/task-shots/20260828-a1.pdf"],
      attachments: [{ path: "/h/task-shots/20260828-a1.pdf",
                      name: "Q3 report.pdf", kind: "file" }],
    });
    expect(chips.map((c) => [c.name, c.kind, c.thumb]))
      .toEqual([["Q3 report.pdf", "file", null]]);
  });

  it("falls back to basename and extension for an entry stored before D619", () => {
    // Every entry on disk before today has only paths. A worse answer than the
    // browser's, and the only one available — the same one the server derives
    // (schedule._derived_attachment).
    const chips = restoredAttachments({
      images: ["/h/task-shots/20260828-a1.png", "/h/task-shots/20260828-b2.csv"],
    });
    expect(chips.map((c) => [c.name, c.kind])).toEqual([
      ["20260828-a1.png", "image"],
      ["20260828-b2.csv", "file"],
    ]);
  });

  it("opens with no chips at all for a new task", () => {
    expect(restoredAttachments(null)).toEqual([]);
    expect(restoredAttachments(undefined)).toEqual([]);
    expect(restoredAttachments({})).toEqual([]);
  });

  it("takes the entry's kind even where the extension disagrees", () => {
    // A `.tif` the upload endpoint transcoded is stored as the PNG beside it,
    // but a stored `kind` is the browser's own answer and outranks the guess.
    const chips = restoredAttachments({
      attachments: [{ path: "/h/task-shots/x.tif", name: "scan.tif",
                      kind: "image" }],
    });
    expect(chips[0]!.kind).toBe("image");
  });
});

describe("kind, decided by extension for a path with no File behind it", () => {
  it("draws a picture only for a format this engine can actually draw", () => {
    for (const p of ["/s/a.png", "/s/a.JPG", "/s/a.jpeg", "/s/a.gif",
                     "/s/a.webp", "/s/a.svg"]) {
      expect(attachmentKindOf(p)).toBe("image");
    }
  });

  it("a TIFF or a HEIC is a FILE here — the browser shows one as an empty box", () => {
    // The upload endpoint converts those to a `-view.png` beside the original,
    // and that path matches the drawable list on its own.
    for (const p of ["/s/a.tif", "/s/a.tiff", "/s/a.heic", "/s/a.HEIF"]) {
      expect(attachmentKindOf(p)).toBe("file");
    }
    expect(attachmentKindOf("/s/20260828-1-view.png")).toBe("image");
  });

  it("anything else, and anything without an extension, is a file", () => {
    for (const p of ["/s/a.csv", "/s/a.parquet", "/s/a.pdf", "/s/notes",
                     "/s/some.dir/notes"]) {
      expect(attachmentKindOf(p)).toBe("file");
    }
  });
});

describe("the file preview's URL — the chat's rule, ported (D616)", () => {
  it("frames the first offerable template, with both display-only stamps", () => {
    const src = taskPreviewSrcFor(STAT([{ mode: "duckdb", path: "/t/duckdb/index.html" }]),
                                  "/s/a.csv");
    expect(src).toBe("/render?path=%2Ft%2Fduckdb%2Findex.html&_file=%2Fs%2Fa.csv"
      + "&_preview=1&_nofocus=1");
  });

  it("skips a `conditional` entry and the chat mode itself", () => {
    const src = taskPreviewSrcFor(STAT([
      { mode: "claude", path: "/t/claude/index.html" },
      { mode: "gated", path: "/t/gated/index.html", conditional: true },
      { mode: "code", path: "/t/code/index.html" },
    ]), "/s/a.csv");
    expect(src).toContain("path=%2Ft%2Fcode%2Findex.html");
  });

  it("the `_render` sentinel is a bare /render on the file itself", () => {
    expect(taskPreviewSrcFor(STAT([{ mode: "_render", path: null }]), "/s/a.html"))
      .toBe("/render?path=%2Fs%2Fa.html&_preview=1&_nofocus=1");
  });

  it("forwards stat's remote hint", () => {
    expect(taskPreviewSrcFor(STAT([{ mode: "duckdb", path: "/t/d/i.html" }],
                                  { remote: true }), "/s/a.csv"))
      .toContain("&_remote=1");
  });

  it("null — not an error — for every no-preview answer", () => {
    expect(taskPreviewSrcFor(null, "/s/a.csv")).toBe(null);
    expect(taskPreviewSrcFor(STAT([]), "/s/a.csv")).toBe(null);
    expect(taskPreviewSrcFor(STAT([{ mode: "claude", path: "/t/c/i.html" }]),
                             "/s/a.csv")).toBe(null);
    expect(taskPreviewSrcFor(STAT([{ mode: "x", path: null }]), "/s/a.csv")).toBe(null);
    expect(taskPreviewSrcFor(STAT([{ mode: "duckdb", path: "/t/d/i.html" }],
                                  { is_dir: true }), "/s/a.csv")).toBe(null);
    expect(taskPreviewSrcFor(STAT([{ mode: "duckdb", path: "/t/d/i.html" }]), ""))
      .toBe(null);
  });
});

const HERE = import.meta.dir;
const MODAL = readFileSync(join(HERE, "NewJobModal.tsx"), "utf8");
const API = readFileSync(join(HERE, "../platform/lib/api.ts"), "utf8");
const CSS = readFileSync(join(HERE, "../styles/new-task.css"), "utf8");

describe("the wiring the pure tests cannot see", () => {
  it("a paste attaches ANY file, and still only a file", () => {
    // Ordinary text pastes must stay exactly what they were — the intercept is
    // keyed on file kind alone now, and only then preventDefault()s.
    expect(MODAL).toContain('.filter((f): f is File => !!f)');
    expect(MODAL).not.toContain('f.type.startsWith("image/")');
    expect(MODAL).toContain("if (files.length) {");
  });

  it("a drop attaches every file, unfiltered", () => {
    expect(MODAL).toContain("const picked = [...(files ?? [])];");
    expect(MODAL).toContain("attachFiles(e.dataTransfer.files);");
  });

  it("BOTH text fields take the paste — a file dropped on the title attaches too", () => {
    expect(MODAL.split("onPaste={pasteFiles}").length - 1).toBe(2);
  });

  it("NO caps and NO picker are left in the source", () => {
    expect(MODAL).not.toContain("IMAGES_MAX = ");
    expect(MODAL).not.toContain("imagesRef.current.length >=");
    expect(MODAL).not.toContain("nt-img-add");
    expect(MODAL).not.toContain('type="file"');
    expect(CSS).not.toContain(".nt-img-add");
  });

  it("the upload is MULTIPART — the File goes up, never a base64 string", () => {
    expect(API).toContain('form.append("file", file, file.name || "attachment");');
    expect(API).toContain('fetch("/api/schedule/shot", {');
    expect(API).toContain('headers: { "X-Fused": "1" },');
    expect(API).not.toContain('"/api/schedule/shot", { data:');
    expect(MODAL).toContain("uploadTaskShot(file)");
    // No read step at all any more: the only thing a FileReader was for here
    // (a data-URL thumbnail) is a blob URL now.
    expect(MODAL).not.toContain("new FileReader");
    expect(MODAL).not.toContain("readAsDataURL");
  });

  it("the server's kind is trusted only where the stored path can be drawn", () => {
    // A .tif goes up as bytes no browser draws and comes back a PNG.
    // a failed transcode hands back `kind: "image"` on a `.tif` nobody can draw
    // — that chip wears the glyph and the file viewer, not an empty <img>
    expect(MODAL).toContain(
      'const kind = up.kind === "image" && attachmentKindOf(up.path) === "image"');
    expect(MODAL).toContain('? "image" : (i.thumb ? "image" : "file");');
    expect(MODAL).toContain("return { ...i, path: up.path, kind };");
  });

  it("a picture's thumbnail is a blob URL, revoked when the chip goes", () => {
    expect(MODAL).toContain("URL.createObjectURL(file)");
    expect(MODAL).toContain("if (img.thumb) URL.revokeObjectURL(img.thumb);");
  });

  it("the chip is a thumbnail XOR a glyph, never both", () => {
    expect(MODAL).toContain('{img.kind === "image" && (img.thumb || img.path) ? (');
    expect(MODAL).toContain('<span className="nt-img-glyph" aria-hidden="true">📄</span>');
    expect(MODAL).toContain('<span className="nt-img-name">{img.name}</span>');
    // Same footprint as a thumbnail: one height, in one place.
    expect(CSS).toContain("button.nt-img-doc {");
    expect(CSS).toContain("max-width: 18ch;");
    // the pill keeps its ✕ in flow — never over the name
    expect(CSS).toContain(".nt-img:has(.nt-img-doc) button.nt-img-x {");
    expect(CSS).toContain("position: static;");
  });

  it("the whole upload is registered BEFORE it is awaited anywhere", () => {
    const add = MODAL.indexOf("pendingRef.current.add(pending);");
    const up = MODAL.indexOf("const pending: Promise<void> = uploadTaskShot(file)");
    expect(up).toBeGreaterThan(-1);
    expect(add).toBeGreaterThan(up);
  });

  it("Save waits out in-flight uploads, then reads paths the ref already holds", () => {
    // The ref is the AUTHORITY, not a mirror taken at render: a `setImages`
    // updater only reaches `images` on the next render, so a drop-then-Save
    // read an empty path and filter(Boolean) dropped the attachment.
    expect(MODAL).toContain("await Promise.all([...pendingRef.current])");
    expect(MODAL).toContain("imagesRef.current.map((i) => i.path).filter(Boolean)");
    expect(MODAL).toContain("imagesRef.current = fn(imagesRef.current);");
  });

  it("every mutation goes through applyImages — nothing writes state alone", () => {
    expect(MODAL.split("setImages(").length - 1).toBe(1);
  });

  it("an Edit opens through restoredAttachments, and a restored chip draws off its stored path", () => {
    // The pure tests above own WHAT is restored; this pins that the modal
    // actually reaches for that function rather than a second derivation.
    expect(MODAL).toContain("restoredAttachments(editing)");
    expect(MODAL).toContain("img.thumb ?? rawUrl(img.path)");
  });

  it("a picture zooms; a file does not — one viewer per kind", () => {
    expect(MODAL).toContain('{viewer && viewer.kind === "image" && (');
    expect(MODAL).toContain('{viewer && viewer.kind === "file" && (');
    expect(MODAL).toContain("setViewerZoom((z) => !z)");
  });

  it("the file frame wears the shared seal, imported and not mirrored", () => {
    expect(MODAL).toContain('import { THUMB_SEAL } from "@platform/lib/frame-focus";');
    expect(MODAL).toContain('import { thumbUrl } from "@platform/lib/thumb-frame";');
    expect(MODAL).toContain("{...THUMB_SEAL}");
    expect(MODAL).toContain('tabIndex={-1}');
    // Exactly one iframe in this file: no chip and no receipt boots a template.
    expect(MODAL.split("<iframe").length - 1).toBe(1);
  });

  it('"loading preview…" stands until the frame itself fires load', () => {
    expect(MODAL).toContain("onLoad={() => setFrameLoaded(true)}");
    expect(MODAL).toContain("{(previewWait || (!!previewSrc && !frameLoaded)) && (");
    expect(MODAL).toContain("loading preview…");
  });

  it("Close, Escape and the scrim all take the frame down", () => {
    // The conditional render IS the unmount: one `viewer` clears them all.
    expect(MODAL).toContain('className="nt-shotview-scrim" onClick={closeViewer}');
    expect(MODAL).toContain("const closeViewer = useCallback(() => setViewerKey(null), []);");
    expect(MODAL).toContain('document.addEventListener("keydown", onKey, { capture: true })');
    expect(MODAL).toContain("e.stopPropagation();");
  });

  it("a late stat never frames a file the user has moved on from", () => {
    expect(MODAL).toContain("let live = true;");
    expect(MODAL).toContain("return () => { live = false; };");
  });

  it("removing a chip also closes a viewer that was showing it", () => {
    expect(MODAL).toContain("setViewerKey((k) => (k === img.key ? null : k))");
  });

  it("the viewer follows the live entry, so an upload landing after the click reaches it", () => {
    // a key, re-read from `images` each render — never a snapshot of the chip
    expect(MODAL).toContain("const [viewerKey, setViewerKey] = useState<number | null>(null);");
    expect(MODAL).toContain("images.find((i) => i.key === viewerKey) ?? null");
    expect(MODAL).not.toContain("useState<TaskImage | null>");
    // and the stat re-runs on the fields it depends on, not on object identity
    expect(MODAL).toContain("}, [viewer?.key, viewer?.kind, viewer?.path]);");
  });

  it("every blob thumbnail is revoked when the form unmounts", () => {
    expect(MODAL).toContain("useEffect(() => () => {\n    for (const i of imagesRef.current) if (i.thumb) URL.revokeObjectURL(i.thumb);\n  }, []);");
  });

  it("a failed upload takes its chip with it", () => {
    expect(MODAL).toContain("applyImages((prev) => prev.filter((i) => i.key !== key))");
  });

  it("attaching arms the dirty guard — an added file must not be lost to a silent ✕", () => {
    expect(MODAL).toContain('images.map((i) => i.path || "pending").join("\\n") !== initial.images');
  });
});
