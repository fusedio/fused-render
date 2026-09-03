// The chip's open/close rules (statusbar redesign): hover previews, click
// pins, second click / Escape / outside closes, and one chip open at a time.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useStatusChip, type StatusChipState } from "./statusChip";
import type { SectionKey } from "./exclusiveSection";

const ZERO = { openMs: 0, closeMs: 0 };

function Chip({
  sectionKey,
  initialPinned = false,
  onState,
}: {
  sectionKey: SectionKey;
  initialPinned?: boolean;
  onState: (s: StatusChipState) => void;
}) {
  const s = useStatusChip(sectionKey, initialPinned, ZERO);
  onState(s);
  return null;
}

function mount(element: React.ReactElement): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(element);
  });
  return renderer;
}

async function settle() {
  await act(async () => {
    await Promise.resolve();
  });
}

function harness(sectionKey: SectionKey, initialPinned = false) {
  let latest!: StatusChipState;
  const renderer = mount(
    <Chip sectionKey={sectionKey} initialPinned={initialPinned} onState={(s) => (latest = s)} />,
  );
  return {
    renderer,
    get state() {
      return latest;
    },
    enter: () => act(() => latest.hostProps.onPointerEnter()),
    leave: () => act(() => latest.hostProps.onPointerLeave()),
    click: () => act(() => latest.toggle()),
  };
}

test("starts closed and unpinned", () => {
  const h = harness("models");
  expect(h.state.open).toBe(false);
  expect(h.state.pinned).toBe(false);
  h.renderer.unmount();
});

test("hover opens a preview; leaving closes it again", async () => {
  const h = harness("models");
  h.enter();
  expect(h.state.open).toBe(true);
  expect(h.state.pinned).toBe(false);
  h.leave();
  expect(h.state.open).toBe(false);
  h.renderer.unmount();
});

test("a click pins: the panel survives the pointer leaving", async () => {
  const h = harness("activity");
  h.enter();
  h.click();
  expect(h.state.pinned).toBe(true);
  h.leave();
  expect(h.state.open).toBe(true);
  h.renderer.unmount();
});

test("a second click closes at once, pointer still on the chip", async () => {
  const h = harness("activity");
  h.enter();
  h.click();
  h.click();
  expect(h.state.open).toBe(false);
  expect(h.state.pinned).toBe(false);
  h.renderer.unmount();
});

test("a click with no hover (touch, keyboard) still pins open", () => {
  const h = harness("notifications");
  h.click();
  expect(h.state.open).toBe(true);
  expect(h.state.pinned).toBe(true);
  h.renderer.unmount();
});

test("close() unpins and closes together", () => {
  const h = harness("notifications", true);
  expect(h.state.open).toBe(true);
  act(() => h.state.close());
  expect(h.state.open).toBe(false);
  expect(h.state.pinned).toBe(false);
  h.renderer.unmount();
});

test("hovering a second chip closes a pinned first one — one panel at a time", async () => {
  let a!: StatusChipState;
  let b!: StatusChipState;
  const renderer = mount(
    <>
      <Chip sectionKey="models" onState={(s) => (a = s)} />
      <Chip sectionKey="activity" onState={(s) => (b = s)} />
    </>,
  );
  act(() => a.toggle());
  await settle();
  expect(a.pinned).toBe(true);
  act(() => b.hostProps.onPointerEnter());
  await settle();
  expect(b.open).toBe(true);
  expect(a.open).toBe(false);
  expect(a.pinned).toBe(false);
  renderer.unmount();
});
