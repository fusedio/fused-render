// The index-proposals status-bar section's own presentational rules
// (decision #8, SPEC-index-plugins.md). Rendered through
// `IndexProposalsCardView` — the pure, props-in half of this section,
// mirroring `ModelsCardView`/`RepoUpdatesCardView` for the identical reason:
// no polling, no network, so this file can render it directly with a fixed
// row list.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";
import IndexProposalsDock, { IndexProposalsCardView } from "@shell/IndexProposalsDock";
import { proposalRows } from "@shell/index-proposals-lib";
import type { IndexProposal } from "@platform/lib/api";

installDomShim();

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (typeof node.props?.className === "string" && node.props.className.split(" ").includes(className)) {
    hits.push(node);
  }
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, className));
  }
  return hits;
}

function text(node: ReactTestRendererJSON | null): string {
  if (node === null) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((c) => text(c as ReactTestRendererJSON)).join("");
}

const proposal = (over: Partial<IndexProposal> = {}): IndexProposal => ({
  folder: "/Users/me/Work/widget",
  kind: "widgets",
  ...over,
});

function renderInstance(
  props: Partial<Parameters<typeof IndexProposalsCardView>[0]> = {},
): ReactTestRenderer {
  return create(
    <IndexProposalsCardView
      rows={props.rows ?? proposalRows([proposal()])}
      busy={props.busy ?? null}
      collapsed={props.collapsed ?? false}
      onToggle={props.onToggle ?? (() => {})}
      onConfirm={props.onConfirm ?? (() => {})}
      onRefuse={props.onRefuse ?? (() => {})}
    />,
  );
}

function renderView(
  props: Partial<Parameters<typeof IndexProposalsCardView>[0]> = {},
): ReactTestRendererJSON | null {
  return renderInstance(props).toJSON() as ReactTestRendererJSON | null;
}

// The whole point of the pure-idle-state omission this view documents: unlike
// Models/Activity/Notifications there is no permanent slot in the bar for
// "nothing to confirm" — the chip must not exist at all rather than draw an
// idle sentence, since decision #8 is a gate that should not compete for
// attention when it has nothing to gate.
test("no pending proposals draws nothing at all", () => {
  const tree = renderView({ rows: [] });
  expect(tree).toBeNull();
});

test("a pending proposal draws a chip with a count and, when open, the folder name and kind", () => {
  const tree = renderView({ collapsed: true });
  const toggles = findAll(tree, "dl-toggle");
  expect(toggles).toHaveLength(1);
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Index");
  expect(text(findAll(tree, "sc-num")[0])).toBe("1");
  // Collapsed: no panel drawn at all.
  expect(findAll(tree, "dl-panel")).toHaveLength(0);

  const open = renderView({ collapsed: false });
  expect(findAll(open, "dl-panel")).toHaveLength(1);
  expect(text(findAll(open, "dl-title")[0])).toBe("widget");
  expect(text(findAll(open, "dl-status")[0])).toBe('"widgets" wants to index this folder');
});

test("Confirm fires onConfirm with the row's own folder", () => {
  const seen: string[] = [];
  const tree = renderInstance({
    collapsed: false,
    onConfirm: (folder) => seen.push(folder),
  });
  const nav = findAll(tree.toJSON() as ReactTestRendererJSON, "q-all")[0];
  act(() => {
    (nav.props as { onClick: () => void }).onClick();
  });
  expect(seen).toEqual(["/Users/me/Work/widget"]);
});

test("Refuse (the row's ✕) fires onRefuse with the row's own folder", () => {
  const seen: string[] = [];
  const tree = renderInstance({
    collapsed: false,
    onRefuse: (folder) => seen.push(folder),
  });
  const dismiss = findAll(tree.toJSON() as ReactTestRendererJSON, "dl-x")[0];
  expect(dismiss.props["aria-label"]).toBe("Refuse");
  act(() => {
    (dismiss.props as { onClick: () => void }).onClick();
  });
  expect(seen).toEqual(["/Users/me/Work/widget"]);
});

test("busy for a folder disables that row's Confirm/Refuse and relabels Confirm", () => {
  const tree = renderView({ collapsed: false, busy: "/Users/me/Work/widget" });
  const nav = findAll(tree, "q-all")[0];
  const dismiss = findAll(tree, "dl-x")[0];
  expect(text(nav)).toBe("Confirming…");
  expect(nav.props.disabled).toBe(true);
  expect(dismiss.props.disabled).toBe(true);
});

// -------------------------------------------- useIndexProposals's own poll --
//
// Review finding 7: `poll`'s `finally` self-schedules the next tick via
// `window.setTimeout` unconditionally, and `onConfirm`/`onRefuse` (the
// default-exported `IndexProposalsDock`) both call `refresh()` (the same
// `poll`) directly on top of whatever chain the mount effect already started.
// Before the fix, that left TWO live `setTimeout` chains running forever per
// confirm/refuse — doubling again on IndexManager's own mount of the same
// hook — with nothing that ever noticed or cancelled the extra one. The
// fixed `poll` clears its own previously-scheduled timeout at the top before
// doing anything else, so a manual `refresh()` collapses back onto the one
// chain the mount effect owns instead of forking a second.

/** Same technique as ActivityDock.test.tsx's own `captureTimers`, scoped to
 *  `window.setTimeout`/`clearTimeout` (what `useIndexProposals` calls, via
 *  `installDomShim`'s stub) rather than the real global — restored in a
 *  `finally` in every caller so no timer is left stubbed for a later file in
 *  the same `bun test` process. */
function captureWindowTimers(): { pendingCount: () => number; restore: () => void } {
  const pending = new Map<number, () => void>();
  let nextId = 1;
  const win = (globalThis as Record<string, unknown>).window as Record<string, unknown>;
  const realSetTimeout = win.setTimeout;
  const realClearTimeout = win.clearTimeout;
  win.setTimeout = ((fn: () => void) => {
    const id = nextId++;
    pending.set(id, fn);
    return id;
  }) as typeof globalThis.setTimeout;
  win.clearTimeout = ((id: number) => void pending.delete(id)) as typeof globalThis.clearTimeout;
  return {
    pendingCount: () => pending.size,
    restore: () => {
      win.setTimeout = realSetTimeout;
      win.clearTimeout = realClearTimeout;
    },
  };
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function okResponse(data: unknown): Response {
  return { ok: true, status: 200, json: async () => data } as unknown as Response;
}

test("a manual refresh() collapses onto the mount effect's own poll chain, not a second one", async () => {
  const timers = captureWindowTimers();
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string, opts?: { method?: string }) => {
    if (url === "/api/index/proposals") {
      return okResponse({ pending: [{ folder: "/Users/me/Work/widget", kind: "widgets" }], confirmed: [] });
    }
    if (url === "/api/index/proposals/confirm" && opts?.method === "POST") {
      return okResponse({ ok: true });
    }
    throw new Error(`unstubbed fetch: ${url}`);
  }) as typeof fetch;

  try {
    let tree!: ReactTestRenderer;
    await act(async () => {
      tree = create(<IndexProposalsDock />);
    });
    await flush();
    // The mount effect's own first poll has landed and scheduled exactly one
    // recurring tick.
    expect(timers.pendingCount()).toBe(1);

    // Collapsed by default (no pin, no hover) — open the panel so the row's
    // own Confirm button is in the tree at all.
    const toggle = findAll(tree.toJSON() as ReactTestRendererJSON, "dl-toggle")[0];
    act(() => {
      (toggle.props as { onClick: () => void }).onClick();
    });

    const nav = findAll(tree.toJSON() as ReactTestRendererJSON, "q-all")[0];
    await act(async () => {
      await (nav.props as { onClick: () => Promise<void> }).onClick();
    });
    await flush();

    // Confirming called `refresh()` on top of the effect's own chain. Before
    // the fix this left TWO pending timeouts (the original chain's next tick
    // plus a fresh one `refresh` started); the fix clears the outstanding one
    // at the top of every `poll` call, so exactly one survives.
    expect(timers.pendingCount()).toBe(1);
  } finally {
    globalThis.fetch = realFetch;
    timers.restore();
  }
});
