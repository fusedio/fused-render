// A row whose feature this device lacks stays on the page: its controls are
// inert and dimmed, and the server's reason replaces the status line.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import type { Prefs } from "@platform/lib/api";
import { SettingRow, SettingsViewContext, type SettingsView } from "@shell/settings/SettingRow";

const view = (prefs: Partial<Prefs>): SettingsView => ({
  tokens: null,
  flashId: null,
  jump: () => undefined,
  prefs: prefs as Prefs,
});

async function mount(prefs: Partial<Prefs>) {
  let box!: ReactTestRenderer;
  await act(async () => {
    box = create(
      createElement(
        SettingsViewContext.Provider,
        { value: view(prefs) },
        createElement(SettingRow, {
          id: "ranked-search",
          status: "Ready",
          control: createElement("button", { id: "ctl" }, "go"),
          children: createElement("textarea", { id: "extra" }),
        }),
      ),
    );
  });
  return box;
}

const REASON = "Needs pip install 'fused-render[index]'";

test("an unavailable row is flagged, shows the reason, and makes its controls inert", async () => {
  const box = await mount({ unavailable: { index: REASON } });
  const row = box.root.findByProps({ "data-setting": "ranked-search" });
  expect(row.props["data-unavailable"]).toBe("true");
  expect(JSON.stringify(box.toJSON())).toContain(REASON);
  expect(JSON.stringify(box.toJSON())).not.toContain("Ready");
  const control = box.root.findByProps({ className: "settings-row-control opacity-55" });
  expect(control.props.inert).toBe("");
  const extra = box.root.findByProps({ className: "settings-row-extra opacity-55" });
  expect(extra.props.inert).toBe("");
  await act(async () => box.unmount());
});

test("an available row is untouched", async () => {
  const box = await mount({ unavailable: { hf: "elsewhere" } });
  const row = box.root.findByProps({ "data-setting": "ranked-search" });
  expect(row.props["data-unavailable"]).toBeUndefined();
  expect(JSON.stringify(box.toJSON())).toContain("Ready");
  expect(box.root.findAllByProps({ inert: "" }).length).toBe(0);
  await act(async () => box.unmount());
});
