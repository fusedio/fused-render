// AppModelGate: the first-open AI-model overview. Driven with react-test-renderer
// and a stubbed `globalThis.fetch` (NOT mock.module: that replaces a module for
// the whole bun process and breaks unrelated files).
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppModelOverview, AppModelRow } from "@platform/lib/api";
import { AppModelGate } from "./AppModelGate";
import { footerHint, rowDetail, rowValue, substitutionsOf, rowProgress, pendingRows } from "./appModelGateLogic";

const row = (over: Partial<AppModelRow> = {}): AppModelRow => ({
  capability: "text-to-image", verb: "image", label: "FLUX.2 klein 4B",
  model: "black-forest-labs/FLUX.2-klein-4B", use: "Image generation", provider: "local",
  sizeGb: 7.8, downloaded: false, swappable: false, choice: "black-forest-labs/FLUX.2-klein-4B",
  candidates: [], jobId: null, ...over,
});
const textRow = (over: Partial<AppModelRow> = {}) =>
  row({ capability: "text-generation", verb: "text", label: "Qwen3 4B", model: "Qwen/q", choice: "Qwen/q",
        use: "Captions", sizeGb: 2.5, downloaded: true, ...over });

const overview = (rows: AppModelRow[], over: Partial<AppModelOverview> = {}): AppModelOverview => ({
  declares: true, accepted: false, appName: "Poster Maker", appDir: "/apps/poster", hash: "h",
  rows, totalGb: 10.3, needGb: 7.8, etaMin: 7, ...over,
});

// ---- fetch stub ------------------------------------------------------------
type Call = { method: string; url: string; body: any };
let calls: Call[];
let ov: AppModelOverview | Error;
let jobs: any[];
const realFetch = globalThis.fetch;

beforeEach(() => {
  calls = [];
  jobs = [];
  installPortalContainer();
  globalThis.fetch = (async (input: any, init: any = {}) => {
    const url = String(input);
    const method = (init.method || "GET").toUpperCase();
    calls.push({ method, url, body: init.body ? JSON.parse(init.body) : null });
    const ok = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { "content-type": "application/json" } });
    if (url.includes("/api/app-models/overview")) {
      if (ov instanceof Error) throw ov;
      return ok(ov);
    }
    if (url.includes("/api/app-models/accept")) return ok({ ok: true, hash: "h" });
    if (url.includes("/api/ai/runtime/download")) return ok({ jobId: "sys:ai-model:" + calls[calls.length - 1].body.model, model: "m", state: "running" });
    if (url.includes("/api/jobs") && url.endsWith("/cancel")) return ok({});
    if (url.includes("/api/jobs")) return ok({ jobs, now: 1 });
    return ok({});
  }) as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  removePortalContainer();
});

const flush = async () => { for (let i = 0; i < 5; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };

async function mount(onDecline = () => {}, pollMs = 20): Promise<ReactTestRenderer> {
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(
      <AppModelGate path="/apps/poster/index.html" onDecline={onDecline} pollMs={pollMs}>
        <main id="app">APP</main>
      </AppModelGate>,
    );
  });
  await flush();
  return r;
}

const text = (r: ReactTestRenderer) =>
  r.root.findAll(() => true).filter((n) => typeof n.type === "string")
    .flatMap((n) => n.children.filter((c) => typeof c === "string") as string[]).join("|");
const hasApp = (r: ReactTestRenderer) => r.root.findAll((n) => n.props?.id === "app").length > 0;
const button = (r: ReactTestRenderer, label: string) =>
  r.root.findAll((n) => n.type === "button" && n.children.join("").includes(label))[0];
const press = async (r: ReactTestRenderer, label: string) => {
  const b = button(r, label);
  expect(b).toBeDefined();
  await act(async () => { await b.props.onClick(); });
  await flush();
};

describe("gate states", () => {
  test("children are held while the overview loads", async () => {
    let release!: () => void;
    globalThis.fetch = (() => new Promise((res) => { release = () => res(new Response(JSON.stringify({ declares: false }))); })) as any;
    let r!: ReactTestRenderer;
    await act(async () => { r = create(<AppModelGate path="/a/index.html" onDecline={() => {}}><i id="app" /></AppModelGate>); });
    expect(hasApp(r)).toBe(false);
    await act(async () => { release(); });
    await flush();
    expect(hasApp(r)).toBe(true);
  });

  test("declares:false and accepted:true render children", async () => {
    ov = { declares: false } as AppModelOverview;
    expect(hasApp(await mount())).toBe(true);
    ov = overview([row()], { accepted: true });
    expect(hasApp(await mount())).toBe(true);
  });

  test("a failed fetch fails open", async () => {
    ov = new Error("boom");
    expect(hasApp(await mount())).toBe(true);
  });

  test("non-html paths never fetch", async () => {
    let r!: ReactTestRenderer;
    await act(async () => { r = create(<AppModelGate path="/a/data.csv" onDecline={() => {}}><i id="app" /></AppModelGate>); });
    expect(hasApp(r)).toBe(true);
    expect(calls.length).toBe(0);
  });
});

describe("review dialog", () => {
  test("rows show size, On this Mac, Built in and Cloud values and the footer hint", async () => {
    ov = overview([
      row(), textRow(),
      row({ capability: "automatic-speech-recognition", verb: "transcribe", label: "afm-speech", model: "afm-speech", provider: "apple", sizeGb: null, downloaded: true, use: "Transcription" }),
      row({ capability: "text-classification", verb: "decide", label: "sonnet", model: "sonnet", provider: "cloud", sizeGb: null, downloaded: true, use: "Classification" }),
    ]);
    const r = await mount();
    expect(hasApp(r)).toBe(false);
    const t = text(r);
    for (const s of ["Before you open", "FLUX.2 klein 4B", "7.8 GB", "On this Mac", "Built in", "Cloud", "7.8 GB · about 7 min"]) expect(t).toContain(s);
    expect(t).toContain("sends data off this machine");
    expect(button(r, "Download")).toBeDefined();
  });

  test("Change shows only on swappable rows with candidates, and picking one shows the amber callout", async () => {
    const swap = row({ swappable: true, candidates: [{ id: "other/z", label: "Z-Image Turbo", sizeGb: 3 }] });
    ov = overview([swap, textRow({ swappable: true })]);
    const r = await mount();
    expect(r.root.findAll((n) => n.type === "button" && n.props.className === "am-change").length).toBe(1);
    await press(r, "Change");
    const select = r.root.findByType("select");
    await act(async () => { select.props.onChange({ target: { value: "other/z" } }); });
    await flush();
    expect(text(r).split("|").join("")).toContain("Poster Maker was built with FLUX.2 klein 4B. Results may differ.");
    expect(text(r)).toContain("Nothing to download");
    expect(button(r, "Open")).toBeDefined();
  });

  test("Not now calls onDecline and posts nothing", async () => {
    ov = overview([row()]);
    let declined = 0;
    const r = await mount(() => { declined++; });
    await press(r, "Not now");
    expect(declined).toBe(1);
    expect(calls.filter((c) => c.method === "POST").length).toBe(0);
    expect(hasApp(r)).toBe(false);
    expect(text(r)).toContain("Review models");
  });

  test("Download accepts, then downloads only the rows not on disk, then shows progress", async () => {
    ov = overview([row(), textRow()]);
    const r = await mount();
    await press(r, "Download");
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts[0].url).toContain("/api/app-models/accept");
    expect(posts[0].body.substitutions).toEqual({});
    const downloads = posts.filter((c) => c.url.includes("/api/ai/runtime/download"));
    expect(downloads.map((d) => d.body.model)).toEqual(["black-forest-labs/FLUX.2-klein-4B"]);
    expect(text(r)).toContain("Getting ready");
    expect(hasApp(r)).toBe(false);
  });

  test("nothing to download: Open accepts and opens the app", async () => {
    ov = overview([textRow()], { needGb: 0, etaMin: null });
    const r = await mount();
    await press(r, "Open");
    expect(calls.some((c) => c.url.includes("/api/ai/runtime/download"))).toBe(false);
    expect(hasApp(r)).toBe(true);
  });
});

describe("progress view", () => {
  test("opens the app once the jobs finish and the overview confirms", async () => {
    ov = overview([row()]);
    const r = await mount();
    await press(r, "Download");
    jobs = [{ id: "sys:ai-model:black-forest-labs/FLUX.2-klein-4B", state: "running", done: 5, total: 10, message: "" }];
    await act(async () => { await new Promise((res) => setTimeout(res, 40)); });
    expect(hasApp(r)).toBe(false);
    expect(text(r)).toContain("50%");
    jobs = [{ id: "sys:ai-model:black-forest-labs/FLUX.2-klein-4B", state: "done", done: 10, total: 10, message: "" }];
    ov = overview([row({ downloaded: true })]);
    await act(async () => { await new Promise((res) => setTimeout(res, 60)); });
    await flush();
    expect(hasApp(r)).toBe(true);
  });

  test("Cancel download cancels each job and declines", async () => {
    ov = overview([row()]);
    let declined = 0;
    const r = await mount(() => { declined++; });
    await press(r, "Download");
    await press(r, "Cancel download");
    expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/cancel") && c.url.includes("FLUX"))).toBe(true);
    expect(declined).toBe(1);
    expect(hasApp(r)).toBe(false);
  });
});

describe("pure helpers", () => {
  test("value and detail wording", () => {
    expect(rowValue(row({ downloaded: true }))).toBe("On this Mac");
    expect(rowValue(row({ provider: "apple", sizeGb: null, downloaded: true }))).toBe("Built in");
    expect(rowDetail(row({ provider: "cloud", use: "Summaries" }))).toContain("off this machine");
  });
  test("footer hint and substitutions", () => {
    const rows = [row({ swappable: true, candidates: [{ id: "o/z", label: "Z", sizeGb: 3 }] })];
    expect(footerHint(rows, {}, 6)).toBe("7.8 GB · about 6 min");
    expect(footerHint(rows, { "text-to-image": "o/z" }, 6)).toBe("Nothing to download");
    expect(substitutionsOf(rows, { "text-to-image": "o/z" })).toEqual({ "text-to-image": "o/z" });
    expect(substitutionsOf(rows, { "text-to-image": rows[0].model })).toEqual({});
    expect(pendingRows(rows, {}).length).toBe(1);
  });
  test("rowProgress from jobs", () => {
    const r = row();
    expect(rowProgress(r, "j", []).state).toBe("waiting");
    expect(rowProgress(r, "j", [{ id: "j", state: "error", message: "disk full" } as any])).toEqual({ state: "error", message: "disk full" });
  });
});
