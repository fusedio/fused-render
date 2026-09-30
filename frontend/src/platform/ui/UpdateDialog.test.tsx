// ONE DIALOG, ONE MODE NOW (D1; SPEC-update-notifications.md) — rendered, not
// asserted on as source. What is pinned here is everything a reader of the
// refresh dialog sees and everything that keeps it from being dismissed:
//
//   * same title, same sentence, same "Refresh page" button it has always had;
//   * `busy` is the chassis' "this cannot be closed from the chrome" lever: no
//     ✕ is rendered at all, and Esc/backdrop are refused.
//
// The restart mode's tests (the three-step strip, the stage clock, the live
// announcer) are DELETED along with the mode itself — the restart decision is
// now a status-bar notification (`UpdateNotifier.test.tsx`), not this dialog.
import {
  installDomShim,
  installPortalContainer,
  removePortalContainer,
} from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";

const { UpdateDialog } = await import("@platform/ui/UpdateDialog");

const mounted: Array<ReturnType<typeof create>> = [];
async function mount(el: React.ReactElement) {
  // The shared chassis portals into `document.body`, which the shim only
  // provides on request — see `installPortalContainer`. Per mount, because
  // other suites replace `globalThis.document` outright; and taken away again
  // below, because a body that merely exists changes what Base UI's popovers
  // do in every suite that runs after this one.
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

/** Every string in the subtree, joined — the dialog is prose, and asserting on
 *  it the way a reader takes it in (one run of text) is what catches a sentence
 *  split across elements as well as one that is simply missing. */
function text(node: ReactTestInstance): string {
  let out = "";
  const walk = (children: unknown[]) => {
    for (const c of children) {
      if (typeof c === "string") out += c;
      else if (typeof c === "number") out += String(c);
      else if (c && typeof c === "object" && "children" in (c as ReactTestInstance))
        walk((c as ReactTestInstance).children as unknown[]);
    }
  };
  walk(node.children as unknown[]);
  return out;
}
const byClass = (r: ReturnType<typeof create>, cls: string) =>
  r.root.findAll((n) => String(n.props?.className ?? "").split(" ").includes(cls));

test("refresh mode is exactly what it has always been", async () => {
  const r = await mount(
    <UpdateDialog kind="refresh" version="0.5.51" buildVersion="0.5.50" />,
  );
  expect(text(r.root.findByType("h2"))).toBe("fused-render updated to v0.5.51");
  expect(text(r.root.findByType("p"))).toBe(
    "This page is still on v0.5.50. Refresh to load the new version.",
  );
  const buttons = r.root.findAllByType("button");
  expect(buttons.length).toBe(1);
  expect(text(buttons[0])).toBe("Refresh page");
});

test("cannot be closed from the chrome", async () => {
  const r = await mount(<UpdateDialog kind="refresh" version="0.5.51" buildVersion="0.5.50" />);
  // `busy` drops the ✕ entirely — the chassis does not render it — and the
  // same flag is what makes `decideClose` answer "block" for Esc and for a
  // backdrop press. No ✕ in the tree IS the assertion that `busy` is set.
  expect(byClass(r, "modal-close").length).toBe(0);
  expect(r.root.findAll((n) => n.props?.["aria-modal"] === "true").length).toBe(1);
});
