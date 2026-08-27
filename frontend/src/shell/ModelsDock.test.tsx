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
  // D588: Models carries NO indicator — neither the circle Jobs and
  // Notifications show nor the old filled dot. Label, optional size, nothing.
  expect(findAll(tree, "dl-dot")).toHaveLength(0);
  expect(findAll(tree, "dl-zero")).toHaveLength(0);
  expect(findAll(tree, "dl-new-dot")).toHaveLength(0);
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No models loaded");
});

// D575 (user: "lets just have [model · memory size] and remove the count"):
// the label and the resident TOTAL, never a count. The size is the number
// being watched, and it is already the total across every resident model.
test("the chip names the category and the resident total — never a count", () => {
  const tree = renderView({
    models: [
      model({ residentBytes: 9 * 1024 ** 3 }),
      model({ model: "org/other-model", residentBytes: 9 * 1024 ** 3 }),
    ],
  });
  const summary = text(findAll(tree, "dl-summary")[0]);
  expect(summary).toBe("Models · 18 GB");
  // The count is the specific thing D575 removed — guard it by value, not by
  // the whole string, so a future size-format change cannot mask a regression.
  expect(summary).not.toContain("2");
});

// The whole point of dropping the count (D575): the chip reads IDENTICALLY
// for one resident model and for five, so nothing in it moves as models come
// and go — only the total changes.
test("one model and five models read the same, and only the total distinguishes them", () => {
  const one = renderView({ models: [model({ residentBytes: 4 * 1024 ** 3 })] });
  const five = renderView({
    models: [1, 2, 3, 4, 5].map((n) =>
      model({ model: `org/m${n}`, residentBytes: (4 * 1024 ** 3) / 5 }),
    ),
  });
  expect(text(findAll(one, "dl-summary")[0])).toBe("Models · 4.0 GB");
  expect(text(findAll(five, "dl-summary")[0])).toBe("Models · 4.0 GB");
});

// D588, THE LOADING GAP the user read as a confusing third state: a worker in
// venv/starting/downloading/loading carries `residentBytes: null`, so the chip
// used to fall back to the bare label while NOT being muted — visually a
// third state between "idle" and "resident". The chip now counts ready models
// only, so a bring-up reads exactly like idle (muted, no size) and the
// bring-up itself is visible in Jobs, where `supervisor._report` gives it a
// percentage and a cancel.
test("a model still loading leaves the chip in its idle state, not a third one", () => {
  const tree = renderView({
    models: [model({ state: "loading", residentBytes: null })],
  });
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Models");
  expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).toContain(
    "is-idle",
  );
});

// A READY worker whose runner reported no size must not produce that third
// state either — which is why the chip is keyed on the BYTES rather than on a
// ready count.
test("a ready model with no reported size also reads as idle", () => {
  const tree = renderView({ models: [model({ residentBytes: null })] });
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Models");
  expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).toContain(
    "is-idle",
  );
});

// The PANEL is a different question from the chip: a bring-up is legitimately
// listed there, with its state standing in for the size it has not got yet.
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

// SUPERSEDED BY D588 (user: "lets just remove the circle from models item",
// after "I see many different states for the model item ... this is
// confusing"). There is no arrival indicator on this chip at all now — and
// none app-wide: `hasNew` is deleted from the hook, since each chip shows one
// circle for "is there anything here" rather than a mark for "is it new".
// Asserted as an absence so a future edit cannot quietly reintroduce one.
test("Models renders no arrival indicator, whatever it holds", () => {
  for (const models of [[], [model()], [model({ state: "loading", residentBytes: null })]]) {
    const tree = renderView({ models });
    expect(findAll(tree, "dl-new-dot")).toHaveLength(0);
    expect(findAll(tree, "dl-dot")).toHaveLength(0);
    expect(findAll(tree, "dl-zero")).toHaveLength(0);
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
