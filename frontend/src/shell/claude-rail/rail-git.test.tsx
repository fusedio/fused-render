// The rail's git read (useRailGit; owner, 2026-11-10): a path change keeps
// the last repository drawn while the new read is out — the explorer's
// folder → file hop in one repository must not flicker the Git door, its
// hairline or its card — the answer swaps it, and only an answer of "no
// repository" takes it away. A check still out for the old path is dropped.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { RailGitRepo } from "@platform/lib/api";

const { useRailGit, resetRailGitCacheForTests: resetCache } = await import("./rail-git");
type RailGit = ReturnType<typeof useRailGit>;

const repo = (branch: string): RailGitRepo => ({
  root: "/r",
  branch,
  has_remote: true,
  default_branch: "main",
  ahead: 0,
  behind: 0,
  commits: [],
});

/** Every git read waits until the test answers it, in order. */
let pending: { url: string; method: string; answer: (body: unknown) => void; reject: () => void }[] = [];
const realFetch = globalThis.fetch;
beforeEach(() => {
  pending = [];
  resetCache();
  (globalThis as { fetch: unknown }).fetch = (url: string, init?: { method?: string }) =>
    // Only the git reads are the test's; the tasks pulse waits forever.
    new Promise<Response>((res, rej) => {
      if (!String(url).includes("/api/git/rail")) return;
      pending.push({
        url: String(url),
        method: init?.method ?? "GET",
        answer: (b) => res(new Response(JSON.stringify(b), { status: 200 })),
        reject: () => rej(new Error("connection lost")),
      });
    });
});
afterEach(() => {
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

let seen: RailGit | null = null;
function Probe({ path }: { path: string | null }) {
  seen = useRailGit(path);
  return null;
}
const answer = async (i: number, body: unknown) => {
  await act(async () => {
    pending[i]!.answer(body);
    await new Promise((res) => setTimeout(res, 0));
  });
};

describe("useRailGit across a path change", () => {
  test("the last repository stays until the new answer; null takes it away; no path, nothing", async () => {
    let r!: ReactTestRenderer;
    await act(async () => {
      r = create(<Probe path="/r/a" />);
    });
    expect(seen!.repo).toBeUndefined();
    expect(pending[0]!.url).toContain("limit=50");
    await answer(0, { ok: true, repo: repo("a") });
    expect(seen!.repo?.branch).toBe("a");
    // Folder → file: the read is out, the repository stays drawn.
    await act(async () => {
      r.update(<Probe path="/r/a/f.py" />);
    });
    expect(pending).toHaveLength(2);
    expect(seen!.repo?.branch).toBe("a");
    await answer(1, { ok: true, repo: repo("b") });
    expect(seen!.repo?.branch).toBe("b");
    // Out of the repository: the answer null takes it away.
    await act(async () => {
      r.update(<Probe path="/plain" />);
    });
    expect(seen!.repo?.branch).toBe("b");
    await answer(2, { ok: true, repo: null });
    expect(seen!.repo).toBeNull();
    await act(async () => {
      r.update(<Probe path={null} />);
    });
    expect(seen!.repo).toBeUndefined();
    act(() => r.unmount());
  });

  test("a check out for the old path is dropped: its late answer neither lands nor holds the flag; `checked` resets", async () => {
    let r!: ReactTestRenderer;
    await act(async () => {
      r = create(<Probe path="/r/a" />);
    });
    await answer(0, { ok: true, repo: repo("a") });
    await act(async () => {
      seen!.check();
    });
    expect(seen!.checking).toBe(true);
    const check = pending.length - 1;
    expect(pending[check]!.method).toBe("POST");
    await act(async () => {
      r.update(<Probe path="/r/b" />);
    });
    expect(seen!.checking).toBe(false);
    expect(seen!.checked).toBeNull();
    expect(seen!.repo?.branch).toBe("a");
    await answer(check, { ok: true, repo: repo("stale"), checked: false });
    expect(seen!.repo?.branch).toBe("a");
    expect(seen!.checked).toBeNull();
    await answer(pending.length - 1, { ok: true, repo: repo("b") });
    expect(seen!.repo?.branch).toBe("b");
    act(() => r.unmount());
  });
});

describe("useRailGit sync (the card's Send / Get latest)", () => {
  test("posts the op, holds `syncing`, lands the fresh counts; a refusal is the note", async () => {
    await act(async () => {
      create(<Probe path="/r/a" />);
    });
    await answer(0, { ok: true, repo: repo("a") });
    await act(async () => {
      seen!.sync("push");
    });
    expect(seen!.syncing).toBe("push");
    expect(pending[1]!.url).toContain("/api/git/rail/sync");
    expect(pending[1]!.method).toBe("POST");
    // A second press while one is out does nothing.
    await act(async () => {
      seen!.sync("pull");
    });
    expect(pending).toHaveLength(2);
    await answer(1, { ok: true, repo: repo("sent"), result: { ok: true, op: "push" } });
    expect(seen!.syncing).toBeNull();
    expect(seen!.repo?.branch).toBe("sent");
    expect(seen!.syncNote).toBeNull();
    await act(async () => {
      seen!.sync("pull");
    });
    await answer(2, { ok: true, repo: repo("sent"), result: { ok: false, reason: "not-fast-forward", message: "diverged" } });
    expect(seen!.syncNote).toBe("diverged");
    // A later read whose counts moved (fixed in a terminal) takes the note away.
    await act(async () => {
      seen!.reload();
    });
    await answer(3, { ok: true, repo: { ...repo("sent"), upstream: { ahead: 0, behind: 0 } } });
    expect(seen!.syncNote).toBeNull();
  });

  test("a lost answer says so and re-reads, so a push that landed shows as landed", async () => {
    await act(async () => {
      create(<Probe path="/r/a" />);
    });
    await answer(0, { ok: true, repo: { ...repo("a"), upstream: { ahead: 2, behind: 0 } } });
    await act(async () => {
      seen!.sync("push");
    });
    await act(async () => {
      pending[1]!.reject();
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(seen!.syncing).toBeNull();
    expect(seen!.syncNote).toBe("The push did not finish.");
    expect(pending[2]!.method).toBe("GET");
    await answer(2, { ok: true, repo: { ...repo("a"), upstream: { ahead: 0, behind: 0 } } });
    expect(seen!.repo?.upstream).toEqual({ ahead: 0, behind: 0 });
    expect(seen!.syncNote).toBeNull();
  });

  test("no sync while a check is out; a path change drops a sync that is out", async () => {
    let r!: ReactTestRenderer;
    await act(async () => {
      r = create(<Probe path="/r/a" />);
    });
    await answer(0, { ok: true, repo: repo("a") });
    await act(async () => {
      seen!.check();
    });
    await act(async () => {
      seen!.sync("push");
    });
    expect(pending.map((p) => p.url.includes("/sync"))).toEqual([false, false]);
    await answer(1, { ok: true, repo: repo("a"), checked: true });
    await act(async () => {
      seen!.sync("push");
    });
    expect(seen!.syncing).toBe("push");
    await act(async () => {
      r.update(<Probe path="/r/b" />);
    });
    expect(seen!.syncing).toBeNull();
    await answer(3, { ok: true, repo: repo("b") });
    // The old path's late answer neither lands nor says anything.
    await answer(2, { ok: true, repo: repo("late"), result: { ok: false, message: "nope" } });
    expect(seen!.repo?.branch).toBe("b");
    expect(seen!.syncNote).toBeNull();
  });
});

describe("cachedRailGit: a new rail's first frame (QA, 2026-11-10)", () => {
  test("the path's last answer, else the innermost repository holding it", async () => {
    const { cachedRailGit, resetRailGitCacheForTests } = await import("./rail-git");
    resetRailGitCacheForTests();
    expect(cachedRailGit("/r/a")).toBeUndefined();
    let r!: ReactTestRenderer;
    await act(async () => {
      r = create(<Probe path="/r" />);
    });
    await answer(0, { ok: true, repo: { ...repo("outer"), root: "/r" } });
    await act(async () => {
      r.update(<Probe path="/r/sub" />);
    });
    await answer(1, { ok: true, repo: { ...repo("inner"), root: "/r/sub" } });
    await act(async () => {
      r.update(<Probe path="/plain" />);
    });
    await answer(2, { ok: true, repo: null });
    expect(cachedRailGit("/r/a.py")?.branch).toBe("outer");
    expect(cachedRailGit("/r/sub/x/y.py")?.branch).toBe("inner");
    expect(cachedRailGit("/r/")?.branch).toBe("outer");
    expect(cachedRailGit("/plain")).toBeNull();
    expect(cachedRailGit("/rx")).toBeUndefined();
    expect(cachedRailGit(null)).toBeUndefined();
    act(() => r.unmount());
    resetRailGitCacheForTests();
  });
});

describe("the answers cache is bounded (CodeRabbit on #2236)", () => {
  test("200 paths at most; a path answered again is the newest, so the least recent goes first", async () => {
    const { cachedRailGit, railGitCacheSize, rememberRailGitForTests, resetRailGitCacheForTests } = await import("./rail-git");
    resetRailGitCacheForTests();
    for (let i = 0; i < 200; i++) rememberRailGitForTests(`/p${i}`, null);
    expect(railGitCacheSize()).toBe(200);
    // /p0 answered again: now the newest.
    rememberRailGitForTests("/p0", null);
    rememberRailGitForTests("/p200", null);
    expect(railGitCacheSize()).toBe(200);
    expect(cachedRailGit("/p0")).toBeNull();
    expect(cachedRailGit("/p1")).toBeUndefined();
    expect(cachedRailGit("/p200")).toBeNull();
    resetRailGitCacheForTests();
  });
});
