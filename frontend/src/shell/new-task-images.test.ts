// ---- images on the New task card -----------------------------------------------
// The pure half (buildSchedulePayload) is tested as the function it is; the
// wiring that no pure function holds — what a paste intercepts, when the upload
// is awaited, where an Edit's images come from — is pinned to the source, this
// repo's habit for exactly that kind of claim (see new-task-form.test.ts and
// repoCardControls.test.ts).
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
let IMAGES_MAX: typeof import("./NewJobModal").IMAGES_MAX;

beforeAll(async () => {
  const mod = await import("./NewJobModal");
  buildSchedulePayload = mod.buildSchedulePayload;
  IMAGES_MAX = mod.IMAGES_MAX;
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

describe("what goes on the wire", () => {
  it("carries the uploaded paths, in attach order", () => {
    const body = buildSchedulePayload({
      ...FORM,
      images: ["/home/.fused-render/task-shots/a.png",
               "/home/.fused-render/task-shots/b.jpg"],
    });
    expect(body.images).toEqual(["/home/.fused-render/task-shots/a.png",
                                 "/home/.fused-render/task-shots/b.jpg"]);
  });

  it("leaves the key off the wire entirely when nothing is attached", () => {
    expect("images" in buildSchedulePayload({ ...FORM, images: [] })).toBe(false);
    expect("images" in buildSchedulePayload(FORM)).toBe(false);
  });

  it("the cap matches the server's IMAGES_MAX", () => {
    expect(IMAGES_MAX).toBe(4);
  });
});

const HERE = import.meta.dir;
const MODAL = readFileSync(join(HERE, "NewJobModal.tsx"), "utf8");
const API = readFileSync(join(HERE, "../platform/lib/api.ts"), "utf8");

describe("the wiring the payload test cannot see", () => {
  it("a paste attaches ONLY when the clipboard holds an image file", () => {
    // Ordinary text pastes must stay exactly what they were — the intercept
    // is keyed on file kind + image type, and only then preventDefault()s.
    expect(MODAL).toContain('.filter((f): f is File => !!f && f.type.startsWith("image/"))');
    expect(MODAL).toContain("if (files.length) {");
  });

  it("BOTH text fields take the paste — a screenshot on the title attaches too", () => {
    expect(MODAL.split("onPaste={pasteImages}").length - 1).toBe(2);
  });

  it("a chip's thumbnail opens the viewer, and a second click zooms it", () => {
    // The claude template's #shotview, ported: fitted first, click for natural
    // size with the box scrolling, scrim and Escape both close it.
    expect(MODAL).toContain('className="nt-shotview-scrim" onClick={() => setViewer(null)}');
    expect(MODAL).toContain("setViewerZoom((z) => !z)");
  });

  it("Escape closes the viewer WITHOUT reaching the modal's own close", () => {
    expect(MODAL).toContain('document.addEventListener("keydown", onKey, { capture: true })');
    expect(MODAL).toContain("e.stopPropagation();");
  });

  it("removing a chip also closes a viewer that was showing it", () => {
    expect(MODAL).toContain("setViewer((v) => (v?.key === img.key ? null : v))");
  });

  it("the cap is answered against the ref BEFORE the upload starts", () => {
    // A file that loses the cap race must not POST anyway: that writes orphan
    // bytes into a dir with no TTL and leaves Save an upload to await that no
    // chip will ever show (Bugbot, PR #865).
    const at = MODAL.indexOf("if (imagesRef.current.length >= IMAGES_MAX) return;");
    expect(at).toBeGreaterThan(-1);
    expect(at).toBeLessThan(MODAL.indexOf("uploadTaskShot(dataUrl)"));
  });

  it("Save waits out in-flight uploads, then reads paths the ref already holds", () => {
    // The ref is the AUTHORITY, not a mirror taken at render: a `setImages`
    // updater only reaches `images` on the next render, so a drop-then-Save
    // read an empty path and filter(Boolean) dropped the picture.
    expect(MODAL).toContain("await Promise.all([...uploadsRef.current])");
    expect(MODAL).toContain("imagesRef.current.map((i) => i.path).filter(Boolean)");
    expect(MODAL).toContain("imagesRef.current = fn(imagesRef.current);");
  });

  it("every mutation goes through applyImages — nothing writes state alone", () => {
    // One bare setImages remains (applyImages' own mirror); any other would be
    // a path the ref never learned about.
    expect(MODAL.split("setImages(").length - 1).toBe(1);
  });

  it("an Edit opens on the entry's stored paths, drawn through /api/fs/raw", () => {
    expect(MODAL).toContain("(editing?.images ?? []).map((p, i)");
    expect(MODAL).toContain("img.dataUrl ?? rawUrl(img.path)");
  });

  it("a failed upload takes its thumbnail with it", () => {
    expect(MODAL).toContain("applyImages((prev) => prev.filter((i) => i.key !== key))");
  });

  it("attaching arms the dirty guard — an added image must not be lost to a silent ✕", () => {
    expect(MODAL).toContain('images.map((i) => i.path || "pending").join("\\n") !== initial.images');
  });

  it("the upload endpoint is the only writer the form talks to", () => {
    expect(API).toContain('postJson<{ path: string }>("/api/schedule/shot", { data: dataUrl })');
  });
});
