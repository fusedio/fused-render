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
  device: "mps",
  loadedAt: 0,
  startedAt: 0,
  jobId: "",
  idleSeconds: 0,
  unloadsInSeconds: null,
  ...over,
});

function renderInstance(
  props: Partial<Parameters<typeof ModelsCardView>[0]> = {},
): ReactTestRenderer {
  return create(
    <ModelsCardView
      models={props.models ?? [model()]}
      totalResidentBytes={
        "totalResidentBytes" in props ? (props.totalResidentBytes as number | null) : 4_000_000_000
      }
      collapsed={props.collapsed ?? false}
      hasNew={props.hasNew ?? false}
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

test("no models loaded draws the IDLE state, no chevron, nothing to press", () => {
  const tree = renderView({ models: [], totalResidentBytes: null });
  expect(tree).not.toBeNull();
  expect(text(findAll(tree, "dl-idle")[0])).toBe("No models loaded");
  expect(findAll(tree, "dl-toggle")).toHaveLength(0);
  expect(findAll(tree, "dl-chevron")).toHaveLength(0);
});

test("the chip is plain text — count and cost, no 'Models:' label prefix", () => {
  const tree = renderView({
    models: [model(), model({ model: "org/other-model" })],
    totalResidentBytes: 18 * 1024 ** 3, // formatSize's own base-1024 steps
  });
  const summary = text(findAll(tree, "dl-summary")[0]);
  expect(summary).toBe("2 models · 18 GB");
  expect(summary).not.toContain("Models");
});

test("singularizes one model, and omits the cost when nothing is measured", () => {
  const tree = renderView({ models: [model()], totalResidentBytes: null });
  expect(text(findAll(tree, "dl-summary")[0])).toBe("1 model");
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

test("a quiet dot marks an unacknowledged arrival — never rendered without one", () => {
  const quiet = renderView({ hasNew: false });
  expect(findAll(quiet, "dl-new-dot")).toHaveLength(0);

  const flagged = renderView({ hasNew: true });
  expect(findAll(flagged, "dl-new-dot")).toHaveLength(1);
});
