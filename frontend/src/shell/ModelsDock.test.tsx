// The Models status-bar section's own presentational rules (D565/D566, D567):
// idle state, the chip's plain-text count and cost, the panel as a quick-info
// popover (one row, one Unload button, no gauge), and the same "quiet dot,
// never a forced expansion" contract the other two sections carry
// (`lib/autoExpand.ts`). Rendered through `ModelsCardView` — the pure,
// props-in half of this section, mirroring `DownloadManagerView`/
// `RepoUpdatesCardView` for the identical reason: no polling, no network, no
// `window`/`document`, so this file can render it directly with a fixed
// model list.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

import { ModelsCardView } from "@shell/ModelsDock";
import type { AiLoadedModel } from "@platform/lib/api";

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

const model = (over: Partial<AiLoadedModel> = {}): AiLoadedModel => ({
  model: "mlx-community/Qwen3-8B-MLX-4bit",
  capability: "text",
  runner: "mlx",
  state: "ready",
  detail: null,
  error: null,
  residentBytes: 4_000_000_000,
  footprintBytes: null,
  footprintBasis: null,
  device: "mps",
  loadedAt: 0,
  startedAt: 0,
  jobId: "",
  idleSeconds: 0,
  unloadsInSeconds: null,
  ...over,
});


/** D590: every chip carries the SAME circle (`StatusDot`) — outlined when the
 *  section holds nothing, filled (`.is-on`) when it holds something. Asserted
 *  through a helper so both states are always checked as one element's two
 *  forms, never as two elements that could drift apart. */
function circleFilled(tree: ReactTestRendererJSON | null): boolean {
  const dots = findAll(tree, "dl-dot");
  expect(dots).toHaveLength(1);
  return ((dots[0].props.className as string) ?? "").split(" ").includes("is-on");
}

function renderInstance(
  props: Partial<Parameters<typeof ModelsCardView>[0]> = {},
): ReactTestRenderer {
  return create(
    <ModelsCardView
      models={props.models ?? [model()]}
      collapsed={props.collapsed ?? false}
      onToggle={props.onToggle ?? (() => {})}
      onUnload={props.onUnload ?? (async () => {})}
    />,
  );
}

function renderView(
  props: Partial<Parameters<typeof ModelsCardView>[0]> = {},
): ReactTestRendererJSON | null {
  return renderInstance(props).toJSON() as ReactTestRendererJSON | null;
}

// D573 (user: "lets have simpler stuff like models (x count) | notifications
// | downloads etc and the no xyz part in the popover thing that opens", then
// "the chevron doesn't belong to the status bar. lets follow vscode/cursor
// for inspiration"): the chip is now ALWAYS a real button — idle sections
// included, VS Code/Cursor style, hover is the only affordance — and the
// idle sentence moved out of the chip into the panel it opens.
test("no models loaded still draws a real, clickable chip — just muted, and its panel holds the idle sentence", () => {
  const tree = renderView({ models: [], collapsed: false });
  expect(tree).not.toBeNull();
  const toggles = findAll(tree, "dl-toggle");
  expect(toggles).toHaveLength(1);
  expect(toggles[0].type).toBe("button");
  expect((toggles[0].props.className as string).split(" ")).toContain("is-idle");
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Models");
  // The idle sentence now lives in the panel, not the chip.
  expect(findAll(tree, "dl-idle")).toHaveLength(0);
  // D590 (user: "lets just stick to a circle for all items") reverses D588's
  // removal of the indicator from this chip alone: Models carries the same
  // circle as every other chip, outlined here because nothing is resident.
  expect(circleFilled(tree)).toBe(false);
  // The retired marks stay retired.
  expect(findAll(tree, "dl-zero")).toHaveLength(0);
  expect(findAll(tree, "dl-new-dot")).toHaveLength(0);
  expect(findAll(tree, "dl-count")).toHaveLength(0);
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No models loaded");
});

// D589 (user: "the memory gb next to the models isn't even accurate"): the
// chip is the bare label, full stop. The aggregate was a sum of
// `residentBytes` — "RSS of the worker process. Not the model's size", per
// api.ts's own comment on the field — so it under-reported MLX's allocator
// pool and over-reported shared pages. It is gone rather than corrected,
// because no arithmetic fixes a number measuring the wrong thing.
test("the chip is the bare label — no size, no count, whatever is resident", () => {
  for (const models of [
    [] as AiLoadedModel[],
    [model({ residentBytes: 4 * 1024 ** 3 })],
    [model(), model({ model: "org/other", residentBytes: 9 * 1024 ** 3 })],
    [model({ state: "loading", residentBytes: null })],
  ]) {
    const tree = renderView({ models });
    expect(text(findAll(tree, "dl-summary")[0])).toBe("Models");
  }
});

// `idle` keys off the ROW LIST now, not off a byte sum — which dissolves
// D588's ready-vs-loading problem instead of solving it again: with no size to
// fall back from, a bring-up and a resident model both just mean "there is
// something here", and only genuinely-nothing is muted.
test("muting tracks whether there are rows at all, not whether bytes were reported", () => {
  const nothing = renderView({ models: [] });
  expect((findAll(nothing, "dl-toggle")[0].props.className as string).split(" ")).toContain(
    "is-idle",
  );

  // A ready model whose runner reported no size, and a model mid-bring-up:
  // both used to be able to read as idle-but-unmuted. Both are simply "there
  // is something here" now.
  for (const models of [
    [model({ residentBytes: null })],
    [model({ state: "loading", residentBytes: null })],
  ]) {
    const tree = renderView({ models });
    expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).not.toContain(
      "is-idle",
    );
  }
});

// The panel is where cost still lives, and per-worker it is a real comparable
// figure even though the aggregate was not — so this row keeps its number
// (D589 deliberately left `.dl-amount` alone).
test("the panel row still reports that worker's own size", () => {
  const tree = renderView({ models: [model({ residentBytes: 4 * 1024 ** 3 })] });
  expect(text(findAll(findAll(tree, "dl-row")[0], "dl-amount")[0])).toBe("4.0 GB");
});

// A bring-up is legitimately listed in the panel, with its state standing in
// for the size it has not got yet (D588) — the bring-up's real progress is a
// job row in Jobs, via `supervisor._report`.
test("the panel lists a loading model, with its state where the size goes", () => {
  const tree = renderView({
    models: [model({ state: "downloading", residentBytes: null })],
  });
  expect(findAll(tree, "dl-panel-empty")).toHaveLength(0);
  const rows = findAll(tree, "dl-row");
  expect(rows).toHaveLength(1);
  expect(text(findAll(rows[0], "dl-amount")[0])).toBe("downloading");
});

test("collapsed shows no panel at all — no gauge, no rows, just the chip", () => {
  const tree = renderView({ collapsed: true });
  expect(findAll(tree, "dl-panel")).toHaveLength(0);
  expect(findAll(tree, "dl-row")).toHaveLength(0);
});

test("expanded draws one row per model — its name, its own resident bytes, an Unload button, no gauge", () => {
  const tree = renderView({
    models: [model({ model: "mlx-community/Qwen3-8B-MLX-4bit", residentBytes: 4_200_000_000 })],
  });
  const row = findAll(tree, "dl-row")[0];
  expect(text(findAll(row, "dl-title")[0])).toBe("Qwen3-8B-MLX-4bit"); // owner trimmed, matching repoName()
  expect(findAll(row, "dl-title")[0].props.title).toBe("mlx-community/Qwen3-8B-MLX-4bit");
  expect(text(findAll(row, "dl-amount")[0])).toBe("3.9 GB");
  expect(text(findAll(row, "dl-row-cancel")[0])).toBe("Unload");
  // No gauge, no progress fill — this is a quick-info popover (user call).
  expect(findAll(tree, "dl-bar")).toHaveLength(0);
});

test("pressing Unload calls onUnload with the model id and shows Unloading… mid-flight", async () => {
  const pending: Array<() => void> = [];
  const seen: string[] = [];
  const onUnload = (id: string) => {
    seen.push(id);
    return new Promise<void>((resolve) => pending.push(resolve));
  };
  const renderer = renderInstance({ onUnload });

  const before = renderer.toJSON() as ReactTestRendererJSON;
  const button = findAll(before, "dl-row-cancel")[0];
  act(() => {
    (button.props as { onClick: () => void }).onClick();
  });

  expect(seen).toEqual(["mlx-community/Qwen3-8B-MLX-4bit"]);
  const mid = renderer.toJSON() as ReactTestRendererJSON;
  expect(text(findAll(mid, "dl-row-cancel")[0])).toBe("Unloading…");
  expect(findAll(mid, "dl-row-cancel")[0].props.disabled).toBe(true);

  await act(async () => {
    pending.pop()?.();
  });
});

test("a failed unload says so in the panel — it does not fail silently", async () => {
  const onUnload = async () => {
    throw new Error("network down");
  };
  const renderer = renderInstance({ onUnload });
  const before = renderer.toJSON() as ReactTestRendererJSON;
  const button = findAll(before, "dl-row-cancel")[0];

  await act(async () => {
    (button.props as { onClick: () => void }).onClick();
  });

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-status")).toHaveLength(1);
  expect(text(findAll(after, "dl-status")[0]).length).toBeGreaterThan(0);
  // Recovered — the button reads Unload again, not stuck on Unloading…
  expect(text(findAll(after, "dl-row-cancel")[0])).toBe("Unload");
});

// D590: ONE circle, and it tracks "is there anything here" — never newness.
// `hasNew` is deleted from the hook app-wide, so no chip has an arrival mark
// any more; asserted as an absence so a future edit cannot reintroduce one
// alongside the circle, which is the ambiguity D588 removed.
test("the circle tracks whether anything is resident, and is the only indicator", () => {
  expect(circleFilled(renderView({ models: [] }))).toBe(false);
  expect(circleFilled(renderView({ models: [model()] }))).toBe(true);
  // A bring-up counts as "something here" — D589 keys this off the row list,
  // so there is no third state for a model without a reported size.
  expect(
    circleFilled(renderView({ models: [model({ state: "loading", residentBytes: null })] })),
  ).toBe(true);

  for (const models of [[], [model()], [model({ state: "loading", residentBytes: null })]]) {
    const tree = renderView({ models });
    expect(findAll(tree, "dl-new-dot")).toHaveLength(0);
    expect(findAll(tree, "dl-count")).toHaveLength(0);
  }
});

// The user's rule, verbatim: "no count. just a circle outlined or filled".
// Pinned as a property of the whole chip rather than of one state: nothing in
// the collapsed summary may render a digit, in any of the reachable cases.
test("no chip state renders a digit anywhere in the summary", () => {
  for (const models of [
    [] as AiLoadedModel[],
    [model()],
    [model(), model({ model: "org/b" }), model({ model: "org/c" })],
    [model({ state: "loading", residentBytes: null })],
  ]) {
    const tree = renderView({ models });
    expect(text(findAll(tree, "dl-summary")[0])).toBe("Models");
    expect(text(findAll(tree, "dl-toggle")[0])).not.toMatch(/[0-9]/);
  }
});

// D588 item 3: the ONLY treatments on this chip are `.is-idle`'s muting and
// the hover / `aria-expanded` wash. The failure tint moved to Notifications in
// D586 and must not be reachable here.
test("the failure tint cannot reach the Models chip", () => {
  for (const models of [[], [model()], [model({ state: "error", residentBytes: null })]]) {
    const tree = renderView({ models });
    expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).not.toContain(
      "is-failure",
    );
  }
});
