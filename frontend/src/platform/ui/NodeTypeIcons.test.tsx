// nodeTypeIcon: a canvas node's file picks the workbench's mark by its
// extension; anything else — including a name whose "extension" is an
// inherited object key — falls back to the explorer's plain file icon.
import { expect, test } from "bun:test";
import { HtmlNodeIcon, nodeTypeIcon, PythonNodeIcon } from "@platform/ui/NodeTypeIcons";
import { iconForEntry } from "@platform/ui/FileIcons";

// The explorer's plain file outline (a name with no extension).
const plainFile = () => iconForEntry("notes", false);

test("a known node extension picks the node mark", () => {
  expect(nodeTypeIcon("hex.py").type).toBe(PythonNodeIcon);
  expect(nodeTypeIcon("dashboard.HTML").type).toBe(HtmlNodeIcon);
});

test("an inherited key is not an icon: notes.__proto__ falls back to the plain file icon", () => {
  for (const name of ["notes.__proto__", "notes.constructor", "notes.toString"]) {
    expect(nodeTypeIcon(name)).toEqual(plainFile());
  }
});

test("a type the canvas has no mark for wears the explorer's icon", () => {
  expect(nodeTypeIcon("data.parquet")).toEqual(iconForEntry("data.parquet", false));
  expect(nodeTypeIcon("data.parquet")).not.toEqual(plainFile());
});
