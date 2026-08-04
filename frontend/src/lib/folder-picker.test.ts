// The pure path logic inside the shared template folder picker
// (fused_render/templates/shared/folder-picker.js).
//
// That file is an IIFE that hangs itself off `window`, like ro-badge.js and
// graph-canvas.js — deliberately not an ES module, because templates load it
// with a plain <script src="/template-shared/…"> and have no bundler. So it is
// loaded here the way a browser would: evaluate the source against a stub
// global and read the object it exported. Importing it as a module instead
// would mean changing the shipped file's shape to suit the test.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join as joinPath } from "node:path";

const SOURCE = readFileSync(
  joinPath(import.meta.dir, "../../../fused_render/templates/shared/folder-picker.js"),
  "utf8"
);

interface Paths {
  parent(p: string): string;
  join(dir: string, name: string): string;
  isRoot(p: string): boolean;
  basename(p: string): string;
  freeName(base: string, taken: string[]): string;
  crumbs(dir: string): { path: string; label: string }[];
}

interface Choice {
  dir: string;
  name: string;
  path: string;
}

interface Picker {
  paths: Paths;
  open(opts?: Record<string, unknown>): Promise<Choice | null>;
}

// `fetch` is called bare inside the file (it is browser code, not a module), so
// it is shadowed with an extra Function parameter rather than by poking a global
// — the test must not leave a stubbed fetch behind for the next file.
function loadPicker(fetchStub?: typeof fetch, doc?: unknown): Picker {
  const globals: Record<string, unknown> = { document: doc, console: undefined };
  // No document touched at load time (CSS injects lazily, as in ro-badge.js),
  // so a bare object is a sufficient window for the pure helpers.
  new Function("window", "fetch", SOURCE)(globals, fetchStub ?? fetch);
  return globals.fusedFolderPicker as Picker;
}

const { paths } = loadPicker();

// ------------------------------------------------------------------ parent

test("parent walks one level up", () => {
  expect(paths.parent("/a/b/c")).toBe("/a/b");
  expect(paths.parent("/a/b/c/")).toBe("/a/b");
});

test("the root is its own parent, so Up can't escape the filesystem", () => {
  expect(paths.parent("/")).toBe("/");
  expect(paths.parent("/a")).toBe("/");
});

test("a Windows drive root is its own parent too", () => {
  expect(paths.parent("C:/Users/ada")).toBe("C:/Users");
  expect(paths.parent("C:/Users")).toBe("C:/");
  expect(paths.parent("C:/")).toBe("C:/");
});

// -------------------------------------------------------------------- join

test("join does not double the separator at a root", () => {
  expect(paths.join("/", "repo")).toBe("/repo");
  expect(paths.join("/a/b", "repo")).toBe("/a/b/repo");
  expect(paths.join("C:/", "repo")).toBe("C:/repo");
});

// ------------------------------------------------------------------ isRoot

test("isRoot recognises both kinds of root", () => {
  expect(paths.isRoot("/")).toBe(true);
  expect(paths.isRoot("C:/")).toBe(true);
  expect(paths.isRoot("/a")).toBe(false);
});

// ---------------------------------------------------------------- freeName

test("an unused name is proposed as-is", () => {
  expect(paths.freeName("project", ["other", "notes.txt"])).toBe("project");
});

test("a taken name gains the same numeric suffix the server derives", () => {
  // Mirrors reader.py's _free_dest, so the name the picker proposes is the one
  // the clone would have picked by itself.
  expect(paths.freeName("project", ["project"])).toBe("project 2");
  expect(paths.freeName("project", ["project", "project 2"])).toBe("project 3");
});

test("a clash only counts an exact name, not a prefix", () => {
  expect(paths.freeName("project", ["projector", "project-old"])).toBe("project");
});

test("freeName gives up rather than looping forever", () => {
  const taken = ["project", ...Array.from({ length: 200 }, (_, i) => `project ${i + 2}`)];
  const chosen = paths.freeName("project", taken);
  // Whatever it lands on, it must terminate and stay a usable name; the
  // server's own existence check is the backstop.
  expect(typeof chosen).toBe("string");
  expect(chosen.length).toBeGreaterThan(0);
});

// ---------------------------------------------------------------- basename

test("basename labels a folder, and a root has no label of its own", () => {
  expect(paths.basename("/a/b/c")).toBe("c");
  expect(paths.basename("/a/b/c/")).toBe("c");
  // Both roots answer "" so the caller can substitute the path. Stripping the
  // trailing slash off "C:/" would otherwise leave the bare "C:", which is a
  // cwd-relative path, not a folder name, and must never reach the UI.
  expect(paths.basename("/")).toBe("");
  expect(paths.basename("C:/")).toBe("");
});

// ------------------------------------------------------------------ crumbs

test("crumbs walk from the root down to the folder shown", () => {
  expect(paths.crumbs("/a/b").map((c) => c.path)).toEqual(["/", "/a", "/a/b"]);
  // The root's own label falls back to the path — "" would be an unclickable
  // gap at the head of the breadcrumb.
  expect(paths.crumbs("/a/b").map((c) => c.label)).toEqual(["/", "a", "b"]);
});

test("crumbs on a Windows path stop at the drive root", () => {
  expect(paths.crumbs("C:/Users/ada").map((c) => c.path)).toEqual([
    "C:/", "C:/Users", "C:/Users/ada",
  ]);
});

test("crumbs of a root are just the root", () => {
  expect(paths.crumbs("/")).toEqual([{ path: "/", label: "/" }]);
});

// ------------------------------------------------- native picker vs fallback
// The routing contract, which is the whole point of having two backends:
//   * the shell says it can -> POST /api/fs/pick-folder and use the answer;
//   * the user cancels       -> resolve null, do NOT re-ask in HTML;
//   * anything else          -> fall back to the in-page dialog.
// The fallback is observed by the dialog TOUCHING the document: the stub below
// throws a recognisable error from createElement, so "fell back" and "did not
// fall back" are distinguishable without a DOM implementation.

const FELL_BACK = "fp-test: the in-page dialog was opened";

const throwingDocument = {
  createElement() {
    throw new Error(FELL_BACK);
  },
};

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

/** A fetch stub answering /api/config and /api/fs/pick-folder, recording calls. */
function stubFetch(
  config: Record<string, unknown>,
  pick: () => Response,
  listing: string[] = []
) {
  const calls: { url: string; body?: unknown }[] = [];
  const impl = ((url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url === "/api/config") return Promise.resolve(jsonResponse(200, config));
    if (url === "/api/fs/pick-folder") return Promise.resolve(pick());
    if (url.startsWith("/api/fs/list")) {
      return Promise.resolve(
        jsonResponse(200, {
          path: decodeURIComponent(url.split("=")[1] ?? "/"),
          entries: listing.map((name) => ({ name, is_dir: true })),
        })
      );
    }
    throw new Error("unexpected fetch: " + url);
  }) as unknown as typeof fetch;
  return { impl, calls };
}

test("with the capability advertised, a chosen folder comes from the OS dialog", async () => {
  const { impl, calls } = stubFetch(
    { native_dir_picker: true },
    () => jsonResponse(200, { path: "/Users/ada/code" })
  );
  const picker = loadPicker(impl, throwingDocument);
  expect(await picker.open({ start: "/Users/ada" })).toEqual({
    dir: "/Users/ada/code",
    name: "",
    path: "/Users/ada/code",
  });
  // The starting directory really reaches the endpoint — a native dialog that
  // always opens at the home folder is a native dialog nobody wants.
  expect(calls.find((c) => c.url === "/api/fs/pick-folder")?.body).toEqual({
    start: "/Users/ada",
    title: null,
  });
});

test("a native cancel resolves null and does NOT open the HTML dialog", async () => {
  // The bug this guards: treating {path: null} as a failure would answer a
  // cancel by popping a second, different chooser.
  const { impl } = stubFetch(
    { native_dir_picker: true },
    () => jsonResponse(200, { path: null })
  );
  const picker = loadPicker(impl, throwingDocument);
  expect(await picker.open({ start: "/Users/ada" })).toBeNull();
});

test("with `name` set, the native dialog's folder gains a free child name", async () => {
  const { impl } = stubFetch(
    { native_dir_picker: true },
    () => jsonResponse(200, { path: "/Users/ada/code" }),
    ["project", "project 2"]
  );
  const picker = loadPicker(impl, throwingDocument);
  expect(await picker.open({ start: "/Users/ada", name: "project" })).toEqual({
    dir: "/Users/ada/code",
    name: "project 3",
    path: "/Users/ada/code/project 3",
  });
});

test("no advertised capability falls back to the in-page dialog", async () => {
  // The hosted case: there is no GUI session to raise a dialog into, so the
  // endpoint is never even called.
  const { impl, calls } = stubFetch({}, () => jsonResponse(200, { path: "/nope" }));
  const picker = loadPicker(impl, throwingDocument);
  await expect(picker.open({ start: "/Users/ada" })).rejects.toThrow(FELL_BACK);
  expect(calls.map((c) => c.url)).toEqual(["/api/config"]);
});

test("a failing native dialog falls back instead of dead-ending", async () => {
  // Busy (409) or unavailable (501): the button must still do something.
  const { impl } = stubFetch(
    { native_dir_picker: true },
    () => jsonResponse(409, { error: "a folder chooser is already open" })
  );
  const picker = loadPicker(impl, throwingDocument);
  await expect(picker.open({ start: "/Users/ada" })).rejects.toThrow(FELL_BACK);
});

test("native: false skips the capability probe entirely", async () => {
  const { impl, calls } = stubFetch(
    { native_dir_picker: true },
    () => jsonResponse(200, { path: "/nope" })
  );
  const picker = loadPicker(impl, throwingDocument);
  await expect(picker.open({ native: false })).rejects.toThrow(FELL_BACK);
  expect(calls).toEqual([]);
});

// ----------------------------------------------------------- the CSS literal
// The stylesheet is a template literal, so a backtick or a `${` anywhere inside
// it — including inside a CSS comment — ends the literal mid-rule and the whole
// file becomes a SyntaxError. Every test above loads the source, so any of them
// would fail; this one names the cause, because "picker: undefined" in a browser
// console does not.

test("the CSS literal contains nothing that could terminate it", () => {
  const start = SOURCE.indexOf("var CSS = `");
  expect(start).toBeGreaterThan(-1);
  const body = SOURCE.slice(start + "var CSS = `".length);
  const css = body.slice(0, body.indexOf("`;"));
  expect(css.length).toBeGreaterThan(1000);       // it really is the stylesheet
  expect(css).not.toInclude("`");
  expect(css).not.toInclude("${");
});

test("the caller's title rides to the OS dialog, not just to the fallback", () => {
  // The dialog the user sees is the native one, so it has to say what it is for.
  const { impl, calls } = stubFetch(
    { native_dir_picker: true },
    () => jsonResponse(200, { path: "/Users/ada/code" })
  );
  const picker = loadPicker(impl, throwingDocument);
  return picker
    .open({ start: "/Users/ada", title: "Clone this bundle into…" })
    .then(() => {
      expect(calls.find((c) => c.url === "/api/fs/pick-folder")?.body).toEqual({
        start: "/Users/ada",
        title: "Clone this bundle into…",
      });
    });
});
