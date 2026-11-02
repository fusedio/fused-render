// The job pop-up's IDENTITY as `NotificationHost` mounts it — not its own
// internal countdown, which `JobPopupCard.test.tsx` already covers in full.
//
// `jobs.ts`'s `popupTick` keys "have I popped this?" on `popupKey` (id +
// `finished_at`), not on the bare id, precisely because one id CAN go
// terminal more than once: `fused_render/ai/supervisor.py`'s
// `job_id_for(model)` mints one id per resident model (`sys:ai-model:*`),
// reused across that model's load, weights-only download and unload, not a
// fresh id per run the way `jobs.py`'s ordinary job ids are. If the mounted
// `<JobPopupCard>` is keyed only on `job.id`, a second terminal event landing
// on the same id while the first card is still up (or leaving) reuses that
// same component instance — its mount effect (`[]` deps) already ran once
// for the first event and never reruns, so the second event's card never
// restarts its own countdown and can vanish on the FIRST event's timer
// instead of running its own `JOB_POPUP_VISIBLE_MS` in full.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";
import { JOB_POPUP_VISIBLE_MS, type Job } from "@platform/lib/jobs";

installDomShim();
const { default: NotificationHost } = await import("@platform/ui/NotificationHost");
const { _resetNotificationsForTest } = await import("@platform/lib/notifications");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function job(over: Partial<Job> = {}): Job {
  return {
    id: "sys:ai-model:llama",
    title: "Llama 3",
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
    source: "",
    origin: "",
    owner: "server",
    cancellable: false,
    cancel_requested: false,
    started_at: 0,
    updated_at: 0,
    finished_at: 1,
    stalled: false,
    waiting_for: "",
    tier: "trail",
    group: over.id ?? "sys:ai-model:llama",
    ...over,
  };
}

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (
    typeof node.props?.className === "string" &&
    node.props.className.split(" ").includes(className)
  ) {
    hits.push(node);
  }
  for (const c of node.children ?? []) hits.push(...findAll(c as ReactTestRendererJSON, className));
  return hits;
}

test(
  "two terminal events on one resident-model-load id each get a FULL-LENGTH card",
  async () => {
    // `bun test` runs every suite in ONE process, and `lib/notifications`'s
    // store is module state: a suite that ran earlier and called `notify()`
    // without resetting (the Monitor page's Kill/Stop tests did, 2026-10-30)
    // leaves a live popup behind. `NotificationHost` also mounts
    // `MessagePopupCard`, which renders THAT popup as a second `.toast-slot`
    // — and the store's own 2.5 s exit timer, armed when the stray `notify()`
    // ran, flips it to `leaving` partway through this test's first window.
    // On CI the gap between the two suites landed the stray card's exit
    // inside the first assertion's window on 10 of 10 runs; locally the gap
    // was long enough that the card was already gone. Start from an empty
    // store so the only cards counted here are this test's own.
    _resetNotificationsForTest();
    const first = job({ finished_at: 1 });
    let renderer: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(<NotificationHost jobPopup={first} />);
    });
    try {

      // Most of the way through the first card's visible window, but not there
      // yet — still up, not leaving.
      await act(async () => {
        await sleep(JOB_POPUP_VISIBLE_MS - 100);
      });
      let tree = renderer!.toJSON() as ReactTestRendererJSON;
      expect(findAll(tree, "leaving").length).toBe(0);

      // A SECOND terminal event on the SAME id — the model unloaded, reloaded,
      // and finished again — while the first card is still up.
      const second = job({ finished_at: 2 });
      await act(async () => {
        renderer!.update(<NotificationHost jobPopup={second} />);
      });

      // 400ms after the second event landed — ~2900ms after the FIRST did,
      // past where the first card's own countdown would have fired. A stale
      // instance reusing that timer would already read as leaving here; a
      // fresh card for the second event, just 400ms into its own life, must
      // not.
      await act(async () => {
        await sleep(400);
      });
      tree = renderer!.toJSON() as ReactTestRendererJSON;
      expect(findAll(tree, "leaving").length).toBe(0);

      // The second card's OWN countdown still runs to completion in full.
      await act(async () => {
        await sleep(JOB_POPUP_VISIBLE_MS - 400 + 100);
      });
      tree = renderer!.toJSON() as ReactTestRendererJSON;
      expect(findAll(tree, "leaving").length).toBe(1);

    } finally {
      // `NotificationHost` mounts `ServerStatusBanner`, which arms a real
      // `window.setInterval` polling `/api/config` every 5s and subscribes to
      // the update store. Left running past this test it keeps calling
      // whatever `fetch` a LATER file's suite has stubbed, for the rest of
      // the process — and a banner that outlived a FAILED run here answered
      // `ServerStatusBanner.test.tsx`'s own "installed" transition with a
      // second probe (its `probes` read 2, not 1, on every red CI run). So
      // the renderer comes down in `finally`: on a pass AND on a failure.
      await act(() => {
        renderer!.unmount();
      });
    }
  },
  JOB_POPUP_VISIBLE_MS * 2 + 3000,
);
