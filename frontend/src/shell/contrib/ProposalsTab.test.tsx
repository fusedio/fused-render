// The inbox card's Try and Accept are pinned to the review on screen: they
// send that review's head, and when the sender has pushed since, the
// server's "review-needed" sentence is shown and the card re-reads — the yes
// never reaches code whose findings the owner did not see.
//
// fetch is stubbed directly (no mock.module, which is process-wide in bun).
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";
import type { AppContrib, AppContribState, InboxItem } from "@platform/lib/contrib";

installDomShim();
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { ProposalsTab } = await import("./ProposalsTab");

const realFetch = globalThis.fetch;
let sent: { url: string; body: Record<string, unknown> }[] = [];
let answer: unknown = { ok: true };
beforeEach(() => {
  sent = [];
  globalThis.fetch = (async (url: unknown, init?: RequestInit) => {
    sent.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : {} });
    return { ok: true, status: 200, json: async () => answer };
  }) as unknown as typeof fetch;
});
let r: ReactTestRenderer | null = null;
afterEach(() => {
  act(() => r?.unmount());
  r = null;
  globalThis.fetch = realFetch;
});

// The sender pushed `new` after Claude reviewed `old`: the review on screen
// is old's.
const ITEM: InboxItem = {
  number: 12,
  url: "https://github.com/me/sine/pull/12",
  title: "Faster plot",
  author: "sina",
  from_fused: true,
  summary: "Draws the plot in one pass.",
  review: { verdict: "fine", summary: "Looks fine.", risks: [], head_sha: "old" },
  review_status: "ready",
  try: null,
  head_sha: "new",
  created_at: "2026-10-26T10:00:00Z",
  fits: true,
};

function state(refreshes: { n: number }): AppContribState {
  const data = { inbox: { items: [ITEM], count: 1 } } as unknown as AppContrib;
  return {
    data,
    settled: true,
    answered: true,
    refresh: async () => {
      refreshes.n += 1;
    },
  };
}

const textOf = (n: ReactTestRenderer["root"] | string): string =>
  typeof n === "string" ? n : n.children.map((c) => textOf(c as ReactTestRenderer["root"] | string)).join("");

test("Try sends the head of the review on screen, and a review-needed refusal shows the server's sentence and re-reads", async () => {
  answer = { ok: false, reason: "review-needed", message: "sina changed this proposal after its review." };
  const refreshes = { n: 0 };
  await act(async () => {
    r = create(<ProposalsTab dir="/w/sine" slug="sine" contrib={state(refreshes)} />);
  });
  const tryBtn = r!.root.findAll((n) => n.type === "button" && textOf(n).trim() === "Try")[0]!;
  await act(async () => {
    tryBtn.props.onClick();
    await new Promise((res) => setTimeout(res, 0));
  });
  const post = sent.find((s) => s.url === "/api/contrib/inbox/try");
  expect(post?.body).toEqual({ path: "/w/sine", number: 12, confirm: false, head_sha: "old" });
  expect(textOf(r!.root)).toContain("sina changed this proposal after its review.");
  expect(refreshes.n).toBeGreaterThan(0);
});
