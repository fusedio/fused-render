import { describe, expect, test } from "bun:test";

import { insertAt, jobNote, micClock, transcribeTake, watchJob } from "./dictation";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const noSleep = () => Promise.resolve();

describe("jobNote", () => {
  test("the server's own detail wins, then the first-run install, then the plain state", () => {
    expect(jobNote(null)).toBe("Transcribing…");
    expect(jobNote({ detail: "Preparing MLX Whisper — downloading torch (1m40s)…" })).toBe(
      "Preparing MLX Whisper — downloading torch (1m40s)…",
    );
    expect(jobNote({ waiting_for: "sys:ai-model:whisper" })).toBe("Preparing the speech model (first time only)…");
    expect(jobNote({ waiting_for: "sys:capture:abc" })).toBe("Transcribing…");
  });
});

test("micClock: m:ss", () => {
  expect(micClock(0)).toBe("0:00");
  expect(micClock(7)).toBe("0:07");
  expect(micClock(75)).toBe("1:15");
});

describe("insertAt", () => {
  test("an empty box takes the words as they are", () => {
    expect(insertAt("", 0, 0, "hello there")).toEqual({ value: "hello there", caret: 11 });
  });
  test("at the end: one space before, the caret after the words", () => {
    expect(insertAt("fix this", 8, 8, "and that")).toEqual({ value: "fix this and that", caret: 17 });
  });
  test("mid-sentence: a space on either side, only where one is missing", () => {
    expect(insertAt("fix bug", 3, 3, "the")).toEqual({ value: "fix the bug", caret: 7 });
    expect(insertAt("fix  bug", 4, 4, "the")).toEqual({ value: "fix the bug", caret: 7 });
  });
  test("over a selection: the selected words are replaced", () => {
    expect(insertAt("fix old bug", 4, 7, "the new")).toEqual({ value: "fix the new bug", caret: 11 });
  });
  test("a caret past the end is the end", () => {
    expect(insertAt("ab", 99, 99, "c")).toEqual({ value: "ab c", caret: 4 });
  });
});

describe("watchJob", () => {
  test("running and waiting are both in flight; the first other state ends the watch", async () => {
    const states = ["waiting", "running", "done"];
    const seen: string[] = [];
    const rec = await watchJob("j1", undefined, (j) => seen.push(j.state), {
      fetch: (async () => json({ jobs: [{ id: "j1", state: states.shift() }] })) as unknown as typeof fetch,
      sleep: noSleep,
    });
    expect(rec?.state).toBe("done");
    expect(seen).toEqual(["waiting", "running"]);
  });
  test("a row that vanishes after a sighting: null after five misses", async () => {
    let calls = 0;
    const rec = await watchJob("j1", undefined, undefined, {
      fetch: (async () => json({ jobs: calls++ === 0 ? [{ id: "j1", state: "running" }] : [] })) as unknown as typeof fetch,
      sleep: noSleep,
    });
    expect(rec).toBeNull();
    expect(calls).toBe(6);
  });
  test("stopped() ends it without another read", async () => {
    let calls = 0;
    const rec = await watchJob("j1", () => true, undefined, {
      fetch: (async () => {
        calls += 1;
        return json({ jobs: [] });
      }) as unknown as typeof fetch,
      sleep: noSleep,
    });
    expect(rec).toBeNull();
    expect(calls).toBe(0);
  });
});

describe("transcribeTake", () => {
  test("the words come from the transcript file, and the requester rides the start call", async () => {
    const calls: Array<{ url: string; headers?: Record<string, string> }> = [];
    const notes: string[] = [];
    const said = await transcribeTake("/r/a.m4a", "shell:chat-composer", (n) => notes.push(n), () => {}, {
      fetch: (async (url: string, init?: RequestInit) => {
        calls.push({ url, headers: init?.headers as Record<string, string> });
        if (url === "/api/ai/transcribe") return json({ jobId: "t1", output: "/r/a.json" });
        if (url === "/api/jobs") {
          const n = calls.filter((c) => c.url === "/api/jobs").length;
          return json({ jobs: [{ id: "t1", state: n === 1 ? "running" : "done", detail: n === 1 ? "Reading audio…" : "" }] });
        }
        return json({ text: "  hello   there " });
      }) as unknown as typeof fetch,
      sleep: noSleep,
    });
    expect(said).toBe("  hello   there ");
    expect(calls[0]!.headers!["X-Fused-Requester"]).toBe("shell:chat-composer");
    expect(notes).toEqual(["Reading audio…"]);
    expect(calls[calls.length - 1]!.url).toContain(encodeURIComponent("/r/a.json"));
  });
  test("a cold model: say so once, wait for the download, ask again", async () => {
    let starts = 0;
    const said: string[] = [];
    const text = await transcribeTake("/r/a.m4a", "x", () => {}, (m, tone) => said.push(tone + ": " + m), {
      fetch: (async (url: string) => {
        if (url === "/api/ai/transcribe") {
          starts += 1;
          return starts === 1
            ? json({ error: { type: "model_loading", message: "loading" }, jobId: "dl1" }, 409)
            : json({ jobId: "t1", output: "/r/a.json" });
        }
        if (url === "/api/jobs") return json({ jobs: [{ id: "dl1", state: "done" }, { id: "t1", state: "done" }] });
        return json({ text: "ok" });
      }) as unknown as typeof fetch,
      sleep: noSleep,
    });
    expect(text).toBe("ok");
    expect(starts).toBe(2);
    expect(said).toEqual([
      "info: Downloading the speech model (first time only). Your recording is kept; the text follows.",
    ]);
  });
  test("a failed job is a typed error carrying the server's message", async () => {
    const run = transcribeTake("/r/a.m4a", "x", () => {}, () => {}, {
      fetch: (async (url: string) => {
        if (url === "/api/ai/transcribe") return json({ jobId: "t1", output: "/r/a.json" });
        return json({ jobs: [{ id: "t1", state: "error", message: "no runner" }] });
      }) as unknown as typeof fetch,
      sleep: noSleep,
    });
    await expect(run).rejects.toMatchObject({ message: "no runner", type: "ai_error" });
  });
  test("a plain 409 (no model, no runner) stays an error, not a wait", async () => {
    const run = transcribeTake("/r/a.m4a", "x", () => {}, () => {}, {
      fetch: (async () => json({ error: "no speech model installed" }, 409)) as unknown as typeof fetch,
      sleep: noSleep,
    });
    await expect(run).rejects.toMatchObject({ message: "no speech model installed", type: "unavailable" });
  });
});
