// The floating job pop-up's own lifecycle: it must stay up for
// `JOB_POPUP_VISIBLE_MS`, then play the same `TOAST_EXIT_MS` collapse
// `lib/toast` uses before telling its parent it is gone — real timers, the
// same `sleep`/`afterExit` idiom `toast.test.ts` already uses for the
// identical shape of test, rather than mocked ones (this module reads
// `window.setTimeout` directly, and bun:test has no fake-timer harness wired
// up for it).
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import type { ReactTestRendererJSON } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";
import type { Job } from "@platform/lib/jobs";
import { JOB_POPUP_VISIBLE_MS } from "@platform/lib/jobs";
import { TOAST_EXIT_MS } from "@platform/lib/toast";

installDomShim();
const { default: JobPopupCard } = await import("@platform/ui/JobPopupCard");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const JOB: Job = {
  id: "j1",
  title: "a red fox in snow",
  detail: "",
  model: "",
  kind: "task",
  state: "done",
  done: null,
  total: null,
  total_scope: "phase",
  total_estimated: false,
  unit: "",
  message: "",
  page: "",
  origin: "",
  owner: "page",
  cancellable: true,
  cancel_requested: false,
  started_at: 0,
  updated_at: 0,
  finished_at: 10,
  stalled: false,
  waiting_for: "",
  tier: "transient",
};

test("the card stays mounted for JOB_POPUP_VISIBLE_MS, then leaves, then calls onGone", async () => {
  let gone = false;
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(<JobPopupCard job={JOB} onGone={() => (gone = true)} />);
  });

  // Still up, not yet leaving, well before the visible window ends.
  await act(async () => {
    await sleep(Math.min(200, JOB_POPUP_VISIBLE_MS - 100));
  });
  expect(gone).toBe(false);
  let json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).not.toContain("leaving");

  // Past the visible window, but not yet the exit animation — now leaving,
  // still mounted, `onGone` not yet called.
  await act(async () => {
    await sleep(JOB_POPUP_VISIBLE_MS - Math.min(200, JOB_POPUP_VISIBLE_MS - 100) + 40);
  });
  expect(gone).toBe(false);
  json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).toContain("leaving");

  // Past the exit animation too — `onGone` has fired.
  await act(async () => {
    await sleep(TOAST_EXIT_MS + 60);
  });
  expect(gone).toBe(true);
}, JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS + 2000);

test("clicking the card (opening it) closes it early, through JobRow's own dismiss", async () => {
  // A job with somewhere to go, and a `dismissFn` stub standing in for the
  // real network call so the dismiss resolves deterministically — the same
  // test seam `JobRow.test.tsx` uses on `JobRow` directly, threaded through
  // `JobPopupCard` unchanged.
  let gone = false;
  const job: Job = { ...JOB, page: "/tmp/out.png" };
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(
      <JobPopupCard
        job={job}
        onGone={() => (gone = true)}
        dismissFn={async (id) => ({ dismissed: id })}
      />,
    );
  });

  const root = renderer!.root;
  const clickable = root.findAll(
    (node) => typeof node.props.onClick === "function" && node.props.role === "button",
  );
  expect(clickable.length).toBeGreaterThan(0);
  await act(async () => {
    clickable[0].props.onClick({ preventDefault() {}, stopPropagation() {} });
  });
  // Let the stubbed dismiss promise settle.
  await act(async () => {
    await sleep(20);
  });
  expect(gone).toBe(false); // still in its exit window, not yet fully gone
  const json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).toContain("leaving");
}, 5000);

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (typeof node.props?.className === "string" && node.props.className.split(" ").includes(className)) {
    hits.push(node);
  }
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, className));
  }
  return hits;
}

test("the ✕ only closes the card — it never calls the real, server-side dismiss", async () => {
  let dismissCalls = 0;
  const job: Job = { ...JOB, page: "/tmp/out.png", tier: "trail" };
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(
      <JobPopupCard
        job={job}
        onGone={() => {}}
        dismissFn={async (id) => {
          dismissCalls++;
          return { dismissed: id };
        }}
      />,
    );
  });

  const before = renderer!.toJSON() as ReactTestRendererJSON;
  const x = findAll(before, "dl-x")[0];
  expect(x).toBeDefined();
  act(() => {
    (x.props as { onClick: () => void }).onClick();
  });

  // The card starts leaving on its own — no network dismiss behind it, and
  // no wait needed for one to settle.
  expect(dismissCalls).toBe(0);
  const after = renderer!.toJSON() as ReactTestRendererJSON;
  expect(after.props.className).toContain("leaving");
});
