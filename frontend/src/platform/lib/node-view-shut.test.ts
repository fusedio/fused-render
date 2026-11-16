import { beforeEach, describe, expect, it } from "bun:test";
import { getNodeViewShut, setNodeViewShut } from "./node-view-shut";
import { _setUiLayoutForTest, getUiLayout, sanitizeUiLayout } from "./ui-layout";

// The node view's shut pane (SPEC §53.3 NV-4) lives in the server-held layout
// document (`node_view_shut`), read off ui-layout.ts's cache.
describe("node_view_shut", () => {
  beforeEach(() => _setUiLayoutForTest({ v: 1 }));

  it("is open until the reader shuts it", () => {
    expect(getNodeViewShut()).toBe(false);
    setNodeViewShut(true);
    expect(getNodeViewShut()).toBe(true);
    expect(getUiLayout().node_view_shut).toBe(true);
    setNodeViewShut(false);
    expect(getNodeViewShut()).toBe(false);
    // Opened again is a stored false, not a deleted key: an older bundle's
    // whole-document PUT then cannot bring back a stale `true`.
    expect(getUiLayout().node_view_shut).toBe(false);
  });

  it("reads a stored value back, and drops one that is not a boolean", () => {
    _setUiLayoutForTest({ v: 1, node_view_shut: true });
    expect(getNodeViewShut()).toBe(true);
    expect(sanitizeUiLayout({ v: 1, node_view_shut: "yes" }).node_view_shut).toBe(undefined);
  });

  it("leaves every other field of the document alone", () => {
    _setUiLayoutForTest({ v: 1, explorerSide: { hidden: true } });
    setNodeViewShut(true);
    expect(getUiLayout().explorerSide).toEqual({ hidden: true });
  });
});
