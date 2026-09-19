// The bottom-right progress card for a RUNNING self-update job. What makes it
// different from `JobPopupCard` is exactly what these tests pin: it draws a
// live row (Cancel present), its ✕ exists even though the job is running
// (a running `JobRow` normally has no Dismiss), the ✕ hides the card without
// any server call, and it never leaves on its own.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";
import type { Job } from "@platform/lib/jobs";

const { default: UpdateProgressCard } = await import("@platform/ui/UpdateProgressCard");

const JOB: Job = {
  id: "sys:update:9.9.10",
  title: "Update to v9.9.10",
  detail: "Downloading",
  model: "",
  kind: "download",
  state: "running",
  done: 10 * 1024 * 1024,
  total: 60 * 1024 * 1024,
  total_scope: "phase",
  total_estimated: false,
  unit: "bytes",
  message: "",
  page: "/preferences",
  source: "",
  origin: "",
  owner: "server",
  cancellable: true,
  cancel_requested: false,
  started_at: 0,
  updated_at: 0,
  finished_at: null,
  stalled: false,
  waiting_for: "",
  tier: "trail",
  group: "sys:update:9.9.10",
};

type Json = ReactTestRendererJSON;
function findAll(node: Json | Json[] | null, pred: (n: Json) => boolean, out: Json[] = []): Json[] {
  if (node === null) return out;
  if (Array.isArray(node)) {
    node.forEach((n) => findAll(n, pred, out));
    return out;
  }
  if (pred(node)) out.push(node);
  for (const c of node.children ?? []) if (typeof c !== "string") findAll(c, pred, out);
  return out;
}
const byAria = (tree: Json | Json[] | null, prefix: string) =>
  findAll(tree, (n) => typeof n.props?.["aria-label"] === "string" && n.props["aria-label"].startsWith(prefix));

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test("a running update draws Cancel AND a ✕, and the ✕ hides the card without a request", async () => {
  const realFetch = globalThis.fetch;
  let fetched = 0;
  (globalThis as { fetch: unknown }).fetch = async () => {
    fetched += 1;
    throw new Error("no network in this test");
  };
  try {
    let r!: ReturnType<typeof create>;
    await act(async () => {
      r = create(<UpdateProgressCard job={JOB} />);
    });
    const tree = r.toJSON() as Json;
    expect(tree.props.className).toContain("update-progress-card");
    expect(byAria(tree, "Cancel ").length).toBe(1);
    const x = byAria(tree, "Dismiss ");
    expect(x.length).toBe(1);
    await act(async () => {
      x[0].props.onClick();
    });
    expect(r.toJSON()).toBeNull();
    expect(fetched).toBe(0);
  } finally {
    (globalThis as { fetch: unknown }).fetch = realFetch;
  }
});

test("the card has no clock — it is still there well past a pop-up's lifetime", async () => {
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<UpdateProgressCard job={JOB} />);
  });
  await act(async () => {
    await sleep(300);
  });
  const tree = r.toJSON() as Json;
  expect(tree).not.toBeNull();
  expect(tree.props.className).not.toContain("leaving");
});
