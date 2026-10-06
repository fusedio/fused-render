// The blocking overlay shown from the moment a restart-to-update is requested
// until the new server answers and the page reloads. What is pinned here:
//   * nothing renders for `ready`/dismissed-`gave-up`;
//   * every in-flight stage renders the chassis' `busy` modal (no ✕, Esc and
//     backdrop refused) with a spinner and the "Restarting to update…" title;
//   * `gave-up` swaps the contents for an error with Dismiss and Try again.
import {
  installDomShim,
  installPortalContainer,
  removePortalContainer,
} from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, mock, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";

const { RestartOverlayView, overlayVisible } = await import("@platform/ui/RestartOverlay");

const mounted: Array<ReturnType<typeof create>> = [];
async function mount(el: React.ReactElement) {
  installPortalContainer();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  removePortalContainer();
});

function text(node: ReactTestInstance): string {
  let out = "";
  const walk = (children: unknown[]) => {
    for (const c of children) {
      if (typeof c === "string") out += c;
      else if (c && typeof c === "object" && "children" in (c as ReactTestInstance))
        walk((c as ReactTestInstance).children as unknown[]);
    }
  };
  walk(node.children as unknown[]);
  return out;
}
const byClass = (r: ReturnType<typeof create>, cls: string) =>
  r.root.findAll((n) => String(n.props?.className ?? "").split(" ").includes(cls));

test("overlayVisible: in-flight stages and an undismissed give-up only", () => {
  expect(overlayVisible("ready", false)).toBe(false);
  for (const s of ["quitting", "restarting", "reconnecting", "back"] as const) {
    expect(overlayVisible(s, false)).toBe(true);
  }
  expect(overlayVisible("gave-up", false)).toBe(true);
  expect(overlayVisible("gave-up", true)).toBe(false);
  expect(overlayVisible("stuck", false)).toBe(true);
  expect(overlayVisible("stuck", true)).toBe(false);
});

test("an in-flight restart blocks the window: busy modal, spinner, title", async () => {
  const r = await mount(
    <RestartOverlayView stage="quitting" onDismiss={() => {}} onRetry={() => {}} />,
  );
  expect(text(r.root.findByType("h2"))).toBe("Restarting to update…");
  expect(byClass(r, "restart-spinner").length).toBe(1);
  // `busy` drops the ✕ entirely: no way to close it from the chrome.
  expect(byClass(r, "modal-close").length).toBe(0);
  expect(r.root.findAll((n) => n.props?.["aria-modal"] === "true").length).toBe(1);
  expect(r.root.findAllByType("button").length).toBe(0);
});

test("the stage word is shown while waiting", async () => {
  const r = await mount(
    <RestartOverlayView stage="reconnecting" onDismiss={() => {}} onRetry={() => {}} />,
  );
  expect(text(r.root.findByType("p"))).toContain("Reconnecting…");
});

test("gave-up shows an error with Dismiss and Try again, no spinner", async () => {
  const onDismiss = mock(() => {});
  const onRetry = mock(() => {});
  const r = await mount(<RestartOverlayView stage="gave-up" onDismiss={onDismiss} onRetry={onRetry} />);
  expect(text(r.root.findByType("h2"))).toContain("didn't");
  expect(byClass(r, "restart-spinner").length).toBe(0);
  const labels = r.root.findAllByType("button").map((b) => text(b));
  expect(labels).toContain("Dismiss");
  expect(labels).toContain("Try again");
  const btn = (label: string) => r.root.findAllByType("button").find((b) => text(b) === label)!;
  act(() => btn("Try again").props.onClick());
  act(() => btn("Dismiss").props.onClick());
  expect(onRetry).toHaveBeenCalledTimes(1);
  expect(onDismiss).toHaveBeenCalledTimes(1);
});

test("stuck names the dropped press and is closable, with no spinner and no retry", async () => {
  const onDismiss = mock(() => {});
  const onRetry = mock(() => {});
  const r = await mount(<RestartOverlayView stage="stuck" onDismiss={onDismiss} onRetry={onRetry} />);
  expect(text(r.root.findByType("h2"))).toBe("Fused Render couldn't restart itself");
  expect(text(r.root.findByType("p"))).toContain("menu-bar icon");
  expect(text(r.root.findByType("p"))).toContain("⌘Q");
  expect(byClass(r, "restart-spinner").length).toBe(0);
  const labels = r.root.findAllByType("button").map((b) => text(b));
  expect(labels).toContain("Dismiss");
  expect(labels).not.toContain("Try again");
  const dismiss = r.root.findAllByType("button").find((b) => text(b) === "Dismiss")!;
  act(() => dismiss.props.onClick());
  expect(onDismiss).toHaveBeenCalledTimes(1);
  expect(onRetry).toHaveBeenCalledTimes(0);
});
