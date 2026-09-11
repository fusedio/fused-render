// The badge's two faces, rendered: the idle row ("Check for updates") that the
// slot wears while there is nothing to report, and the accordion it hands the
// slot to when a check finds a version. The store is module-global and reads
// /api/config through fetch, so fetch is stubbed by URL — one stub for what the
// server currently says, another for what POST /api/update/check answers.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";
import type { UpdateStatus } from "@platform/lib/api";

const { default: UpdateBadge } = await import("./UpdateBadge");
const { pokeUpdateStatus, resetUpdateStatusForTests } = await import("@platform/lib/update-status");

function status(overrides: Partial<UpdateStatus>): UpdateStatus {
  return {
    state: "idle",
    method: "none",
    latest_version: null,
    progress: null,
    progress_total: null,
    error: null,
    manual_command: null,
    ...overrides,
  };
}

const realFetch = globalThis.fetch;
/** What GET /api/config carries as `update`, and what POST /api/update/check
 *  answers. Both are read by URL so a test can make the check FIND something
 *  the config poll had not yet seen. */
let configUpdate: UpdateStatus | null = status({});
let checkAnswer: () => UpdateStatus = () => status({});
const posts: string[] = [];
beforeEach(() => {
  posts.length = 0;
  (globalThis as { fetch: unknown }).fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (url.includes("/api/update/check")) {
      posts.push(init?.method ?? "GET");
      const answer = checkAnswer();
      // The store then re-polls config; make the two agree, the way the server does.
      configUpdate = answer;
      return new Response(JSON.stringify(answer), { headers: { "content-type": "application/json" } });
    }
    if (url.includes("/api/config")) {
      const body = configUpdate ? { version: "0.5.22", update: configUpdate } : { version: "0.5.22" };
      return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
});

const mounted: Array<ReturnType<typeof create>> = [];
async function mount(el: React.ReactElement) {
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(el);
  });
  await flush();
  mounted.push(r);
  return r;
}
async function flush() {
  await act(async () => {
    await new Promise((done) => setTimeout(done, 0));
  });
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  // The store is module-global: without this, one test's "available" is the
  // next test's starting state, and its 60s poll timer outlives the file.
  resetUpdateStatusForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

type Json = ReactTestRendererJSON;
function text(node: Json | Json[] | null): string {
  if (node === null) return "";
  if (Array.isArray(node)) return node.map(text).join("");
  return (node.children ?? []).map((c) => (typeof c === "string" ? c : text(c))).join("");
}
function find(node: Json | Json[] | null, cls: string): Json | null {
  if (node === null) return null;
  if (Array.isArray(node)) {
    for (const n of node) {
      const hit = find(n, cls);
      if (hit) return hit;
    }
    return null;
  }
  const own = String(node.props?.className ?? "").split(" ");
  if (own.includes(cls)) return node;
  for (const c of node.children ?? []) {
    if (typeof c === "string") continue;
    const hit = find(c, cls);
    if (hit) return hit;
  }
  return null;
}

test("with an updater and nothing to report, the slot offers a check", async () => {
  configUpdate = status({});
  const r = await mount(<UpdateBadge version="0.5.22" />);
  const row = find(r.toJSON(), "update-badge-row-check");
  expect(row).not.toBeNull();
  expect(text(row)).toBe("Check for updates");
  // Quiet: the accent dot means news, and this row is the absence of it.
  expect(find(r.toJSON(), "update-badge-dot")).toBeNull();
  // The refresh glyph sits where the accordion keeps its chevron.
  expect(find(row, "update-badge-refresh")).not.toBeNull();
  expect(find(row, "update-badge-chev")).toBeNull();
});

test("a press posts one check and reads the answer back as up to date, with the version", async () => {
  configUpdate = status({});
  checkAnswer = () => status({});
  const r = await mount(<UpdateBadge version="0.5.22" />);
  const row = find(r.toJSON(), "update-badge-row-check")!;
  await act(async () => {
    row.props.onClick();
  });
  await flush();
  expect(posts).toEqual(["POST"]);
  const after = find(r.toJSON(), "update-badge-row-check")!;
  expect(text(after)).toBe("Up to date · v0.5.22");
  // …and it is a button again, so a second press is possible once the answer
  // has been read (the four-second hold is a timer, not a lock).
  expect(after.props.disabled).toBe(false);
});

test("a check that finds a version hands the slot to the accordion, already open", async () => {
  configUpdate = status({});
  checkAnswer = () => status({ state: "available", latest_version: "9.9.9" });
  const r = await mount(<UpdateBadge version="0.5.22" />);
  await act(async () => {
    find(r.toJSON(), "update-badge-row-check")!.props.onClick();
  });
  await flush();
  const tree = r.toJSON();
  expect(find(tree, "update-badge-row-check")).toBeNull();
  const row = find(tree, "update-badge-row")!;
  expect(text(row)).toContain("Update available — v9.9.9");
  expect(row.props["aria-expanded"]).toBe(true);
  const action = find(tree, "update-badge-action")!;
  expect(text(action)).toBe("Update to v9.9.9");
});

test("a check-only manager gets the row and the sentence, not the Update button", async () => {
  configUpdate = status({ state: "available", latest_version: "9.9.9", check_only: true });
  const r = await mount(<UpdateBadge version="0.5.22" />);
  await act(async () => {
    find(r.toJSON(), "update-badge-row")!.props.onClick();
  });
  const tree = r.toJSON();
  expect(find(tree, "update-badge-action")).toBeNull();
  expect(text(find(tree, "update-badge-panel"))).toContain("no bundle to update");
});

test("a check the SERVER could not complete is a failed check, not up to date", async () => {
  // check() catches its own network errors and answers "idle" — the same state
  // as up to date — with the reason beside it. The row has to read the reason.
  configUpdate = status({});
  checkAnswer = () => status({ check_error: "no route to host" });
  const r = await mount(<UpdateBadge version="0.5.22" />);
  await act(async () => {
    find(r.toJSON(), "update-badge-row-check")!.props.onClick();
  });
  await flush();
  expect(text(find(r.toJSON(), "update-badge-row-check"))).toBe("Couldn't check");
});

test("a server already mid-fetch answers 'checking', and the row waits for the real answer", async () => {
  // A non-forced check() that lands while the auto tick's fetch is out returns
  // "checking" at once. That is a promise of an answer, not an answer, and the
  // row must not read it as "Up to date" (bugbot, PR #1097).
  configUpdate = status({});
  checkAnswer = () => status({ state: "checking" });
  const r = await mount(<UpdateBadge version="0.5.22" />);
  await act(async () => {
    find(r.toJSON(), "update-badge-row-check")!.props.onClick();
  });
  await flush();
  const mid = find(r.toJSON(), "update-badge-row-check")!;
  expect(text(mid)).toBe("Checking…");
  expect(mid.props.disabled).toBe(true);
  // The fetch ends; the next poll carries the durable answer.
  configUpdate = status({});
  await act(async () => {
    pokeUpdateStatus();
  });
  await flush();
  expect(text(find(r.toJSON(), "update-badge-row-check"))).toBe("Up to date · v0.5.22");
});

test("the badge shows exactly what the wire says — a 'checking' poll is the idle face", async () => {
  // The server keeps "available" on the wire while its tick re-checks (mac.py
  // `check()`), so the store holds nothing back: if the wire ever does say
  // "checking", that is a manager that was idle, and the idle face is right.
  configUpdate = status({ state: "available", latest_version: "9.9.9", check_only: true });
  const r = await mount(<UpdateBadge version="0.5.22" />);
  expect(text(find(r.toJSON(), "update-badge-row"))).toContain("Update available — v9.9.9");
  configUpdate = status({ state: "checking" });
  await act(async () => {
    pokeUpdateStatus();
  });
  await flush();
  expect(find(r.toJSON(), "update-badge-row-check")).not.toBeNull();
});

test("a failed check says so and does not throw", async () => {
  configUpdate = status({});
  checkAnswer = () => {
    throw new Error("offline");
  };
  const r = await mount(<UpdateBadge version="0.5.22" />);
  await act(async () => {
    find(r.toJSON(), "update-badge-row-check")!.props.onClick();
  });
  await flush();
  expect(text(find(r.toJSON(), "update-badge-row-check"))).toBe("Couldn't check");
});

test("no updater, no row: an unpackaged run without the dev manager shows nothing", async () => {
  configUpdate = null;
  const r = await mount(<UpdateBadge version="0.5.22" />);
  expect(r.toJSON()).toBeNull();
});
