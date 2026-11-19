// The hook's own rules: where a take's words go, and when they go nowhere.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { AudioRecording, CaptureResult } from "./capture-audio";
import { useDictation, type Dictation, type DictationDeps } from "./dictation";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

interface World {
  deps: DictationDeps;
  cancelled: number;
  /** Let the transcript read answer. */
  release(): void;
}

function world(text: string): World {
  let open!: () => void;
  const gate = new Promise<void>((r) => (open = r));
  const w: World = { deps: {}, cancelled: 0, release: () => open() };
  const took: CaptureResult = {
    id: "c1", mode: "audio", state: "done", path: "/r/a.m4a", url: null, seconds: 3, maxSeconds: 120, jobId: "cap1",
  };
  const rec = {
    id: "c1", mode: "audio", path: "/r/a.m4a", jobId: "cap1", maxSeconds: 120, state: "recording", url: "", paused: false,
    pause: async () => {}, resume: async () => {},
    stop: async () => took,
    cancel: async () => {
      w.cancelled += 1;
      return { ...took, path: null };
    },
  } as AudioRecording;
  w.deps = {
    capture: async () => rec,
    sleep: () => new Promise((r) => setTimeout(r, 1)),
    fetch: (async (url: string) => {
      if (url === "/api/ai/transcribe") return json({ jobId: "t1", output: "/r/a.json" });
      // The capture's job stays "running" (the watch that follows it), the
      // transcription's is done at once.
      if (url === "/api/jobs") return json({ jobs: [{ id: "cap1", state: "running" }, { id: "t1", state: "done" }] });
      await gate;
      return json({ text });
    }) as unknown as typeof fetch,
  };
  return w;
}

function mount(w: World) {
  const got = { text: [] as string[], said: [] as string[], settled: 0 };
  let mic!: Dictation;
  function Probe() {
    mic = useDictation({
      requester: "test",
      onText: (t) => got.text.push(t),
      onMessage: (m) => got.said.push(m),
      onSettled: () => (got.settled += 1),
      deps: w.deps,
    });
    return null;
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<Probe />);
  });
  return { got, mic: () => mic, unmount: () => act(() => r.unmount()) };
}

const tick = (ms = 20) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

test("a take: press records, press again transcribes, the words arrive with their whitespace collapsed", async () => {
  const w = world("  hello   there ");
  const m = mount(w);
  await act(async () => m.mic().toggle());
  await tick();
  expect(m.mic().rec).toBe(true);
  expect(m.mic().time).toBe("0:00");
  await act(async () => m.mic().toggle());
  await tick();
  expect(m.mic().rec).toBe(false);
  expect(m.mic().busy).toBe(true);
  w.release();
  await tick();
  expect(m.got.text).toEqual(["hello there"]);
  expect(m.got.settled).toBe(1);
  expect(m.mic().busy).toBe(false);
  m.unmount();
});

test("a recording with no speech says so and writes nothing", async () => {
  const w = world("   ");
  const m = mount(w);
  await act(async () => m.mic().toggle());
  await tick();
  await act(async () => m.mic().toggle());
  w.release();
  await tick();
  expect(m.got.text).toEqual([]);
  expect(m.got.said).toEqual(["Didn't catch any speech in that recording."]);
  m.unmount();
});

test("cancel() while recording throws the take away", async () => {
  const w = world("never");
  const m = mount(w);
  await act(async () => m.mic().toggle());
  await tick();
  await act(async () => m.mic().cancel());
  await tick();
  expect(w.cancelled).toBe(1);
  expect(m.mic().rec).toBe(false);
  expect(m.mic().busy).toBe(false);
  expect(m.got.text).toEqual([]);
  m.unmount();
});

test("cancel() while transcribing: the words that come back land nowhere, and nothing is said", async () => {
  const w = world("for the other chat");
  const m = mount(w);
  await act(async () => m.mic().toggle());
  await tick();
  await act(async () => m.mic().toggle());
  await tick();
  expect(m.mic().busy).toBe(true);
  await act(async () => m.mic().cancel());
  expect(m.mic().busy).toBe(false);
  w.release();
  await tick();
  expect(m.got.text).toEqual([]);
  expect(m.got.said).toEqual([]);
  expect(m.got.settled).toBe(0);
  m.unmount();
});

test("cancel() while the microphone is still opening: the take that arrives is not kept", async () => {
  // CodeRabbit, PR #2372: with no handle yet, `cancel()` had nothing to stop,
  // and the capture that resolved afterwards recorded into the next chat.
  const w = world("for the other chat");
  const capture = w.deps.capture!;
  let open!: () => void;
  const opening = new Promise<void>((r) => (open = r));
  w.deps.capture = async (...args) => {
    await opening;
    return capture(...args);
  };
  const m = mount(w);
  await act(async () => m.mic().toggle());
  await act(async () => m.mic().cancel());
  open();
  await tick();
  expect(w.cancelled).toBe(1);
  expect(m.mic().rec).toBe(false);
  // …and the button is free for the next take.
  w.deps.capture = capture;
  await act(async () => m.mic().toggle());
  await tick();
  expect(m.mic().rec).toBe(true);
  m.unmount();
});

test("unmounting mid-recording cancels the take nobody could stop any more", async () => {
  const w = world("never");
  const m = mount(w);
  await act(async () => m.mic().toggle());
  await tick();
  m.unmount();
  await tick();
  expect(w.cancelled).toBe(1);
});
