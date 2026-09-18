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
//   * the footer is the interaction: a button before the press, and the
//     THREE-STEP STRIP in its place afterwards — never both, except on
//     `gave-up`, where the strip is the record of a wait that failed and the
//     button is the only way out;
//   * which step is ticked, which is live and which is still to come, for every
//     stage the machine can reach, plus the sentence each stage puts under the
//     title — including the one that withdraws the "15 seconds" estimate once
//     the wait has outrun it;
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
const { RESTART_SLOW_MS, RESTART_STAGES, restartStageLabel } = await import(
  "@platform/lib/restart-flow"
);

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

/** The strip as a reader takes it in: one entry per step, in order, carrying
 *  the word it shows and the state its class claims. Read off the rendered tree
 *  rather than off `restartSteps` — the point of these tests is that the
 *  component draws what the pure function decided, so re-deriving it here would
 *  assert nothing. */
function steps(r: ReturnType<typeof create>): Array<{ state: string; word: string }> {
  return byClass(r, "update-dialog-step").map((n) => {
    const cls = String(n.props.className).split(" ");
    const state = cls.find((c) => c.startsWith("is-"))?.slice(3) ?? "";
    return { state, word: text(byClass_in(n, "update-dialog-step-word")[0]!) };
  });
}
const byClass_in = (node: ReactTestInstance, cls: string) =>
  node.findAll((n) => String(n.props?.className ?? "").split(" ").includes(cls));

/** How many spinners the strip is running. Exactly one while a step is live,
 *  and ZERO on `back` and `gave-up` — a spinner on either is a promise the
 *  flow has already stopped being able to keep. */
const spinners = (r: ReturnType<typeof create>) => byClass(r, "update-spinner").length;

const body = (r: ReturnType<typeof create>) => text(r.root.findByType("p"));

/** A restart dialog at a stage, with the press placed `elapsed` ms ago and the
 *  clock frozen — the seam the 25s sentence is tested through. */
function restartAt(
  stage: (typeof RESTART_STAGES)[number],
  opts: { elapsed?: number; onRestart?: () => void } = {},
) {
  const now = 1_700_000_000_000;
  return (
    <UpdateDialog
      kind="restart"
      version="0.5.96"
      installedVersion="0.5.97"
      stage={stage}
      requestedAt={now - (opts.elapsed ?? 0)}
      now={now}
      onRestart={opts.onRestart ?? (() => {})}
    />
  );
}

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

test("the strip REPLACES the button, so the press has no second meaning", async () => {
  for (const stage of ["quitting", "restarting", "reconnecting", "back"] as const) {
    const r = await mount(restartAt(stage));
    expect(r.root.findAllByType("button").length).toBe(0);
    const strip = byClass(r, "update-dialog-steps");
    expect(strip.length).toBe(1);
    // Announced as text rather than as a whole dialog re-read: only the strip
    // changes between stages, and one region for all three steps means one
    // announcement per transition rather than three.
    expect(strip[0].props["aria-live"]).toBe("polite");
    expect(strip[0].props.role).toBe("status");
  }
});

test("each in-flight stage ticks what is done, spins what is live and leaves the rest hollow", async () => {
  // THE WHOLE POINT OF THE STRIP. One word said what the app was doing; this
  // says how far along it is, and these three rows are that claim in full.
  const quitting = await mount(restartAt("quitting"));
  expect(steps(quitting)).toEqual([
    { state: "live", word: "Quitting…" },
    { state: "upcoming", word: "Restart" },
    { state: "upcoming", word: "Reconnect" },
  ]);
  expect(spinners(quitting)).toBe(1);

  const restarting = await mount(restartAt("restarting"));
  expect(steps(restarting)).toEqual([
    { state: "done", word: "Quit" },
    { state: "live", word: "Restarting…" },
    { state: "upcoming", word: "Reconnect" },
  ]);
  expect(spinners(restarting)).toBe(1);

  const reconnecting = await mount(restartAt("reconnecting"));
  expect(steps(reconnecting)).toEqual([
    { state: "done", word: "Quit" },
    { state: "done", word: "Restart" },
    { state: "live", word: "Reconnecting…" },
  ]);
  expect(spinners(reconnecting)).toBe(1);

  // The live word is the machine's own label, not a second copy of it here.
  expect(steps(restarting)[1]!.word).toBe(restartStageLabel("restarting"));
});

test("the same-version blip simply un-ticks — nothing animates backwards", async () => {
  // `reduceRestart` sends `reconnecting` back to `quitting` when the old process
  // answers on the version that was already running. The strip is derived from
  // the stage every render, so that regression costs one re-render and no
  // memory of a furthest point that would have to be unwound.
  const back = await mount(restartAt("quitting"));
  expect(steps(back).map((s) => s.state)).toEqual(["live", "upcoming", "upcoming"]);
});

test("back ticks all three and tints, with nothing left spinning", async () => {
  const r = await mount(restartAt("back"));
  expect(steps(r).map((s) => s.state)).toEqual(["done", "done", "done"]);
  expect(steps(r).map((s) => s.word)).toEqual(["Quit", "Restart", "Reconnect"]);
  expect(spinners(r)).toBe(0);
  // The success tint is a class, not an inline colour: the token lives in the
  // stylesheet (notifications.css) and must stay the app's own --success.
  expect(String(byClass(r, "update-dialog-steps")[0]!.props.className)).toContain("is-back");
});

test("the strip runs no clock and paints no strip before the press", async () => {
  const r = await mount(restartAt("ready"));
  expect(byClass(r, "update-dialog-steps").length).toBe(0);
  expect(spinners(r)).toBe(0);
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
  await act(async () => {
    buttons[0].props.onClick();
  });
  expect(presses.length).toBe(1);
});

test("gave-up keeps the strip, greyed and still, beside the button", async () => {
  // The strip is the RECORD of the wait that failed; dropping it for a bare
  // button would erase it. But no step may claim to have finished — the restart
  // demonstrably did not — and above all nothing may still be spinning, which is
  // the forever-promise the cap exists to stop making.
  const r = await mount(restartAt("gave-up"));
  const strip = byClass(r, "update-dialog-steps");
  expect(strip.length).toBe(1);
  expect(String(strip[0]!.props.className)).toContain("is-stalled");
  expect(steps(r).map((s) => s.state)).toEqual(["upcoming", "upcoming", "upcoming"]);
  expect(steps(r).map((s) => s.word)).toEqual(["Quit", "Restart", "Reconnect"]);
  expect(spinners(r)).toBe(0);
  // Both in the SAME footer row — the stacking that would grow the dialog is
  // what `margin-right: auto` on the strip exists to avoid.
  expect(r.root.findAllByType("button").length).toBe(1);
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

// ---- the sentence under the title ------------------------------------------

test("the body names which version is closing and which is starting", async () => {
  // PROSE, deliberately: the one-word rule (2026-09-08) governs the step NAMES
  // in the strip. A body forbidden to name a version could not say the one thing
  // the dialog is up about.
  const r = await mount(restartAt("restarting"));
  expect(body(r)).toBe(
    "Closing v0.5.96 and starting v0.5.97. This page comes back on its own — usually in about 15 seconds.",
  );
});

test("the estimate is withdrawn once the wait has outrun it, in the same slot", async () => {
  const justUnder = await mount(restartAt("restarting", { elapsed: RESTART_SLOW_MS - 1 }));
  expect(body(justUnder)).toContain("about 15 seconds");

  const atTheMark = await mount(restartAt("restarting", { elapsed: RESTART_SLOW_MS }));
  expect(body(atTheMark)).toBe("Taking a little longer than usual — still working on it.");
  // ONE sentence either way — the slot does not gain a line, so the dialog does
  // not move under a reader who is already waiting.
  expect(atTheMark.root.findAllByType("p").length).toBe(1);
  // …and the strip is untouched by it: the machine is still in `restarting`.
  expect(steps(atTheMark).map((s) => s.state)).toEqual(["done", "live", "upcoming"]);
});

test("a wait with no press behind it never reads as overlong", async () => {
  // `requestedAt` is null in the window that has adopted nothing yet. Elapsed
  // time is unknowable there, and guessing would print the anxious sentence over
  // a restart that started a second ago.
  const r = await mount(
    <UpdateDialog
      kind="restart"
      version="0.5.96"
      installedVersion="0.5.97"
      stage="restarting"
      requestedAt={null}
      now={1_700_000_000_000}
      onRestart={() => {}}
    />,
  );
  expect(body(r)).toContain("about 15 seconds");
});

test("back and gave-up each say what happened, not what is happening", async () => {
  expect(body(await mount(restartAt("back")))).toBe("Back on v0.5.97 — reloading…");
  // Never "taking longer": the wait is over either way, and `back` in
  // particular is a beat from a reload.
  expect(body(await mount(restartAt("back", { elapsed: RESTART_SLOW_MS * 4 })))).toBe(
    "Back on v0.5.97 — reloading…",
  );
  expect(body(await mount(restartAt("gave-up")))).toBe(
    "The app didn't come back. You can try again, or start fused-render from the menu bar.",
  );
  // The way out that does not depend on this page at all is named, because if
  // the app is truly gone the button beside it cannot work.
  expect(body(await mount(restartAt("gave-up")))).toContain("menu bar");
});

test("ready still says what it always said", async () => {
  expect(body(await mount(restartAt("ready")))).toBe(
    "The app is still running v0.5.96. Restart to finish the update.",
  );
});
