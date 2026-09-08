// AppVersionPicker: renders only once GET /api/git/app-folder confirms an
// enclosing app folder, lists the app folder's own recent commits, and
// writes `_snapshot` onto the page URL on selection ("Live" clears it).
//
// Driven through the REAL component (react-test-renderer, no DOM) rather than
// asserted on source text — this branch's earlier snapshot work was twice
// caught shipping a defect that a test hand-assigning "resolved" state let
// through (see DECISIONS-app-snapshot-preview.md, round 2): the actual
// resolve/gate code path has to run for a test to mean anything.
//
// `window`/`location`/`history` are the globals this component's own hooks
// touch (`useUrlVersion`'s `window.addEventListener`, `replaceSearch`'s
// `history.replaceState`) — installed once at file load, mirroring
// RepoUpdatesDock.test.tsx's own router.ts precedent (a real, unmocked
// router.ts import needs exactly these). Unlike that file, this one's tests
// actually exercise `window`/`history` rather than only needing the
// module-init pass through, so they are reset per-test rather than per-test
// torn down.
//
// BUT THEY ARE PUT BACK WHEN THE FILE IS DONE, and the stub EXTENDS the shared
// shim rather than standing in for it (`platform/lib/testDomShim.ts`, which
// states both rules). One `bun test` run is one process with one `globalThis`
// and no reset between files, so a stub carrying only the members THIS file
// needs is not local to this file at all: it hands every file that runs later
// a `window` that is truthy and half-missing, and `toast.ts`'s
// `window.setTimeout` and `router.ts`'s `window.dispatchEvent` then fail on
// code that has nothing to do with version picking. Which files run after this
// one is not a fact about this file either — it is bun's walk order, and
// merging a branch that only ADDS test files is enough to change it.
import { afterAll, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";
import { restoreGlobal } from "@platform/lib/testDomShim";

let currentUrl = { pathname: "/apps/repo/myapp", search: "" };
let replaced: string[] = [];
const listeners = new Map<string, Set<() => void>>();

const savedLocation = (globalThis as Record<string, unknown>).location;
const savedWindow = (globalThis as Record<string, unknown>).window;
const savedHistory = (globalThis as Record<string, unknown>).history;
const savedFetch = (globalThis as Record<string, unknown>).fetch;

(globalThis as Record<string, unknown>).location = currentUrl;
(globalThis as Record<string, unknown>).window = {
  ...(savedWindow as object),
  addEventListener: (ev: string, fn: () => void) => {
    if (!listeners.has(ev)) listeners.set(ev, new Set());
    listeners.get(ev)!.add(fn);
  },
  removeEventListener: (ev: string, fn: () => void) => {
    listeners.get(ev)?.delete(fn);
  },
};
(globalThis as Record<string, unknown>).history = {
  state: null,
  replaceState: (_state: unknown, _title: string, url: string) => {
    replaced.push(url);
    const [pathname, search] = url.split("?");
    currentUrl = { pathname, search: search ? "?" + search : "" };
    (globalThis as Record<string, unknown>).location = currentUrl;
    // main.tsx wraps the real history.replaceState to also dispatch
    // "fused:urlchange" (useUrlVersion's own signal) — replicated here, since
    // this fake stands in for that wrapper, not for the bare browser API.
    for (const fn of listeners.get("fused:urlchange") ?? []) fn();
  },
};

// Through `restoreGlobal`, not a bare `delete` and not a bare assignment: a
// global that was genuinely ABSENT before this file has to go back to absent,
// and one that was there has to go back to what it was.
afterAll(() => {
  restoreGlobal("location", savedLocation);
  restoreGlobal("window", savedWindow);
  restoreGlobal("history", savedHistory);
  restoreGlobal("fetch", savedFetch);
});

const { default: AppVersionPicker } = await import("@shell/AppVersionPicker");

// ---- fixtures ----------------------------------------------------------------

const APP_DIR = "/repo/myapp";
const COMMITS = [
  { sha: "a".repeat(40), short: "aaaaaaa", subject: "v2", author: "T", when: 200 },
  { sha: "b".repeat(40), short: "bbbbbbb", subject: "v1", author: "T", when: 100 },
];

type FetchPlan = {
  appFolder: "ok" | "404" | "error";
  commits: "ok" | "error";
  // Defaults to COMMITS.length (2) when omitted — pass a bigger number to
  // exercise a capped list (fewer commits returned than actually exist).
  total?: number;
};

function installFetch(plan: FetchPlan) {
  (globalThis as Record<string, unknown>).fetch = (async (url: string) => {
    const u = new URL(url, "http://x");
    if (u.pathname === "/api/git/app-folder") {
      if (plan.appFolder === "ok") {
        return {
          ok: true,
          json: async () => ({ ok: true, app_dir: APP_DIR }),
        };
      }
      if (plan.appFolder === "404") {
        return { ok: false, status: 404, json: async () => ({ error: "no app folder" }) };
      }
      throw new Error("network down");
    }
    if (u.pathname === "/api/git/commits") {
      if (plan.commits === "ok") {
        return {
          ok: true,
          json: async () => ({
            ok: true,
            commits: COMMITS,
            has_more: false,
            total: plan.total ?? COMMITS.length,
          }),
        };
      }
      return { ok: false, status: 502, json: async () => ({ error: "git exploded" }) };
    }
    throw new Error("unexpected fetch: " + url);
  }) as typeof fetch;
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function findSelect(
  node: ReactTestRendererJSON | ReactTestRendererJSON[] | null,
): ReactTestRendererJSON | null {
  if (node === null) return null;
  if (Array.isArray(node)) {
    for (const n of node) {
      const hit = findSelect(n);
      if (hit) return hit;
    }
    return null;
  }
  if (typeof node === "string") return null;
  if (node.type === "select") return node;
  for (const child of node.children ?? []) {
    const hit = findSelect(child as ReactTestRendererJSON);
    if (hit) return hit;
  }
  return null;
}

function options(select: ReactTestRendererJSON): string[] {
  return (select.children ?? [])
    .filter((c): c is ReactTestRendererJSON => typeof c !== "string")
    .map((o) => String(o.props.value));
}

// The option's rendered text ("v2 — subject") rather than its `value` (a
// sha) — a plain join of its children, all of which are strings/numbers for
// this component's option JSX.
function optionLabel(select: ReactTestRendererJSON, value: string): string {
  const opt = (select.children ?? [])
    .filter((c): c is ReactTestRendererJSON => typeof c !== "string")
    .find((o) => String(o.props.value) === value)!;
  return (opt.children ?? []).map((c) => String(c)).join("");
}

// Depth-first search by className — the closed-face label (task 7) is a
// plain `<span>` sitting beside the (now presentation-only) `<select>`, not
// a `type` any existing helper here already searches for.
function findByClassName(
  node: ReactTestRendererJSON | ReactTestRendererJSON[] | null,
  className: string,
): ReactTestRendererJSON | null {
  if (node === null) return null;
  if (Array.isArray(node)) {
    for (const n of node) {
      const hit = findByClassName(n, className);
      if (hit) return hit;
    }
    return null;
  }
  if (typeof node === "string") return null;
  const classes = String(node.props?.className ?? "").split(/\s+/);
  if (classes.includes(className)) return node;
  for (const child of node.children ?? []) {
    const hit = findByClassName(child as ReactTestRendererJSON, className);
    if (hit) return hit;
  }
  return null;
}

function textOf(node: ReactTestRendererJSON): string {
  return (node.children ?? []).map((c) => String(c)).join("");
}

let renderer: ReactTestRenderer | null = null;

beforeEach(() => {
  currentUrl = { pathname: "/apps/repo/myapp", search: "" };
  (globalThis as Record<string, unknown>).location = currentUrl;
  replaced = [];
  listeners.clear();
  renderer?.unmount();
  renderer = null;
});

// -------------------------------------------------------------------- the gate

test("the probe saying no app folder renders nothing", async () => {
  installFetch({ appFolder: "404", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  expect(renderer!.toJSON()).toBeNull();
});

test("a probe failure (network trouble, not a confirmed 404) also renders nothing", async () => {
  installFetch({ appFolder: "error", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  expect(renderer!.toJSON()).toBeNull();
});

test("an app folder that resolves renders the picker with Live selected", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON());
  expect(select).not.toBeNull();
  expect(select!.props.value).toBe("");
});

// ----------------------------------------------------------------- the commits

test("the app folder's commits render as options, newest first, as the server sent them", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  expect(options(select)).toEqual(["", COMMITS[0].sha, COMMITS[1].sha]);
});

test("the newest row is labelled v<total>, not a sha", async () => {
  installFetch({ appFolder: "ok", commits: "ok" }); // total defaults to COMMITS.length (2)
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  expect(optionLabel(select, COMMITS[0].sha)).toBe("v2 — " + COMMITS[0].subject);
  expect(optionLabel(select, COMMITS[1].sha)).toBe("v1 — " + COMMITS[1].subject);
  // The sha is not gone from the row — just not the label.
  expect(select.children!.find(
    (c): c is ReactTestRendererJSON =>
      typeof c !== "string" && String(c.props.value) === COMMITS[0].sha,
  )!.props.title).toBe(COMMITS[0].sha);
});

test("numbering stays right when the list is capped: the total, not the returned count, sets v<n>", async () => {
  // The server has 7 commits total; the picker only fetched (was capped to)
  // these 2 rows. Without a truthful `total`, the newest visible row would
  // wrongly read v2 (== the returned list's own length) instead of v7.
  installFetch({ appFolder: "ok", commits: "ok", total: 7 });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  expect(optionLabel(select, COMMITS[0].sha)).toBe("v7 — " + COMMITS[0].subject);
  expect(optionLabel(select, COMMITS[1].sha)).toBe("v6 — " + COMMITS[1].subject);
});

test("an empty repository (ok, zero commits, total 0) renders just Live", async () => {
  (globalThis as Record<string, unknown>).fetch = (async (url: string) => {
    const u = new URL(url, "http://x");
    if (u.pathname === "/api/git/app-folder") {
      return { ok: true, json: async () => ({ ok: true, app_dir: APP_DIR }) };
    }
    if (u.pathname === "/api/git/commits") {
      return {
        ok: true,
        json: async () => ({ ok: true, commits: [], has_more: false, total: 0 }),
      };
    }
    throw new Error("unexpected fetch: " + url);
  }) as typeof fetch;
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  expect(options(select)).toEqual([""]);
});

test("a failed commits fetch leaves the page live and pickable, not stuck loading", async () => {
  installFetch({ appFolder: "ok", commits: "error" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  expect(select).not.toBeNull();
  // Only "Live" — no commit rows, and no perpetual "Loading commits" hang.
  expect(options(select)).toEqual([""]);
  expect(select.props.value).toBe("");
});

// --------------------------------------------------------------------- select

test("selecting a commit writes it onto the URL as _snapshot", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  await act(async () => {
    select.props.onChange({ target: { value: COMMITS[0].sha } });
  });
  expect(replaced.length).toBe(1);
  expect(replaced[0]).toContain("_snapshot=" + COMMITS[0].sha);
});

test("the URL identity stays a sha even though the row reads v<n> — never a version number", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  // The option's own value (what a selection writes) is the 40-char sha, no
  // matter what its visible label says.
  expect(optionLabel(select, COMMITS[0].sha)).toBe("v2 — " + COMMITS[0].subject);
  await act(async () => {
    select.props.onChange({ target: { value: COMMITS[0].sha } });
  });
  expect(replaced[0]).toContain("_snapshot=" + COMMITS[0].sha);
  expect(replaced[0]).not.toContain("_snapshot=v2");
});

test('picking "Live" after a selection clears _snapshot from the URL', async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  currentUrl = { pathname: "/apps/repo/myapp", search: "?_snapshot=" + COMMITS[0].sha };
  (globalThis as Record<string, unknown>).location = currentUrl;
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  let select = findSelect(renderer!.toJSON())!;
  expect(select.props.value).toBe(COMMITS[0].sha);

  await act(async () => {
    select.props.onChange({ target: { value: "" } });
  });
  expect(replaced.length).toBe(1);
  expect(replaced[0]).not.toContain("_snapshot");

  await flush();
  select = findSelect(renderer!.toJSON())!;
  expect(select.props.value).toBe("");
});

// ---------------------------------------------------------- the closed face

test('the closed face shows "Live" when nothing is selected', async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const face = findByClassName(renderer!.toJSON(), "app-version-picker-face-label")!;
  expect(face).not.toBeNull();
  expect(textOf(face)).toBe("Live");
});

test("the closed face shows only the version number, never the subject", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  await act(async () => {
    select.props.onChange({ target: { value: COMMITS[0].sha } });
  });
  await flush();
  const face = findByClassName(renderer!.toJSON(), "app-version-picker-face-label")!;
  expect(textOf(face)).toBe("v2");
  // The separator the option label wears ("v2 — subject") never appears on
  // the closed face — this fixture's own subject happens to BE "v2", which
  // is exactly why an unqualified `.not.toContain(subject)` would prove
  // nothing here; the separator is the thing that actually distinguishes
  // "just the number" from "the full option text".
  expect(textOf(face)).not.toContain(" — ");
  // The option itself still carries the full text — this is presentation
  // only, exactly as the module's own top comment says.
  const optionText = optionLabel(select, COMMITS[0].sha);
  expect(optionText).toBe("v2 — " + COMMITS[0].subject);
});

test("a deep-linked sha outside the loaded list shows the short sha on the closed face too", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  const deepSha = "c".repeat(40);
  currentUrl = { pathname: "/apps/repo/myapp", search: "?_snapshot=" + deepSha };
  (globalThis as Record<string, unknown>).location = currentUrl;
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const face = findByClassName(renderer!.toJSON(), "app-version-picker-face-label")!;
  expect(textOf(face)).toBe(deepSha.slice(0, 7));
});

test("selecting still writes the sha with the new closed face in place", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  await act(async () => {
    select.props.onChange({ target: { value: COMMITS[0].sha } });
  });
  expect(replaced[0]).toContain("_snapshot=" + COMMITS[0].sha);
});

test("the accessible name is unchanged: aria-label stays on the real select, and the visible face is aria-hidden", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  expect(select.props["aria-label"]).toBe("App version");
  const face = findByClassName(renderer!.toJSON(), "app-version-picker-face-label")!;
  expect(String(face.props["aria-hidden"])).toBe("true");
});

test("a sha already on the URL that is not among the loaded commits still gets its own option, not a silent snap back to Live", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  const deepSha = "c".repeat(40);
  currentUrl = { pathname: "/apps/repo/myapp", search: "?_snapshot=" + deepSha };
  (globalThis as Record<string, unknown>).location = currentUrl;
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const select = findSelect(renderer!.toJSON())!;
  expect(select.props.value).toBe(deepSha);
  expect(options(select)).toContain(deepSha);
});

// ------------------------------------------------------- reads as a control
//
// FINDING 6 (code review): the real `<select>` is `opacity: 0` and only its
// own painted text was ever hidden by design (task 7) — but with no caret
// glyph on the closed face, the control has NOTHING sighted-mouse-user
// affordance saying "this opens a menu"; it reads as static text. A
// `:focus-within` ring is the other half of this finding, and is NOT
// assertable here: react-test-renderer has no CSS engine at all, so there is
// nothing in this file that could ever tell a real ring from none — that
// half is CSS-only and reviewed by reading the stylesheet, not tested here.
test("the closed face carries a caret glyph, so it reads as a control and not plain text", async () => {
  installFetch({ appFolder: "ok", commits: "ok" });
  await act(async () => {
    renderer = create(<AppVersionPicker dir={APP_DIR} />);
  });
  await flush();
  const caret = findByClassName(renderer!.toJSON(), "app-version-picker-caret");
  expect(caret).not.toBeNull();
  // Decorative only — the select above already carries the accessible name,
  // same reasoning as the face label itself.
  expect(String(caret!.props["aria-hidden"])).toBe("true");
});
