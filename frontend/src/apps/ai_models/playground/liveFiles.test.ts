// The two playground stages' live-file follows: TranscribeStage's partial
// transcript tail and ImageStage's live preview reload. Both used to re-read on
// a timer while a job ran; both now hear `fs.watch {paths: [file]}` on the
// events bus and do ONE read (reload) per reported change. Driven through the
// helpers' own `subscribe` seam — never `mock.module`, which is process-wide
// in bun.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { SubscribeLike } from "@platform/lib/events";

const { tailPartialTranscript } = await import("./TranscribeStage");
const { followPreview } = await import("./ImageStage");

type Frame = (snap: unknown, delta: unknown, meta: Record<string, unknown>) => void;
interface Sub {
  topic: string;
  params: unknown;
  opts: unknown;
  cb: Frame;
  open: boolean;
}

function fakeSubscribe(path = "/p") {
  const subs: Sub[] = [];
  const subscribe: SubscribeLike = (topic, params, cb, opts) => {
    const sub: Sub = { topic, params, opts, cb: cb as Frame, open: true };
    subs.push(sub);
    return () => {
      sub.open = false;
    };
  };
  return {
    subs,
    subscribe,
    open: () => subs.filter((x) => x.open),
    snapshot: (mtime: number | null) => {
      for (const x of subs) if (x.open) x.cb({ paths: [{ path, mtime }] }, null, { gen: null });
    },
    change: (mtime: number | null) => {
      for (const x of subs) if (x.open) x.cb(null, { changes: [{ path, mtime }] }, { gen: null });
    },
    fail: () => {
      for (const x of subs) if (x.open) x.cb(null, null, { error: "paths: every entry must be an absolute path", status: 400 });
    },
  };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

const SEG = (text: string) => ({ start: 0, end: 1, text });

test("transcript tail: watches the partial file (not hidden-gated), reads once on the snapshot and once per change", async () => {
  const bus = fakeSubscribe();
  const reads: string[] = [];
  let answer = [SEG("hello")];
  const got: unknown[] = [];
  const off = tailPartialTranscript(
    "/out/clip.partial.jsonl",
    (rows) => got.push(rows),
    bus.subscribe,
    async (p) => {
      reads.push(p);
      return answer as never;
    },
  );
  expect(bus.subs.map((x) => [x.topic, x.params, x.opts])).toEqual([
    ["fs.watch", { paths: ["/out/clip.partial.jsonl"] }, { hiddenOk: false }],
  ]);
  bus.snapshot(null);
  await flush();
  expect(reads).toEqual(["/out/clip.partial.jsonl"]);
  expect(got).toEqual([[SEG("hello")]]);

  answer = [SEG("hello"), SEG("world")];
  bus.change(2);
  await flush();
  expect(reads.length).toBe(2);
  expect(got[got.length - 1]).toEqual([SEG("hello"), SEG("world")]);

  off();
  expect(bus.open()).toEqual([]);
});

test("transcript tail: an empty or failed read keeps what is on screen; an error frame reads nothing", async () => {
  const bus = fakeSubscribe();
  let reads = 0;
  let mode: "empty" | "throw" = "empty";
  const got: unknown[] = [];
  const off = tailPartialTranscript("/p", (rows) => got.push(rows), bus.subscribe, async () => {
    reads += 1;
    if (mode === "throw") throw new Error("404");
    return [];
  });
  bus.snapshot(null);
  await flush();
  mode = "throw";
  bus.change(1);
  await flush();
  expect(reads).toBe(2);
  expect(got).toEqual([]);
  bus.fail();
  await flush();
  expect(reads).toBe(2);
  off();
});

test("transcript tail: a read landing after the unsubscribe is dropped", async () => {
  const bus = fakeSubscribe();
  let release!: (rows: never) => void;
  const got: unknown[] = [];
  const off = tailPartialTranscript("/p", (rows) => got.push(rows), bus.subscribe, () => new Promise((r) => (release = r)));
  bus.snapshot(1);
  off();
  release([SEG("late")] as never);
  await flush();
  expect(got).toEqual([]);
});

test("live preview: a snapshot without the file is not a reload; every reported change is one", () => {
  const bus = fakeSubscribe("/out/img.preview.png");
  let reloads = 0;
  const off = followPreview("/out/img.preview.png", () => (reloads += 1), bus.subscribe);
  expect(bus.subs.map((x) => [x.topic, x.params, x.opts])).toEqual([
    ["fs.watch", { paths: ["/out/img.preview.png"] }, { hiddenOk: false }],
  ]);
  bus.snapshot(null);
  expect(reloads).toBe(0);
  bus.change(1);
  bus.change(2);
  expect(reloads).toBe(2);
  bus.fail();
  expect(reloads).toBe(2);
  off();
  expect(bus.open()).toEqual([]);
});

test("live preview: a snapshot in which the file already exists is a reload (the first frame landed before the watch)", () => {
  const bus = fakeSubscribe("/out/img.preview.png");
  let reloads = 0;
  const off = followPreview("/out/img.preview.png", () => (reloads += 1), bus.subscribe);
  bus.snapshot(7);
  expect(reloads).toBe(1);
  off();
});

test("neither stage re-reads on a timer any more", () => {
  const transcribe = readFileSync(join(import.meta.dir, "TranscribeStage.tsx"), "utf8");
  const image = readFileSync(join(import.meta.dir, "ImageStage.tsx"), "utf8");
  expect(transcribe).not.toContain("window.setInterval(tick");
  expect(transcribe).toContain("return tailPartialTranscript(running.outputPartial, setSegments);");
  expect(image).not.toContain("setInterval");
  expect(image).toContain("return followPreview(rendering.previewPath, () => setPreviewTick((n) => n + 1));");
});
