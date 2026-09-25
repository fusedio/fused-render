// The plan popup (D890): opening it from either affordance, sharing PlanCard's
// OWN decision state (not a copy — deciding in the modal must resolve the
// card and close the popup, and a double-click must not double-send), and the
// read-only path opened from a resolved card or from the historical ToolChip.
//
// Needs the DOM shim + a portal container (installPortalContainer), unlike
// cards.test.tsx's plain react-test-renderer mounts: PlanCard/ToolChip render
// nothing extra while the modal is closed (no `createPortal` call happens),
// but every test here opens it, and the shared `Modal` chassis always portals.
//
// Traversal is via `r.root.findAll` (the ReactTestInstance tree), NOT
// `r.toJSON()` (cards.test.tsx's own pattern): `toJSON()` walks the rendered
// DOM shape, which does not include a portal's subtree at all — the dialog
// mounts under `document.body`, not under the root's own output. The instance
// tree does, because it walks React's fiber tree instead (same reason
// `UpdateDialog.test.tsx` uses this pattern for its own portaled chassis).
import {
  installDomShim,
  installPortalContainer,
  removePortalContainer,
} from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";

import { createCardPolicy, CardPolicyProvider } from "./cardPolicy";
import { PlanCard } from "./PlanCard";
import { ToolChip } from "./ToolChip";
import type { PermissionRow, ToolSegment } from "../protocol/types";

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement): ReturnType<typeof create> {
  installPortalContainer();
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  removePortalContainer();
});

type Props = Record<string, unknown>;

/** Every string under a node, joined — the UpdateDialog.test.tsx pattern. */
function textOf(node: ReactTestInstance | string | undefined): string {
  if (node === undefined) return "";
  if (typeof node === "string") return node;
  let out = "";
  for (const c of node.children as Array<ReactTestInstance | string>) {
    out += typeof c === "string" ? c : textOf(c);
  }
  return out;
}
// HOST NODES ONLY (`typeof n.type === "string"`): `findAll` walks composite
// AND host fibers, and a composite that forwards `className` straight through
// (MarkdownView) carries the same prop on both — counting both would double
// every real element.
function all(r: ReturnType<typeof create> | ReactTestInstance, type: string): ReactTestInstance[] {
  const root = r instanceof Object && "root" in r ? (r as ReturnType<typeof create>).root : (r as ReactTestInstance);
  return root.findAll((n) => typeof n.type === "string" && n.type === type);
}
function withClass(r: ReturnType<typeof create> | ReactTestInstance, cls: string): ReactTestInstance[] {
  const root = r instanceof Object && "root" in r ? (r as ReturnType<typeof create>).root : (r as ReactTestInstance);
  return root.findAll(
    (n) =>
      typeof n.type === "string" &&
      String((n.props as Props)?.className ?? "")
        .split(" ")
        .includes(cls),
  );
}
function dialogs(r: ReturnType<typeof create>): ReactTestInstance[] {
  return r.root.findAll((n) => typeof n.type === "string" && (n.props as Props)?.role === "dialog");
}
function labels(r: ReturnType<typeof create> | ReactTestInstance): string[] {
  return all(r, "button").map((b) => textOf(b));
}
function press(r: ReturnType<typeof create> | ReactTestInstance, label: string): void {
  const btn = all(r, "button").find((b) => textOf(b) === label);
  if (!btn) throw new Error("no button labelled " + JSON.stringify(label) + " in " + labels(r).join(" | "));
  act(() => {
    (btn.props as Props & { onClick: (e: unknown) => void }).onClick(fakeEvent());
  });
}
function fakeEvent(over: Props = {}): Props {
  return {
    preventDefault() {},
    stopPropagation() {},
    nativeEvent: {},
    currentTarget: {},
    target: {},
    ...over,
  };
}
/** The clickable open affordances are `role="button"` `div`s, not `button`s
 *  (PlanCard.tsx's `activateOnKey` doc comment explains why: several pinned
 *  tests in cards.test.tsx assert `labels()` — every `<button>` in the tree —
 *  is `[]` once a plan is resolved, and this control is offered in EVERY
 *  state, resolved included). */
function clickOpen(r: ReturnType<typeof create>, cls: string): void {
  const target = withClass(r, cls)[0];
  if (!target) throw new Error("no ." + cls + " in the tree");
  act(() => {
    (target.props as Props & { onClick: (e: unknown) => void }).onClick(fakeEvent());
  });
}

function row(over: Partial<PermissionRow> = {}): PermissionRow {
  return {
    id: "req-1",
    tool: "ExitPlanMode",
    input: { plan: "# Step one\n\nDo the thing." },
    created_at: 0,
    decision: "",
    scope: "",
    mode: "",
    answers: {},
    ...over,
  };
}
const noop = async () => {};

test("clicking the plan body opens a wide, heading-labelled dialog with the same plan text", () => {
  const r = mount(<PlanCard row={row()} onDecide={noop} />);
  expect(dialogs(r)).toHaveLength(0);
  clickOpen(r, "plan-open-target");
  const found = dialogs(r);
  expect(found).toHaveLength(1);
  expect(found[0].props["aria-modal"]).toBe("true");
  // Labelled by the heading (no separate aria-label override).
  expect(typeof found[0].props["aria-labelledby"]).toBe("string");
  const h2 = all(r, "h2")[0];
  expect(textOf(h2)).toBe("Claude has a plan");
  // The card's own `.plan-body` plus the modal's — same funnel, same markup.
  // MarkdownView renders via `dangerouslySetInnerHTML` (protocol/markdown.ts's
  // sanitized HTML), never through children text nodes, so `textOf` (which
  // walks `.children`) sees nothing here — read the rendered HTML instead,
  // same as cards.test.tsx's own convention for this component.
  const bodies = withClass(r, "plan-body");
  expect(bodies).toHaveLength(2);
  const htmlOf = (n: ReactTestInstance) =>
    String((n.props as Props & { dangerouslySetInnerHTML?: { __html?: string } }).dangerouslySetInnerHTML?.__html ?? "");
  const htmls = bodies.map(htmlOf);
  expect(htmls[0]).toBe(htmls[1]); // same funnel producing byte-identical markup
  expect(htmls[0]).toContain("Step one");
  expect(htmls[0]).toContain("Do the thing.");
});

test("the explicit 'Open' affordance opens the same dialog", () => {
  const r = mount(<PlanCard row={row()} onDecide={noop} />);
  clickOpen(r, "plan-open-chip");
  expect(dialogs(r)).toHaveLength(1);
});

test("the modal offers the SAME pending actions, and deciding there resolves the card and closes it", async () => {
  const sent: unknown[] = [];
  const r = mount(
    <PlanCard
      row={row()}
      onDecide={async (...args: unknown[]) => {
        sent.push(args);
      }}
    />,
  );
  clickOpen(r, "plan-open-target");
  // Both surfaces' "Approve plan" exist (card + modal) while pending.
  expect(labels(r).filter((l) => l === "Approve plan")).toHaveLength(2);
  // Scoped to the dialog, so this presses the MODAL's own button and not the
  // card's identically-labelled one sitting earlier in the same tree.
  press(dialogs(r)[0], "Approve plan");
  await act(async () => {});
  expect(sent).toEqual([["req-1", "allow", undefined, undefined]]);
  // Closed by the decision itself (D890) — not left open over a resolved row.
  expect(dialogs(r)).toHaveLength(0);
});

test("a still-in-flight send disables BOTH surfaces at once — one `posting`, not two", async () => {
  let resolveSend!: () => void;
  const pending = new Promise<void>((res) => {
    resolveSend = res;
  });
  const sent: unknown[] = [];
  const r = mount(
    <PlanCard
      row={row()}
      onDecide={async (...args: unknown[]) => {
        sent.push(args);
        await pending;
      }}
    />,
  );
  press(r, "Approve plan"); // the CARD's own button — modal not open yet
  clickOpen(r, "plan-open-chip");
  // The modal's own "Approve plan" is disabled too: same `posting` boolean.
  const modalApprove = all(r, "button").filter((b) => textOf(b) === "Approve plan");
  expect(modalApprove.length).toBeGreaterThan(0);
  expect(modalApprove.every((b) => (b.props as Props).disabled)).toBe(true);
  resolveSend();
  await act(async () => {});
  expect(sent).toHaveLength(1);
});

test("once resolved, the modal is read-only: no actions, just the status line", () => {
  const r = mount(<PlanCard row={row({ decision: "allow" })} onDecide={noop} />);
  clickOpen(r, "plan-open-target");
  expect(withClass(r, "plan-note")).toHaveLength(0);
  // The ONLY button anywhere is the chassis' own ✕ — no Approve/Keep planning,
  // in the dialog or (as ever) on the resolved card itself.
  expect(labels(r)).toEqual(["✕"]);
  const statuses = withClass(r, "perm-status");
  expect(statuses.map((s) => textOf(s))).toEqual(["✓ Plan approved", "✓ Plan approved"]);
  const h2 = all(r, "h2")[0];
  expect(textOf(h2)).toBe("Claude had a plan");
});

// Esc/backdrop-click are NOT exercised here: `testDomShim`'s `document` stub
// makes `addEventListener`/`removeEventListener` no-ops (by design — see its
// own doc comment), so a dispatched keydown never reaches the chassis'
// listener in this harness. The chassis' Esc/backdrop wiring is untouched by
// this feature (the `getContainer` addition reroutes WHICH document it
// listens on, not whether it does) — that half of the brief is a to-verify
// item for a real browser pass, not something this suite can fake green on.

test("the ✕ closes the popup", async () => {
  const r = mount(<PlanCard row={row()} onDecide={noop} />);
  clickOpen(r, "plan-open-target");
  expect(dialogs(r)).toHaveLength(1);
  press(r, "✕");
  // The chassis defers its OWN close paths (Esc/backdrop/✕) by
  // OVERLAY_EXIT_MS for the exit animation (lib/exit-animation.ts) — the real
  // `onClose` (this feature's `closeModal`) fires after that timer, not
  // synchronously with the click.
  await act(async () => {
    await new Promise((res) => setTimeout(res, 160));
  });
  expect(dialogs(r)).toHaveLength(0);
});

test("the historical ToolChip plan opens a read-only popup with no row to decide against", () => {
  const key = "k" + Math.random();
  const policy = createCardPolicy();
  policy.overrides.set(key, true); // chip body renders only while open (A GAP-D10)
  const seg = {
    kind: "tool",
    id: key,
    name: "ExitPlanMode",
    status: "ok",
    input: { plan: "# Historical plan" },
    output: null,
    images: [],
  } as unknown as ToolSegment;
  const r = mount(
    <CardPolicyProvider value={policy}>
      <ToolChip seg={seg} cardKey={key} />
    </CardPolicyProvider>,
  );
  clickOpen(r, "plan-open-chip");
  expect(dialogs(r)).toHaveLength(1);
  // Scoped to the dialog: the ONLY button inside it is the chassis' own ✕ —
  // no Approve/Keep planning for a historical, row-less plan. (The chip's own
  // collapsible trigger button lives outside the dialog and is not this
  // assertion's concern.)
  expect(labels(dialogs(r)[0])).toEqual(["✕"]);
  expect(withClass(r, "plan-note")).toHaveLength(0);
});
