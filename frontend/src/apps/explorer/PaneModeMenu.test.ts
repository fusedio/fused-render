import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { createElement } from "react";
import type { TemplateEntry } from "@platform/lib/api";
import { installDomShim } from "@platform/lib/testDomShim";
import PaneModeMenu, { paneModeChoice } from "./PaneModeMenu";
import { ModeMenu } from "./BarMenu";

const code: TemplateEntry = { mode: "code", path: "/tpl/code/template.html", icon: null, conditional: false };
// A node file's preferred node view, as stat ships it (SPEC PT-8).
const udf: TemplateEntry = {
  mode: "udf",
  path: "/tpl/udf/native",
  icon: null,
  conditional: true,
  native: true,
  prefer: true,
};

describe("paneModeChoice", () => {
  test("while the preferred udf is pending: labelled Code, but udf is what is selected", () => {
    const c = paneModeChoice([udf, code], null, null);
    expect(c.active.mode).toBe("code");
    // So picking Code is a change, and writes ?_mode=code.
    expect(c.selected.mode).toBe("udf");
    expect(c.defaultEntry.mode).toBe("udf");
  });

  test("an explicit _mode=code while pending is selected and labelled", () => {
    const c = paneModeChoice([udf, code], null, "code");
    expect(c.active.mode).toBe("code");
    expect(c.selected.mode).toBe("code");
  });

  test("once the verdict is in, label and selection agree", () => {
    const c = paneModeChoice([udf, code], { udf: true }, null);
    expect(c.active.mode).toBe("udf");
    expect(c.selected.mode).toBe("udf");
  });
});

// The component itself, with the conditions call held open (udf pending):
// picking Code from the pane bar's menu writes `?_mode=code`.

describe("PaneModeMenu (bar)", () => {
  const realFetch = globalThis.fetch;
  beforeEach(() => {
    installDomShim();
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://127.0.0.1:1777");
      if (url.pathname === "/api/fs/stat") {
        return new Response(JSON.stringify({ path: "/x/udf_1.py", templates: [udf, code] }), { status: 200 });
      }
      // /api/fs/conditions: never answers — the udf verdict stays out.
      return new Promise<Response>(() => {});
    }) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  test("Code picked while the preferred udf is pending pins ?_mode=code", async () => {
    const nav: string[] = [];
    let r!: ReturnType<typeof create>;
    await act(async () => {
      r = create(
        createElement(PaneModeMenu, {
          path: "/x/canvas/prod/user/demo/udf_1.py",
          query: "",
          onNavigate: (q: string) => nav.push(q),
          variant: "bar",
        }),
      );
    });
    for (let i = 0; i < 4; i += 1) {
      await act(async () => {
        await new Promise((res) => setTimeout(res, 0));
      });
    }
    const menu = r.root.findByType(ModeMenu);
    expect(menu.props.active).toBe("code");
    await act(async () => {
      menu.props.onSelect("code");
    });
    expect(nav).toEqual(["?_mode=code"]);
    r.unmount();
  });
});
