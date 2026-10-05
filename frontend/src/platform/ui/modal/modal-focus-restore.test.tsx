// PR #1336 review (Bugbot, "Focus restore targets the iframe"): when a Modal
// portals into ANOTHER document (`getContainer`), closing must return focus to
// the trigger in THIS script's document, not to the other document's
// `activeElement` (the <iframe> element).
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

import { Modal } from "./Modal";

type G = { document: Record<string, unknown> };
const g = globalThis as unknown as G;
let savedActive: unknown;

afterEach(() => {
  g.document.activeElement = savedActive;
  removePortalContainer();
});

function run(otherDoc: boolean) {
  installPortalContainer();
  const trigger = { focused: 0, focus() { this.focused++; } };
  const frame = { focused: 0, focus() { this.focused++; } };
  savedActive = g.document.activeElement;
  g.document.activeElement = trigger;
  const body = g.document.body as Record<string, unknown>;
  const container = { ...body };
  if (otherDoc) {
    container.ownerDocument = {
      activeElement: frame,
      body: container,
      addEventListener() {},
      removeEventListener() {},
    };
  }
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(
      <Modal title="t" onClose={() => {}} getContainer={() => container as unknown as Element}>
        <span>hi</span>
      </Modal>,
    );
  });
  act(() => r.unmount());
  return { trigger, frame };
}

test("cross-document modal restores focus to the trigger, not the other document's activeElement", () => {
  const { trigger, frame } = run(true);
  expect(trigger.focused).toBe(1);
  expect(frame.focused).toBe(0);
});

test("same-document modal still restores focus to the trigger", () => {
  const { trigger } = run(false);
  expect(trigger.focused).toBe(1);
});
