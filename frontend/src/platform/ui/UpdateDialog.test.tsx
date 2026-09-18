// ONE DIALOG, TWO MODES (D1) — rendered, not asserted on as source. What is
// pinned here is everything a reader of the dialog sees and everything that
// keeps it from being dismissed:
//
//   * the refresh mode is UNCHANGED (step 6) — same title, same sentence, same
//     "Refresh page" button it has always had, now living in the shared
//     component;
//   * the restart mode names the version WAITING in its title and the version
//     STILL RUNNING in its body, which is the one distinction the whole flow
//     turns on;
//   * the footer is the interaction: a button before the press, and the stage
//     word IN ITS PLACE afterwards — never both;
//   * `busy` on both, which is the chassis' "this cannot be closed from the
//     chrome" lever: no ✕ is rendered at all, and Esc/backdrop are refused.
import {
  installDomShim,
  installPortalContainer,
  removePortalContainer,
} from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";

const { UpdateDialog } = await import("@platform/ui/UpdateDialog");
const { RESTART_STAGES, restartStageLabel } = await import("@platform/lib/restart-flow");

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

test("restart mode names the waiting version in the title and the running one in the body", async () => {
  const r = await mount(
    <UpdateDialog
      kind="restart"
      version="0.5.50"
      installedVersion="0.5.51"
      stage="ready"
      onRestart={() => {}}
    />,
  );
  // The title is about what is READY — a restart is worth the interruption
  // because of the version you are about to get, not the one you have.
  expect(text(r.root.findByType("h2"))).toBe("fused-render v0.5.51 is ready");
  expect(text(r.root.findByType("p"))).toBe(
    "The app is still running v0.5.50. Restart to finish the update.",
  );
});

test("the button is the only control, and pressing it is the whole interaction", async () => {
  const presses: number[] = [];
  const r = await mount(
    <UpdateDialog
      kind="restart"
      version="0.5.50"
      installedVersion="0.5.51"
      stage="ready"
      onRestart={() => presses.push(1)}
    />,
  );
  const buttons = r.root.findAllByType("button");
  expect(buttons.length).toBe(1);
  expect(text(buttons[0])).toBe("Restart fused-render");
  await act(async () => {
    buttons[0].props.onClick();
  });
  expect(presses.length).toBe(1);
});

test("the stage word REPLACES the button, so the press has no second meaning", async () => {
  for (const stage of ["quitting", "restarting", "reconnecting", "back"] as const) {
    const r = await mount(
      <UpdateDialog
        kind="restart"
        version="0.5.50"
        installedVersion="0.5.51"
        stage={stage}
        onRestart={() => {}}
      />,
    );
    expect(r.root.findAllByType("button").length).toBe(0);
    const line = byClass(r, "update-dialog-stage");
    expect(line.length).toBe(1);
    expect(text(line[0])).toBe(restartStageLabel(stage));
    // Announced as text rather than as a whole dialog re-read: only this line
    // changes between stages.
    expect(line[0].props["aria-live"]).toBe("polite");
    expect(line[0].props.role).toBe("status");
  }
});

test("the cap leaves a button, not an empty footer", async () => {
  // bugbot, PR #1214. `gave-up` can be on screen — a restart that did not take
  // leaves the server answering with the disk still ahead, so the dialog stays
  // up (see `bannerSurface`) — and it is the stage a reader most needs a
  // control on. Drawing the (empty) stage line there left a page-blocking
  // dialog with no control at all.
  const presses: number[] = [];
  const r = await mount(
    <UpdateDialog
      kind="restart"
      version="0.5.50"
      installedVersion="0.5.51"
      stage="gave-up"
      onRestart={() => presses.push(1)}
    />,
  );
  const buttons = r.root.findAllByType("button");
  expect(buttons.length).toBe(1);
  expect(text(buttons[0])).toBe("Restart fused-render");
  expect(byClass(r, "update-dialog-stage").length).toBe(0);
  await act(async () => {
    buttons[0].props.onClick();
  });
  expect(presses.length).toBe(1);
});

test("every stage the machine can reach renders something a reader can act on", async () => {
  // A stage added to `RESTART_STAGES` without a face here is a dialog with an
  // empty footer — a page-blocking dialog with nothing in it, which is the
  // failure this guards. No exceptions any more: `gave-up` used to be one.
  for (const stage of RESTART_STAGES) {
    const r = await mount(
      <UpdateDialog
        kind="restart"
        version="0.5.50"
        installedVersion="0.5.51"
        stage={stage}
        onRestart={() => {}}
      />,
    );
    const footer = byClass(r, "modal-footer")[0];
    expect(text(footer).length).toBeGreaterThan(0);
  }
});

test("neither mode can be closed from the chrome", async () => {
  for (const el of [
    <UpdateDialog key="r" kind="refresh" version="0.5.51" buildVersion="0.5.50" />,
    <UpdateDialog
      key="s"
      kind="restart"
      version="0.5.50"
      installedVersion="0.5.51"
      stage="ready"
      onRestart={() => {}}
    />,
  ]) {
    const r = await mount(el);
    // `busy` drops the ✕ entirely — the chassis does not render it — and the
    // same flag is what makes `decideClose` answer "block" for Esc and for a
    // backdrop press. No ✕ in the tree IS the assertion that `busy` is set.
    expect(byClass(r, "modal-close").length).toBe(0);
    expect(r.root.findAll((n) => n.props?.["aria-modal"] === "true").length).toBe(1);
  }
});

test("the button cannot be pressed while the window is still verifying a record", async () => {
  // bugbot, PR #1214: `wake()` is async, so a window that found a restart record
  // paints `ready` until the server answers. A press in that gap would start a
  // SECOND restart on top of the one being verified.
  const presses: number[] = [];
  const r = await mount(
    <UpdateDialog
      kind="restart"
      version="0.5.50"
      installedVersion="0.5.51"
      stage="ready"
      verifying
      onRestart={() => presses.push(1)}
    />,
  );
  const button = r.root.findAllByType("button")[0];
  expect(button.props.disabled).toBe(true);
  // The box and the label stay, so the footer does not move under the cursor for
  // the fraction of a second the request takes.
  expect(text(button)).toBe("Restart fused-render");
  expect(byClass(r, "update-dialog-stage").length).toBe(0);
});

test("the button is live again once the check has answered", async () => {
  const presses: number[] = [];
  const r = await mount(
    <UpdateDialog
      kind="restart"
      version="0.5.50"
      installedVersion="0.5.51"
      stage="ready"
      verifying={false}
      onRestart={() => presses.push(1)}
    />,
  );
  const button = r.root.findAllByType("button")[0];
  expect(button.props.disabled).toBe(false);
  await act(async () => {
    button.props.onClick();
  });
  expect(presses.length).toBe(1);
});
