// A CALLER'S OWN HEAD (Modal `head`; the shared task head on the New task card's
// chat and the Cards popup): drawn in place of the chassis' `.modal-head`, and
// the dialog still named by its title — the id the chassis hands the head is
// the one `aria-labelledby` points at.
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { Modal } from "./Modal";

beforeEach(() => installPortalContainer());
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  removePortalContainer();
});

function render(el: React.ReactElement) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
const hosts = (r: ReactTestRenderer, pick: (n: ReactTestInstance) => boolean) =>
  r.root.findAll((n) => typeof n.type === "string" && pick(n));
const dialogOf = (r: ReactTestRenderer) => hosts(r, (n) => n.props.role === "dialog")[0]!;

test("with `head`, the chassis head is gone and the dialog is named by the head's title", () => {
  const r = render(
    <Modal title="ignored" onClose={() => {}} head={({ titleId }) => <header className="my-head"><span id={titleId}>TASK-042 title</span></header>}>
      <span>body</span>
    </Modal>,
  );
  expect(hosts(r, (n) => String(n.props.className ?? "").includes("modal-head deploy-head"))).toHaveLength(0);
  expect(hosts(r, (n) => n.props.className === "modal-head-slot")).toHaveLength(1);
  const labelled = dialogOf(r).props["aria-labelledby"] as string;
  expect(labelled).toBeTruthy();
  const title = hosts(r, (n) => n.props.id === labelled)[0]!;
  expect(title.children).toEqual(["TASK-042 title"]);
  // No ✕ of the chassis' own: the head brings its corner control.
  expect(hosts(r, (n) => n.props["aria-label"] === "Close")).toHaveLength(0);
});

test("without `head`, the head every caller has always had: the h2 the dialog is named by", () => {
  const r = render(
    <Modal title="Plain" onClose={() => {}}>
      <span>body</span>
    </Modal>,
  );
  expect(hosts(r, (n) => n.props.className === "modal-head-slot")).toHaveLength(0);
  const h2 = hosts(r, (n) => n.type === "h2")[0]!;
  expect(dialogOf(r).props["aria-labelledby"]).toBe(h2.props.id);
});

test("the head's `close` is the chassis' guarded close: a busy dialog does not close", async () => {
  let closes = 0;
  let close: () => void = () => {};
  const head = (ctx: { titleId: string; close: () => void }) => {
    close = ctx.close;
    return <header className="my-head"><span id={ctx.titleId}>t</span></header>;
  };
  render(
    <Modal title="t" busy onClose={() => (closes += 1)} head={head}>
      <span>body</span>
    </Modal>,
  );
  act(() => close());
  await new Promise((done) => setTimeout(done, 400));
  expect(closes).toBe(0);
  const idle = render(
    <Modal title="t" onClose={() => (closes += 1)} head={head}>
      <span>body</span>
    </Modal>,
  );
  void idle;
  act(() => close());
  await act(async () => {
    await new Promise((done) => setTimeout(done, 400));
  });
  expect(closes).toBe(1);
});
