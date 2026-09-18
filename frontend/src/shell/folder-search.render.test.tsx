// FINDING A FOLDER, on the two surfaces that ask for one (Akshil, 2026-09-18:
// "add search functionality for path in new task modal path, and in project
// filter").
//
// Both are TYPEAHEADS OVER THE SEARCH THIS APP ALREADY HAS, and that is the
// thing worth pinning: the path field asks the file index for directories
// (`searchFiles`, `kind: "dir"` — the one endpoint that can be asked for
// folders alone) and reads its keys with the Explorer address bar's own pure
// key map (`completionKeyAction`); the project menu narrows a list this page is
// already holding with the toolbar's own rule (`tasks-lib.projectMatches`).
// Neither invents a language, and neither is the other one's engine.
//
// Mounted rather than sniffed from source, because what broke in the reported
// bug is what a press DOES, and the same fixtures as
// `new-task-run-settings.render.test.tsx` — see its header for why the shim's
// globals are stated here rather than trusted.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";

const realFetch = globalThis.fetch;
const doc = globalThis.document as unknown as { body?: unknown };

function node() {
  return {
    focus() {}, blur() {}, select() {}, setSelectionRange() {}, scrollIntoView() {},
    addEventListener() {}, removeEventListener() {},
    contains: () => false, closest: () => null,
    querySelector: () => null, querySelectorAll: () => [] as unknown[],
    children: [] as unknown[], style: {} as Record<string, string>, value: "",
    getBoundingClientRect: () => ({
      top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0,
    }),
  };
}

const inert = class {
  observe() {} unobserve() {} disconnect() {}
  takeRecords() { return [] as unknown[]; }
};
const g = globalThis as Record<string, unknown>;
g.ResizeObserver = inert;
g.MutationObserver = inert;
g.getComputedStyle = () => new Proxy(
  { getPropertyValue: () => "", getPropertyPriority: () => "", item: () => "", length: 0 } as Record<string, unknown>,
  { get: (t, k) => (k in t ? t[k as string] : "") },
);

/** The card debounces its path check with `window.setTimeout`, and a suite
 *  earlier in the run can leave a `window` on the shared global that has none —
 *  see `new-task-run-settings.render.test.tsx`'s own note. Filled in, never
 *  replaced, and handed back as found. */
const timersWas = new Map<string, unknown>();

function installWindowTimers() {
  const w = globalThis.window as unknown as Record<string, unknown>;
  if (!w) return;
  for (const name of ["setTimeout", "clearTimeout"]) {
    if (typeof w[name] === "function") continue;
    timersWas.set(name, name in w ? w[name] : undefined);
    w[name] = (globalThis[name as "setTimeout"] as (...a: never[]) => unknown)
      .bind(globalThis);
  }
}

function removeWindowTimers() {
  const w = globalThis.window as unknown as Record<string, unknown>;
  for (const [name, before] of timersWas) {
    if (before === undefined) delete w[name];
    else w[name] = before;
  }
  timersWas.clear();
}

// EVERY renderer this file creates, not the last one. A test that mounts twice
// (reopen a menu, remount a card) used to overwrite `box` and orphan the first
// tree: its Modal never unmounted, so its token stayed on `esc-stack`'s
// module-level stack and four unrelated modal tests later in the run found a
// stack that was not empty. Unmount them all, newest first.
const boxes: ReactTestRenderer[] = [];
let box: ReactTestRenderer | null = null;

function track(r: ReactTestRenderer): ReactTestRenderer {
  boxes.push(r);
  return r;
}

afterEach(async () => {
  box = null;
  for (const b of boxes.splice(0).reverse()) {
    await act(async () => b.unmount());
  }
  globalThis.fetch = realFetch;
  removeWindowTimers();
  delete doc.body;
});

function json(body: unknown): Promise<Response> {
  return Promise.resolve(
    { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response,
  );
}

/** Long enough for the field's 200ms search debounce AND its 400ms path check
 *  to have fired and settled. */
const SETTLE_MS = 700;

async function settle(ms = SETTLE_MS) {
  await act(async () => { await new Promise((r) => setTimeout(r, ms)); });
}

/** The PATHS the dropdown is currently offering, in the order it draws them.
 *
 *  Read off each row's `title`, not its text: the rows print the BASENAME now
 *  (the Explorer's rule — the address is in the field, so a row that repeats it
 *  is saying the same thing twice), and what these tests are about is which
 *  FOLDER each row would put in the field. */
function pathRowLabels(b: ReactTestRenderer): string[] {
  return b.root
    .findAll((n) => n.type === "button" && n.props.role === "option")
    .map((n) => {
      const said = n.findAll((c) => c.props?.className === "schedule-recents-path"
        || c.props?.className === "schedule-picker-name");
      return String(said[0]?.props?.title ?? "");
    });
}

/** What one row PRINTS — the basename, and the muted folder beside it. */
function pathRowText(b: ReactTestRenderer): { name: string; where: string }[] {
  return b.root
    .findAll((n) => n.type === "button" && n.props.role === "option")
    .map((n) => ({
      name: String(n.findAll((c) => c.props?.className === "schedule-recents-path"
        || c.props?.className === "schedule-picker-name")[0]?.children?.[0] ?? ""),
      where: String(n.findAll(
        (c) => c.props?.className === "schedule-recents-where")[0]?.children?.[0] ?? ""),
    }));
}

// ---- (a) the New task card's path field -------------------------------------

/** The card, with a server that answers the folder search with `dirs` and
 *  everything else with the emptiest true answer it has. `asked` collects the
 *  specs the field posted, so the QUESTION can be asserted and not only the
 *  answer. */
async function openCard(
  dirs: string[],
  asked: unknown[],
  missing = "",
  listing: Record<string, { name: string; is_dir: boolean; size: number | null }[]> = {},
) {
  const { default: NewJobModal } = await import("./NewJobModal");
  installWindowTimers();
  // THE FORM'S OWN MEMORY OF FOLDERS lives in `localStorage`
  // (`fused-render:recent-paths`, NewJobModal), which `bun test` shares across
  // every suite in the run — so a card another suite scheduled leaves its target
  // in this list and the rows below arrive with a folder nothing here has heard
  // of. The recents under test are the ones this suite passes in as
  // `recentTargets`; the stored tier starts empty.
  try {
    localStorage.removeItem("fused-render:recent-paths");
  } catch {
    // A blocked store has nothing to clear, which is the state we wanted.
  }
  doc.body = { nodeType: 1, children: [] as unknown[], createNodeMock: node };
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    const u = String(url);
    if (u.startsWith("/api/search/files")) {
      asked.push(init?.body ? JSON.parse(String(init.body)) : null);
      return json({
        entries: dirs.map((p) => ({ path: p, is_dir: true, size: null, mtime: null })),
        truncated: false,
      });
    }
    if (u.startsWith("/api/config")) return json({ home: "/Users/me" });
    // THE PATH CHECK. By default every path the field holds EXISTS — `listDir`
    // answers, the verdict is "ok", and no "New folder" row joins the list. A
    // test that wants that row passes `missing`: the typed path then fails to
    // list and its PARENT lists without it, which is exactly the shape
    // `targetVerdict` reads as a folder that is not there yet.
    if (u.startsWith("/api/fs/list")) {
      const at = decodeURIComponent((u.split("path=")[1] ?? "").split("&")[0]);
      if (missing && at === missing) return Promise.reject(new Error("no such dir"));
      // THE COMPLETION SOURCE. Keyed by the folder being listed, so a test can
      // stage one directory's children and watch the field complete through
      // them; anything not staged lists empty, which is the ordinary case and
      // keeps the path check quiet.
      return json({ entries: listing[at] ?? [] });
    }
    return json({ folders: [], entries: [], tasks: [], sessions: [] });
  }) as unknown as typeof fetch;
  await act(async () => {
    box = track(create(createElement(NewJobModal as never, {
      initialTime: null, permissionModes: ["auto"], initialTarget: "/Users/me/code",
      recentTargets: ["/Users/me/code", "/Users/me/news", "/Users/me/aviary"],
      onClose: () => {}, onCreated: () => {},
    } as never), { createNodeMock: node }));
  });
  return box!;
}

function pathField(b: ReactTestRenderer) {
  return b.root.findAll(
    (n) => n.type === "input" && n.props.role === "combobox",
  )[0];
}

test("focusing the path field still offers the folders this form remembers", async () => {
  const b = await openCard([], []);
  await act(async () => { pathField(b).props.onFocus({}); });
  await settle();
  // ALL of them, not the one already in the field. Opening the drop is the
  // reader LOOKING; narrowing against the path it is standing on would answer a
  // focus with a list of one.
  expect(pathRowLabels(b)).toEqual(
    ["/Users/me/code", "/Users/me/news", "/Users/me/aviary"]);
});

test("typing narrows what is remembered and asks the index for the rest", async () => {
  const asked: unknown[] = [];
  const b = await openCard(["/Users/me/Desktop/aviary-lite"], asked);
  await act(async () => { pathField(b).props.onFocus({}); });
  await act(async () => { pathField(b).props.onChange({ target: { value: "aviary" } }); });
  await settle();

  // DIRECTORIES ALONE, and the typed name is what it asked for.
  expect(asked).toEqual([{ kind: "dir", name_terms: ["aviary"], path_hints: [] }]);
  // The remembered folder that answers, then the one the index found — one run
  // of rows, because a row is a folder to run in and where the app learnt about
  // it is not something the reader has to hold.
  expect(pathRowLabels(b)).toEqual(
    ["/Users/me/aviary", "/Users/me/Desktop/aviary-lite"]);
});

test("arrowing to a found folder and pressing Enter fills the field with it", async () => {
  const b = await openCard(["/Users/me/Desktop/aviary-lite"], []);
  await act(async () => { pathField(b).props.onFocus({}); });
  await act(async () => { pathField(b).props.onChange({ target: { value: "aviary" } }); });
  await settle();

  // The list opens with NOTHING highlighted — `completionKeyAction`'s own
  // guarantee, so one Enter cannot mean two things depending on typing speed.
  const wrap = b.root.findAll((n) => n.props?.className === "schedule-recents-wrap")[0];
  const press = (key: string) =>
    act(async () => { wrap.props.onKeyDown({ key, preventDefault() {}, stopPropagation() {} }); });
  await press("ArrowDown");
  await press("ArrowDown");
  await press("Enter");

  expect(pathField(b).props.value).toBe("/Users/me/Desktop/aviary-lite");
  // …and the list is closed, which is what a pick means.
  expect(pathRowLabels(b)).toEqual([]);
});

/** The "<name> — New folder" suggestion's own row, by the badge only it wears. */
function newFolderRow(b: ReactTestRenderer) {
  return b.root.findAll(
    (n) => n.type === "button" && String(n.props.className ?? "").includes("schedule-recents-new"),
  );
}

test("ArrowDown once takes the best MATCH, never the create-new row", async () => {
  // THE DESTRUCTIVE DEFAULT (browser QA, 2026-09-18). The suggestion row led
  // the ring, so on a typed name with no exact match — "ann", with
  // `annfocus-repro` and `annotate-pack` sitting right there — the commonest
  // keystroke pair on a typeahead, ArrowDown then Enter, CREATED a folder
  // literally called "ann". The one row a reader almost never wants was the one
  // the keyboard reached first.
  //
  // So it is the LAST stop, which is where every tag and folder picker puts
  // "Create '<typed>'": the matches first, the new thing after them.
  const b = await openCard(
    ["/Users/me/Desktop/annfocus-repro", "/Users/me/Desktop/annotate-pack"],
    [],
    "ann",
  );
  await act(async () => { pathField(b).props.onFocus({}); });
  await act(async () => { pathField(b).props.onChange({ target: { value: "ann" } }); });
  await settle();

  // The suggestion IS offered — this is not a test that it went away.
  expect(newFolderRow(b).length).toBe(1);

  const wrap = b.root.findAll((n) => n.props?.className === "schedule-recents-wrap")[0];
  const press = (key: string) =>
    act(async () => { wrap.props.onKeyDown({ key, preventDefault() {}, stopPropagation() {} }); });
  await press("ArrowDown");
  await press("Enter");

  expect(pathField(b).props.value).toBe("/Users/me/Desktop/annfocus-repro");
});

test("arrowing past the last match still reaches the create-new row", async () => {
  // Last is not gone: the row stays in the ring, so the keyboard can still get
  // to it — it just has to be asked for.
  const b = await openCard(
    ["/Users/me/Desktop/annfocus-repro", "/Users/me/Desktop/annotate-pack"],
    [],
    "ann",
  );
  await act(async () => { pathField(b).props.onFocus({}); });
  await act(async () => { pathField(b).props.onChange({ target: { value: "ann" } }); });
  await settle();

  const wrap = b.root.findAll((n) => n.props?.className === "schedule-recents-wrap")[0];
  const press = (key: string) =>
    act(async () => { wrap.props.onKeyDown({ key, preventDefault() {}, stopPropagation() {} }); });
  // Two matches, then the suggestion.
  await press("ArrowDown");
  await press("ArrowDown");
  await press("ArrowDown");
  expect(newFolderRow(b)[0].props["aria-selected"]).toBe(true);
  // ArrowUp from -1 lands on the LAST row, which is the same one — the ring is
  // a ring, and the create-new row is its end.
  await press("Enter");
  // Its press keeps the path the field already holds and closes the list, which
  // is what its click has always done.
  expect(pathField(b).props.value).toBe("ann");
});

test("the create-new row is drawn LAST, under the matches", async () => {
  // The ring order and the DOM order are the same order, or
  // `aria-activedescendant` walks a reader backwards through a list that looks
  // forwards.
  const b = await openCard(["/Users/me/Desktop/annotate-pack"], [], "ann");
  await act(async () => { pathField(b).props.onFocus({}); });
  await act(async () => { pathField(b).props.onChange({ target: { value: "ann" } }); });
  await settle();
  const options = b.root.findAll((n) => n.type === "button" && n.props.role === "option");
  expect(String(options[options.length - 1].props.className)).toContain(
    "schedule-recents-new");
});


test("a search with no answer says so instead of showing an empty panel", async () => {
  const b = await openCard([], []);
  await act(async () => { pathField(b).props.onFocus({}); });
  await act(async () => { pathField(b).props.onChange({ target: { value: "zzzz" } }); });
  await settle();
  expect(pathRowLabels(b)).toEqual([]);
  const said = b.root.findAll((n) => n.props?.className === "schedule-recents-empty");
  expect(said.length).toBe(1);
});

// ---- (b) the Tasks page's project filter -------------------------------------

/** The filter bar, with `count` folders in its Project menu, already opened. */
async function openProjectMenu(folders: string[]) {
  const { TaskFilterControls, EMPTY_FILTERS } = await import("./ScheduleTaskViews");
  installWindowTimers();
  doc.body = { nodeType: 1, children: [] as unknown[], createNodeMock: node };
  await act(async () => {
    box = track(create(createElement(TaskFilterControls as never, {
      filters: EMPTY_FILTERS, projects: folders, home: "/Users/me",
      onChange: () => {},
    } as never), { createNodeMock: node }));
  });
  const b = box!;
  const trigger = b.root
    .findAll((n) => n.type === "button" && n.props.className?.includes?.("schedule-tv-filter-btn"))
    .find((n) => n.findAll((c) => String(c.children?.[0] ?? "") === "Project").length > 0);
  await act(async () => { trigger!.props.onClick(); });
  return b;
}

/** What the Project menu is offering, by the name each row prints. */
function projectRowLabels(b: ReactTestRenderer): string[] {
  return b.root
    .findAll((n) => n.type === "button" && n.props.role === "radio")
    .map((n) => {
      const said = n.findAll((c) => c.props?.className === "tasks-pop-label");
      return String(said[0]?.children?.[0] ?? "");
    });
}

function projectSearch(b: ReactTestRenderer) {
  return b.root.findAll(
    (n) => n.type === "input" && n.props["aria-label"] === "Find a folder",
  );
}

const MANY = [
  "/Users/me/aviary", "/Users/me/canvas", "/Users/me/code", "/Users/me/fused-render",
  "/Users/me/lens", "/Users/me/news", "/Users/me/showcase", "/Users/me/tribal",
];

test("a short project list is its own search — no box", async () => {
  // Seven rows are read in one glance. A field above them costs a press and
  // answers a question nobody had.
  const b = await openProjectMenu(MANY.slice(0, 4));
  expect(projectSearch(b).length).toBe(0);
  expect(projectRowLabels(b)).toEqual(
    ["All projects", "aviary", "canvas", "code", "fused-render"]);
});

test("enough folders and the menu can be searched", async () => {
  const b = await openProjectMenu(MANY);
  expect(projectSearch(b).length).toBe(1);
  await act(async () => { projectSearch(b)[0].props.onChange({ target: { value: "en" } }); });
  // Matched on the name the row prints AND on the path behind it — the same
  // rule the toolbar's own search box runs on.
  expect(projectRowLabels(b)).toEqual(["fused-render", "lens"]);
  // "All projects" steps aside while a question is being asked: it answers every
  // query, so at the top of the answers it is noise.
  expect(projectRowLabels(b)).not.toContain("All projects");
});

test("a project search with no answer says so", async () => {
  const b = await openProjectMenu(MANY);
  await act(async () => { projectSearch(b)[0].props.onChange({ target: { value: "zzz" } }); });
  expect(projectRowLabels(b)).toEqual([]);
  expect(b.root.findAll((n) => n.props?.className === "schedule-tv-pop-empty").length).toBe(1);
});

test("the box forgets what was typed once the menu closes", async () => {
  const b = await openProjectMenu(MANY);
  await act(async () => { projectSearch(b)[0].props.onChange({ target: { value: "lens" } }); });
  expect(projectRowLabels(b)).toEqual(["lens"]);
  const trigger = b.root
    .findAll((n) => n.type === "button" && n.props.className?.includes?.("schedule-tv-filter-btn"))
    .find((n) => n.findAll((c) => String(c.children?.[0] ?? "") === "Project").length > 0);
  await act(async () => { trigger!.props.onClick(); });   // close
  await act(async () => { trigger!.props.onClick(); });   // and open again
  // A query that outlived the press that closed the menu would come back as a
  // filter with a reason four clicks in the past.
  expect(projectSearch(b)[0].props.value).toBe("");
  expect(projectRowLabels(b).length).toBe(MANY.length + 1);
});

// ---- the Explorer address bar's behaviour, in this field ----------------------
//
// Akshil, 2026-09-18: "take a look at how path writing and search are integrated
// in the explorer path field, can we follow the same behaviour here?"
//
// The Explorer splits one field into two questions and answers each with a
// different engine (DECISIONS-one-field-search.md). A query that ESCAPES its
// base — a leading `/`, `~`, a drive, a `..` segment — names WHERE to look, so
// the answer is on disk: list that folder, filter by the partial name. A bare
// word names no place, so the only thing that can answer it is the index. The
// predicate is `isPathShapedQuery`, imported here rather than re-derived.

const DESKTOP = {
  "/Users/me/Desktop": [
    { name: "ann-demo", is_dir: true, size: null },
    { name: "annotate-pack", is_dir: true, size: null },
    { name: "notes.md", is_dir: false, size: 120 },
    { name: "zebra", is_dir: true, size: null },
  ],
  "/Users/me/Desktop/ann-demo": [
    { name: "src", is_dir: true, size: null },
  ],
};

async function typePath(b: ReactTestRenderer, value: string) {
  await act(async () => { pathField(b).props.onFocus({}); });
  await act(async () => { pathField(b).props.onChange({ target: { value } }); });
  await settle();
}

function press(b: ReactTestRenderer, key: string) {
  const wrap = b.root.findAll((n) => n.props?.className === "schedule-recents-wrap")[0];
  return act(async () => {
    wrap.props.onKeyDown({ key, preventDefault() {}, stopPropagation() {} });
  });
}

test("a path completes off its own folder — and asks the index nothing", async () => {
  const asked: unknown[] = [];
  const b = await openCard([], asked, "", DESKTOP);
  await typePath(b, "/Users/me/Desktop/ann");

  // The parent is listed and the partial is a case-insensitive PREFIX filter —
  // not fuzzy, not substring: `zebra` and `notes.md` are out, both `ann…` in.
  expect(pathRowLabels(b)).toEqual([
    "/Users/me/Desktop/ann-demo/", "/Users/me/Desktop/annotate-pack/"]);
  // …and the index was never asked. Two engines, one at a time.
  expect(asked).toEqual([]);
});

test("a bare word is a search, and the index answers it", async () => {
  const asked: unknown[] = [];
  const b = await openCard(["/Users/me/Desktop/annfocus-repro"], asked, "", DESKTOP);
  await typePath(b, "ann");
  expect(asked).toEqual([{ kind: "dir", name_terms: ["ann"], path_hints: [] }]);
  expect(pathRowLabels(b)).toContain("/Users/me/Desktop/annfocus-repro");
});

test("Tab takes the first completion and leaves the list open on the next segment",
  async () => {
    // THE EXPLORER COMPLETES ON TAB. An earlier round called this the other way
    // ("Tab leaves a form field") and that was wrong here: this field is the
    // same kind of control, and one address bar answering Tab differently from
    // the other is the divergence the shared key map exists to stop.
    const b = await openCard([], [], "", DESKTOP);
    await typePath(b, "/Users/me/Desktop/ann");
    await press(b, "Tab");

    // The WHOLE path with its trailing separator — `useCompletion`'s `item.path`
    // — which is what makes the next segment complete straight away.
    expect(pathField(b).props.value).toBe("/Users/me/Desktop/ann-demo/");
    await settle();
    expect(pathRowLabels(b)).toEqual(["/Users/me/Desktop/ann-demo/src/"]);
  });

test("Enter steps into a completed folder, and settles on a found one", async () => {
  // The Explorer NAVIGATES into a highlighted directory completion; here the
  // equivalent is "keep typing deeper", so the list stays open. A folder the
  // INDEX found is a whole address rather than a step towards one, so Enter on
  // it is the answer and the list closes.
  const stepping = await openCard([], [], "", DESKTOP);
  await typePath(stepping, "/Users/me/Desktop/ann");
  await press(stepping, "ArrowDown");
  await press(stepping, "Enter");
  expect(pathField(stepping).props.value).toBe("/Users/me/Desktop/ann-demo/");
  await settle();
  expect(pathRowLabels(stepping).length).toBeGreaterThan(0);

  const settling = await openCard(["/Users/me/Desktop/annfocus-repro"], [], "", DESKTOP);
  await typePath(settling, "ann");
  await press(settling, "ArrowDown");
  await press(settling, "Enter");
  expect(pathField(settling).props.value).toBe("/Users/me/Desktop/annfocus-repro");
  expect(pathRowLabels(settling)).toEqual([]);
});

test("a finished segment stops offering itself", async () => {
  // `isExactSingleMatch`: one row whose name is exactly what has been typed is
  // a list handing the reader what they already have.
  const b = await openCard([], [], "", DESKTOP);
  await typePath(b, "/Users/me/Desktop/zebra");
  expect(pathRowLabels(b)).toEqual([]);
});

test("a row prints the NAME, and says where only when the line does not",
  async () => {
    // The Explorer's rows print `item.name` and never the address — the address
    // is in the field one line above. A remembered folder or an index hit did
    // NOT come from that line, so it says where it is, quietly.
    const completing = await openCard([], [], "", DESKTOP);
    await typePath(completing, "/Users/me/Desktop/ann");
    expect(pathRowText(completing)).toEqual([
      { name: "ann-demo", where: "" },
      { name: "annotate-pack", where: "" },
    ]);

    const searching = await openCard(["/Users/me/Desktop/annfocus-repro"], [], "", DESKTOP);
    await typePath(searching, "ann");
    expect(pathRowText(searching)[0]).toEqual(
      { name: "annfocus-repro", where: "~/Desktop" });
  });

test("files complete too — a task may target one", async () => {
  // Not a folder-only list: `targetVerdict` has always allowed a file target,
  // and the Explorer's own completions include files for the same reason.
  const b = await openCard([], [], "", DESKTOP);
  await typePath(b, "/Users/me/Desktop/not");
  expect(pathRowLabels(b)).toEqual(["/Users/me/Desktop/notes.md"]);
  // …and a file is an ANSWER, not a step: no trailing separator, and Enter on it
  // closes the list rather than trying to complete inside it.
  await press(b, "ArrowDown");
  await press(b, "Enter");
  expect(pathField(b).props.value).toBe("/Users/me/Desktop/notes.md");
  expect(pathRowLabels(b)).toEqual([]);
});

test("taking the create-new row keeps the path, it does not become the name",
  async () => {
    // Caught in the browser: the suggestion's text is a NAME ("fu"), not an
    // address, and the ring's Enter wrote it into the field — turning
    // `/Users/me/Desktop/fu` into `fu`. Its press is only ever "close the list";
    // the path it is about is already in the field.
    const b = await openCard([], [], "/Users/me/Desktop/brandnew", DESKTOP);
    await typePath(b, "/Users/me/Desktop/brandnew");
    expect(newFolderRow(b).length).toBe(1);

    // It is the last row, so one ArrowUp from nothing lands on it.
    await press(b, "ArrowUp");
    expect(newFolderRow(b)[0].props["aria-selected"]).toBe(true);
    await press(b, "Enter");

    expect(pathField(b).props.value).toBe("/Users/me/Desktop/brandnew");
    expect(pathRowLabels(b)).toEqual([]);
  });
