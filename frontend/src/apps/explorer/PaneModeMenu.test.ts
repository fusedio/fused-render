import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { createElement } from "react";
import type { TemplateEntry } from "@platform/lib/api";
import { installDomShim } from "@platform/lib/testDomShim";
import PaneModeMenu, { paneModeChoice } from "./PaneModeMenu";
import { ModeMenu } from "./BarMenu";

const code: TemplateEntry = { mode: "code", path: "/tpl/code/template.html", icon: null, conditional: false };
// A node file's preferred node template, as stat ships it (SPEC PT-8).
const udf: TemplateEntry = {
  mode: "canvas_udf",
  path: "/tpl/canvas_udf/template.html",
  icon: null,
  conditional: true,
  node: true,
  prefer: true,
};

describe("paneModeChoice", () => {
  test("before any verdict, the preferred udf is labelled and selected (the path check decides)", () => {
    const c = paneModeChoice([udf, code], null, null);
    expect(c.active.mode).toBe("canvas_udf");
    // So picking Code is a change, and writes ?_mode=code.
    expect(c.selected.mode).toBe("canvas_udf");
    expect(c.defaultEntry.mode).toBe("canvas_udf");
  });

  test("an explicit _mode=code while pending is selected and labelled", () => {
    const c = paneModeChoice([udf, code], null, "code");
    expect(c.active.mode).toBe("code");
    expect(c.selected.mode).toBe("code");
  });

  test("once the verdict is in, label and selection agree", () => {
    const c = paneModeChoice([udf, code], { canvas_udf: true }, null);
    expect(c.active.mode).toBe("canvas_udf");
    expect(c.selected.mode).toBe("canvas_udf");
  });
});

// The component itself, with the conditions call held open (udf pending):
// picking Code from the pane bar's menu writes `?_mode=code`.

// The stat and conditions answers each test sets (each test its own path:
// resolveConditions shares an in-flight call per path).
let statTemplates: TemplateEntry[] = [udf, code];
let verdicts: Record<string, boolean> | null = null; // null: never answers

async function mountBar(path: string, query: string, nav: string[]) {
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(
      createElement(PaneModeMenu, { path, query, onNavigate: (q: string) => nav.push(q), variant: "bar" }),
    );
  });
  for (let i = 0; i < 4; i += 1) {
    await act(async () => {
      await new Promise((res) => setTimeout(res, 0));
    });
  }
  return r;
}

describe("PaneModeMenu (bar)", () => {
  const realFetch = globalThis.fetch;
  beforeEach(() => {
    installDomShim();
    statTemplates = [udf, code];
    verdicts = null;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://127.0.0.1:1777");
      const path = url.searchParams.get("path");
      if (url.pathname === "/api/fs/stat") {
        return new Response(JSON.stringify({ path, templates: statTemplates }), { status: 200 });
      }
      if (verdicts === null) return new Promise<Response>(() => {});
      return new Response(JSON.stringify({ path, conditions: verdicts }), { status: 200 });
    }) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  test("Code picked before the verdict pins ?_mode=code", async () => {
    const nav: string[] = [];
    const r = await mountBar("/x/canvas/prod/user/demo/udf_1.py", "", nav);
    const menu = r.root.findByType(ModeMenu);
    expect(menu.props.active).toBe("canvas_udf");
    await act(async () => {
      menu.props.onSelect("code");
    });
    expect(nav).toEqual(["?_mode=code"]);
    r.unmount();
  });

  test("a pending non-preferred mode named in _mode: picking the default deletes _mode", async () => {
    // `?_mode=reader` while reader's verdict is out: the label falls back to
    // Code (reader has no icon yet), but reader is what the URL selects, so
    // picking Code is a change — back to the default, `_mode` dropped.
    const reader: TemplateEntry = { mode: "reader", path: "/tpl/reader/template.html", icon: null, conditional: true };
    statTemplates = [code, reader];
    const nav: string[] = [];
    const r = await mountBar("/x/pending/notes.py", "?_mode=reader", nav);
    const menu = r.root.findByType(ModeMenu);
    expect(menu.props.active).toBe("code");
    await act(async () => {
      menu.props.onSelect("code");
    });
    expect(nav).toEqual([""]);
    r.unmount();
  });

  test("a pane under a git snapshot never offers a node template", async () => {
    const raw: TemplateEntry = { mode: "raw", path: "/tpl/raw/template.html", icon: null, conditional: false };
    statTemplates = [udf, code, raw];
    verdicts = { canvas_udf: true };
    const live = await mountBar("/x/canvas/prod/user/demo/udf_2.py", "", []);
    expect(live.root.findByType(ModeMenu).props.entries.map((e: { mode: string }) => e.mode)).toEqual([
      "canvas_udf",
      "code",
      "raw",
    ]);
    live.unmount();
    const r = await mountBar("/x/canvas/prod/user/demo/udf_3.py", "?_snapshot=abc1234", []);
    const menu = r.root.findByType(ModeMenu);
    expect(menu.props.entries.map((e: { mode: string }) => e.mode)).toEqual(["code", "raw"]);
    expect(menu.props.active).toBe("code");
    r.unmount();
  });
});
