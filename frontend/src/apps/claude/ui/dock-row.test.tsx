// The dock row above the composer: it wraps the tasks bar, and renders nothing
// without one.
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { DockRow } from "./DockRow";

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

test("tasks present: the bar lives inside the one row", () => {
  const r = mount(<DockRow tasks={<section className="chat-tasks" />} />);
  const row = r.root.findByProps({ className: "c-dock-row" });
  expect(row.findAllByProps({ className: "chat-tasks" })).toHaveLength(1);
});

test("no tasks renders nothing", () => {
  expect(mount(<DockRow tasks={null} />).toJSON()).toBeNull();
});
