// The Workspace's three server doors (ports, holds, remove-anyway) and the hook
// every archive path shares: archiveTask reports its teardown to listeners, so
// one place can tell the reader what was kept without eight call sites each
// remembering to.
import { afterEach, describe, expect, mock, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const api = await import("@platform/lib/api");

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function answer(body: unknown): { calls: { url: string; body: unknown }[] } {
  const calls: { url: string; body: unknown }[] = [];
  globalThis.fetch = mock(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
  return { calls };
}

describe("task teardown api", () => {
  test("fetchTaskPorts posts the ids and unwraps ports", async () => {
    const rec = answer({ ports: { s1: [5173] } });
    expect(await api.fetchTaskPorts(["s1", "s2"])).toEqual({ s1: [5173] });
    expect(rec.calls[0].url).toBe("/api/tasks/ports");
    expect(rec.calls[0].body).toEqual({ ids: ["s1", "s2"] });
  });

  test("fetchTaskHolds posts the ids and unwraps holds", async () => {
    const rows = [{ path: "/w", dirty_files: 2, unpushed: 0, reasons: ["dirty"] }];
    const rec = answer({ holds: { s1: rows } });
    expect(await api.fetchTaskHolds(["s1"])).toEqual({ s1: rows });
    expect(rec.calls[0].url).toBe("/api/tasks/holds");
  });

  test("removeTaskWorktrees posts the key and the chosen paths", async () => {
    const rec = answer({ ok: true, key: "s1", removed: ["/w"], kept: [], gone: [] });
    const res = await api.removeTaskWorktrees("s1", ["/w"]);
    expect(res.removed).toEqual(["/w"]);
    expect(rec.calls[0].url).toBe("/api/tasks/worktrees/remove");
    expect(rec.calls[0].body).toEqual({ key: "s1", paths: ["/w"] });
  });

  test("archiveTask tells every listener what the teardown kept", async () => {
    const teardown = {
      stopped: 1, killed: [], removed: [],
      kept: [{ path: "/w", reasons: ["dirty"], dirty_files: 3, unpushed: 0 }], gone: [],
    };
    answer({ ok: true, key: "s1", cancelled: 0, filed: true, stopped: false, teardown });
    const seen: unknown[] = [];
    const off = api.onTaskArchived((key, res) => seen.push([key, res.teardown]));
    await api.archiveTask("s1");
    off();
    await api.archiveTask("s1");
    expect(seen).toEqual([["s1", teardown]]);
  });

  test("a listener that throws never fails the archive", async () => {
    answer({ ok: true, key: "s1", cancelled: 0, filed: true, stopped: false });
    const off = api.onTaskArchived(() => {
      throw new Error("boom");
    });
    const res = await api.archiveTask("s1");
    off();
    expect(res.ok).toBe(true);
  });
});
