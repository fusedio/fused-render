// The dock row above the composer: tasks bar and Files pill share ONE row;
// the Files list is a popover (portaled, so its body is tested directly).
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { DockRow } from "./DockRow";
import { SessionFiles, SessionFilesList } from "./SessionFiles";

const mounted: ReactTestRenderer[] = [];
function mount(el: React.ReactElement): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

test("both present: tasks bar and Files pill live inside the one row", () => {
  const r = mount(
    <DockRow
      tasks={<section className="chat-tasks" />}
      files={<SessionFiles files={["/a/b.txt", "/c.md"]} />}
    />,
  );
  const row = r.root.findByProps({ className: "c-dock-row" });
  expect(row.findAllByProps({ className: "chat-tasks" })).toHaveLength(1);
  expect(row.findAllByProps({ "aria-expanded": false })).not.toHaveLength(0);
  expect(row.findByProps({ className: "art-chip-lbl" }).children.join("")).toBe("Files (2)");
  expect(row.findAllByProps({ className: "c-files-list" })).toHaveLength(0);
});

test("only one side present still renders the row; neither renders nothing", () => {
  expect(mount(<DockRow tasks={null} files={null} />).toJSON()).toBeNull();
  const only = mount(<DockRow tasks={null} files={<SessionFiles files={["/x"]} />} />);
  expect(only.root.findAllByProps({ className: "c-dock-row" })).toHaveLength(1);
  const none = mount(<SessionFiles files={[]} />);
  expect(none.toJSON()).toBeNull();
});

test("popover body lists name and dir, and a row press calls onPicked", () => {
  let picked = 0;
  const r = mount(<SessionFilesList files={["/a/b/c.txt", "d.md"]} onPicked={() => picked++} />);
  const rows = r.root.findAllByProps({ className: "c-files-row" });
  expect(rows).toHaveLength(2);
  expect(r.root.findAllByProps({ className: "c-files-name" }).map((n) => n.children.join(""))).toEqual([
    "c.txt",
    "d.md",
  ]);
  expect(r.root.findAllByProps({ className: "c-files-dir" })).toHaveLength(1);
});
