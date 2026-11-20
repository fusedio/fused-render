// The New canvas dialog (D1497): one labeled field with the workbench's
// suggestion, the name rules judged against the LIVE listing, and a create
// whose failure stays in the dialog for a retry.
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

const { NewCanvasDialog } = await import("./NewCanvasDialog");

beforeEach(() => installPortalContainer());
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  removePortalContainer();
});

type Props = Parameters<typeof NewCanvasDialog>[0];
const NAMES = ["soil_moisture", "canvas_1", "Canvas_4"];

function dialog(over: Partial<Props> = {}) {
  const props: Props = {
    envLabel: "Production",
    canvasesDir: "~/Fused/canvas/prod",
    existingNames: NAMES,
    onCreate: async () => {},
    onClose: () => {},
    ...over,
  };
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<NewCanvasDialog {...props} />);
  });
  mounted.push(r);
  return { r, props };
}

const hosts = (r: ReactTestRenderer, pick: (n: ReactTestInstance) => boolean) =>
  r.root.findAll((n) => typeof n.type === "string" && pick(n));
const text = (n: ReactTestInstance): string =>
  n.children.map((c) => (typeof c === "string" ? c : text(c))).join("");
const input = (r: ReactTestRenderer) => hosts(r, (n) => n.props.id === "canvas-new-name")[0]!;
const button = (r: ReactTestRenderer, label: RegExp) =>
  hosts(r, (n) => n.type === "button" && label.test(text(n)))[0]!;
const createBtn = (r: ReactTestRenderer) => button(r, /^(Create & open|Creating…)$/);
const errorLine = (r: ReactTestRenderer) =>
  hosts(r, (n) => n.props.id === "canvas-new-error").map(text)[0] ?? null;
const type = (r: ReactTestRenderer, value: string) =>
  act(() => input(r).props.onChange({ target: { value } }));

test("opens on one labeled field holding the next canvas_<n>, ready to create", () => {
  const { r } = dialog();
  const field = input(r);
  // Past the highest canvas_<n>, case-insensitively (Canvas_4 counts).
  expect(field.props.value).toBe("canvas_5");
  const label = hosts(r, (n) => n.type === "label")[0]!;
  expect(label.props.htmlFor).toBe(field.props.id);
  expect(text(label)).toBe("Canvas name");
  // The dialog has exactly one text field: the confusion this replaced was two.
  expect(hosts(r, (n) => n.type === "input")).toHaveLength(1);
  expect(createBtn(r).props.disabled).toBe(false);
  expect(errorLine(r)).toBeNull();
});

test("says where the canvas goes, following the typed name", () => {
  const { r } = dialog();
  const where = () => text(hosts(r, (n) => n.props.className === "canvas-new-where")[0]!);
  expect(where()).toBe("Creates it in Production, clones it to ~/Fused/canvas/prod/canvas_5 and opens it.");
  type(r, "flood risk");
  expect(where()).toContain("~/Fused/canvas/prod/flood_risk");
});

test("without a clone folder the line names only the environment", () => {
  const { r } = dialog({ canvasesDir: null });
  const where = text(hosts(r, (n) => n.props.className === "canvas-new-where")[0]!);
  expect(where).toBe("Creates it in Production and opens it.");
});

test("typing is sanitized and a taken name blocks Create with the workbench's message", () => {
  const { r } = dialog();
  type(r, "Soil Moisture!");
  expect(input(r).props.value).toBe("Soil_Moisture");
  expect(errorLine(r)).toBe("A canvas with this name already exists");
  expect(input(r).props["aria-invalid"]).toBe("true");
  expect(createBtn(r).props.disabled).toBe(true);
  type(r, "");
  expect(errorLine(r)).toBe("Canvas name cannot be empty");
  type(r, "soil_moisture_2");
  expect(errorLine(r)).toBeNull();
  expect(createBtn(r).props.disabled).toBe(false);
});

test("a canvas that appears in the listing while open is caught before Create", () => {
  const { r, props } = dialog();
  expect(createBtn(r).props.disabled).toBe(false);
  act(() => r.update(<NewCanvasDialog {...props} existingNames={[...NAMES, "CANVAS_5"]} />));
  expect(errorLine(r)).toBe("A canvas with this name already exists");
  expect(createBtn(r).props.disabled).toBe(true);
});

test("while the listing reloads Create waits", () => {
  const { r } = dialog({ existingNames: null });
  expect(input(r).props.value).toBe("canvas_1");
  expect(errorLine(r)).toBe("Loading canvases…");
  expect(createBtn(r).props.disabled).toBe(true);
});

test("Create hands over the trimmed name and stays busy while the page moves", async () => {
  const calls: string[] = [];
  let finish!: () => void;
  const { r } = dialog({
    onCreate: (name) => {
      calls.push(name);
      return new Promise<void>((res) => (finish = res));
    },
  });
  type(r, "flood_risk");
  await act(async () => createBtn(r).props.onClick());
  expect(calls).toEqual(["flood_risk"]);
  expect(text(createBtn(r))).toBe("Creating…");
  expect(createBtn(r).props.disabled).toBe(true);
  expect(button(r, /^Cancel$/).props.disabled).toBe(true);
  expect(input(r).props.disabled).toBe(true);
  // A second press while in flight does nothing.
  await act(async () => createBtn(r).props.onClick());
  expect(calls).toHaveLength(1);
  await act(async () => finish());
  expect(text(createBtn(r))).toBe("Creating…");
});

test("Enter in the field creates", async () => {
  const calls: string[] = [];
  const { r } = dialog({ onCreate: async (n) => void calls.push(n) });
  await act(async () => input(r).props.onKeyDown({ key: "Enter", preventDefault() {} }));
  expect(calls).toEqual(["canvas_5"]);
});

test("a failed create shows its message in the dialog and allows a retry", async () => {
  let attempts = 0;
  const { r } = dialog({
    onCreate: async () => {
      attempts += 1;
      throw new Error("fused workbench canvas create failed: 403");
    },
  });
  await act(async () => createBtn(r).props.onClick());
  expect(errorLine(r)).toBe("fused workbench canvas create failed: 403");
  expect(text(createBtn(r))).toBe("Create & open");
  expect(createBtn(r).props.disabled).toBe(false);
  await act(async () => createBtn(r).props.onClick());
  expect(attempts).toBe(2);
  // Editing the name clears the old failure.
  type(r, "canvas_6");
  expect(errorLine(r)).toBeNull();
});

test("Cancel closes", () => {
  let closed = 0;
  const { r } = dialog({ onClose: () => (closed += 1) });
  act(() => button(r, /^Cancel$/).props.onClick());
  expect(closed).toBe(1);
});
