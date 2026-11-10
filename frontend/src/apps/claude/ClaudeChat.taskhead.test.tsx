// THE CHAT'S OWN HEAD ON THE SHARED CHASSIS (shell/TaskHead; design.md "One
// task head, everywhere"), behind its flag. With it on, the side pane, the
// canvas pane and the app page's pane draw ONE `TaskHead` where the
// `.c-anntools` strip and the identity line were: the hide-pane × leading the
// landing, ← Back to chats leading a chat (the × before it where the pane has
// no other close), the chat's ⋮ at the end — and full screen portals the whole
// head into the pane header's `.side-header-lead`, as it did the strip.
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";

const { ClaudeChat } = await import("./ClaudeChat");
const { resetFlagsForTests } = await import("./feature-flag");
const { createMemoryParamsStore } = await import("./params/store");
const { resetListingFeedForTests } = await import("@shell/tasksPulse");
const { publishTaskHeadUnified, resetTaskHeadUnifiedFlagForTests } = await import("@shell/task-head-flag");
const { setSideMaximized } = await import("@platform/lib/side-maximized-store");

const realFetch = globalThis.fetch;
const jsonRes = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as unknown as Response;
beforeEach(() => {
  resetListingFeedForTests();
  resetTaskHeadUnifiedFlagForTests();
  (globalThis as { fetch: unknown }).fetch = async (input: unknown): Promise<Response> => {
    const url = String(typeof input === "string" ? input : (input as { url: string }).url);
    // The landing's long poll never answers (ClaudeChat.boot.test.tsx says why).
    if (url.startsWith("/api/tasks/changes")) return new Promise<Response>(() => {});
    if (url.startsWith("/api/fs/stat")) {
      return jsonRes({ path: "/w/p", is_dir: true, templates: [{ mode: "claude", path: "/w/p/.claude/template.html" }] });
    }
    if (url === "/api/tasks") return jsonRes({ tasks: [] });
    if (url === "/api/schedule") return jsonRes({ entries: [] });
    if (url.startsWith("/api/claude-sessions/history")) return jsonRes({ turns: [] });
    return jsonRes({});
  };
});
const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  act(() => setSideMaximized(false));
  removePortalContainer();
  resetFlagsForTests();
  resetTaskHeadUnifiedFlagForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

const baseProps = { file: "/w/p", chatOnly: true, compact: false, peek: false, autoFocus: false } as const;

async function settle(ms = 20): Promise<void> {
  await act(async () => {
    await new Promise((done) => setTimeout(done, ms));
  });
}
async function mountChat(extra: Record<string, unknown> = {}, nodeMock?: (el: { type: unknown; props: Record<string, unknown> }) => unknown) {
  const params = createMemoryParamsStore();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(
      <ClaudeChat {...baseProps} params={params} {...extra} />,
      nodeMock ? { createNodeMock: nodeMock } : undefined,
    );
  });
  mounted.push(r);
  await settle();
  return r;
}
const hosts = (r: ReturnType<typeof create>, pick: (n: ReactTestInstance) => boolean) =>
  r.root.findAll((n) => typeof n.type === "string" && pick(n), { deep: true });
const byClass = (r: ReturnType<typeof create>, cls: string) =>
  hosts(r, (n) => String(n.props.className ?? "").split(/\s+/).includes(cls));
const byLabel = (r: ReturnType<typeof create>, label: string) => hosts(r, (n) => n.props["aria-label"] === label);

test("flag on, the landing: one shared head — the hide-pane × in the lead, the ⋮ at the end, no strip", async () => {
  publishTaskHeadUnified({ enabled: true });
  let hid = 0;
  const r = await mountChat({ annotateTarget: () => null, onHidePane: () => (hid += 1) });
  expect(byClass(r, "task-head")).toHaveLength(1);
  expect(byClass(r, "c-anntools")).toHaveLength(0);
  const lead = byClass(r, "task-head-lead")[0]!;
  expect(lead.props["aria-label"]).toBe("Hide the Claude panel");
  act(() => lead.props.onClick());
  expect(hid).toBe(1);
  // No ← on the landing: there is no chat to leave.
  expect(byLabel(r, "Back to chats")).toHaveLength(0);
  expect(byClass(r, "task-head-kebab")).toHaveLength(1);
  expect(byClass(r, "c-kebab")).toHaveLength(1);
});

test("flag on, a chat: ← Back to chats leads, the pane's × stays before it, no identity line of the chat's own", async () => {
  publishTaskHeadUnified({ enabled: true });
  const r = await mountChat({ onHidePane: () => {}, initialSessionId: "sess-abc12345" });
  const acts = byClass(r, "task-head-acts")[0]!;
  const buttons = acts.findAll((n) => n.type === "button");
  expect(buttons.map((b) => b.props["aria-label"])).toEqual(["Hide the Claude panel", "Back to chats"]);
  expect(byClass(r, "c-topbar")).toHaveLength(0);
  expect(byClass(r, "c-anntools")).toHaveLength(0);
  expect(byClass(r, "c-kebab")).toHaveLength(1);
});

test("flag off: the strip and its ← Chats, as always", async () => {
  const r = await mountChat({ onHidePane: () => {}, initialSessionId: "sess-abc12345" });
  expect(byClass(r, "task-head")).toHaveLength(0);
  expect(byClass(r, "c-anntools")).toHaveLength(1);
  expect(byClass(r, "c-back")).toHaveLength(1);
});

test("flag on, a host peek keeps the host's head: no shared head of the chat's own", async () => {
  publishTaskHeadUnified({ enabled: true });
  const r = await mountChat({ peek: true, initialSessionId: "sess-abc12345" });
  expect(byClass(r, "task-head")).toHaveLength(0);
});

test("flag on, full screen in a side pane: the whole head is portaled into `.side-header-lead`", async () => {
  installPortalContainer();
  publishTaskHeadUnified({ enabled: true });
  // A portal target as the test renderer takes one (testDomShim's body shape).
  const leadHost = {
    nodeType: 1,
    children: [] as Array<{ type?: string; props?: Record<string, unknown> }>,
    createNodeMock: () => null,
  };
  const pane = { querySelector: (sel: string) => (sel === ".side-header-lead" ? leadHost : null) };
  // Only the chat's ROOT is a node (the rest stay null, as in every other
  // mount here): it is the one the chat asks which pane it sits in.
  const node = (el: { type: unknown; props: Record<string, unknown> }) =>
    String(el.props.className ?? "").split(" ").includes("chat-root") ? rootNode() : null;
  const rootNode = () => ({
    closest: () => pane,
    querySelector: () => null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 600, height: 800, right: 600, bottom: 800 }),
    addEventListener() {},
    removeEventListener() {},
    contains: () => false,
    focus() {},
    scrollTo() {},
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {},
    removeAttribute() {},
    toggleAttribute() {},
    children: [],
    options: [],
    dataset: {},
    value: "",
    textContent: "",
    ownerDocument: (globalThis as { document?: unknown }).document,
    clientWidth: 600,
    clientHeight: 800,
    scrollHeight: 800,
    scrollTop: 0,
  });
  // Real nodes bring the measuring apparatus along; inert stand-ins, put back
  // after (bun runs every suite in one process).
  const g = globalThis as Record<string, unknown>;
  const MISSING = Symbol("missing");
  const inert = class {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  };
  const stubs: Record<string, unknown> = {
    ResizeObserver: inert,
    MutationObserver: inert,
    IntersectionObserver: inert,
    getComputedStyle: () => ({
      display: "block",
      position: "static",
      columnGap: "0",
      paddingLeft: "0",
      paddingRight: "0",
      getPropertyValue: () => "",
    }),
  };
  const was = new Map<string, unknown>();
  for (const [name, stub] of Object.entries(stubs)) {
    was.set(name, name in g ? g[name] : MISSING);
    g[name] = stub;
  }
  try {
  act(() => setSideMaximized(true));
  const r = await mountChat({ initialSessionId: "sess-abc12345" }, node);
  await settle();
  // Not in the column any more…
  const column = byClass(r, "c-chat")[0]!;
  expect(
    column.children.some(
      (c) => typeof c !== "string" && String(c.props.className ?? "").split(" ").includes("task-head"),
    ),
  ).toBe(false);
  // …but in the pane header's lead seat.
  const portaled = leadHost.children.find((c) => String(c.props?.className ?? "").split(" ").includes("task-head"));
  expect(portaled).toBeDefined();
  } finally {
    for (const r of mounted.splice(0)) act(() => r.unmount());
    for (const [name, prior] of was) {
      if (prior === MISSING) delete g[name];
      else g[name] = prior;
    }
  }
});
