// Code review finding (PR #1104): `MessagePopupCard` passed `terminal` off
// `notification.tone` but no `status`, so — before NotificationCard's own
// fix — the glyph never rendered at all, and this card also lost the
// deleted `Toast.tsx`'s own `role={tone === "info" ? "status" : "alert"}`
// distinction entirely (no `role` was set here at all). Both are asserted
// against the REAL rendered output of the real store + real component, not
// against props passed to a mock.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { default: MessagePopupCard } = await import("@platform/ui/MessagePopupCard");
const { notify, _resetNotificationsForTest } = await import("@platform/lib/notifications");

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (
    typeof node.props?.className === "string" &&
    node.props.className.split(" ").includes(className)
  ) {
    hits.push(node);
  }
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, className));
  }
  return hits;
}

test("an error message pops role=alert and the terminal glyph; an info one pops role=status and no glyph", async () => {
  _resetNotificationsForTest();
  try {
    notify({ title: "Could not save", tone: "error" });
    let renderer: ReturnType<typeof create> | null = null;
    await act(async () => {
      renderer = create(<MessagePopupCard />);
    });
    let tree = renderer!.toJSON() as ReactTestRendererJSON;
    const errorRow = findAll(tree, "dl-row")[0];
    expect(errorRow.props.role).toBe("alert");
    expect(findAll(tree, "dl-status")).toHaveLength(1); // the glyph line
    await act(async () => {
      renderer!.unmount();
    });

    _resetNotificationsForTest();
    notify({ title: "Path copied", tone: "info" });
    await act(async () => {
      renderer = create(<MessagePopupCard />);
    });
    tree = renderer!.toJSON() as ReactTestRendererJSON;
    const infoRow = findAll(tree, "dl-row")[0];
    expect(infoRow.props.role).toBe("status");
    expect(findAll(tree, "dl-status")).toHaveLength(0); // no glyph for a non-error tone
    await act(async () => {
      renderer!.unmount();
    });
  } finally {
    _resetNotificationsForTest();
  }
});
