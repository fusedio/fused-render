import { describe, expect, it } from "bun:test";
import type { TemplateEntry, WalkEntry } from "@platform/lib/api";
import {
  ancestorsOf,
  buildTree,
  contentTemplates,
  renderSrc,
  safeRel,
} from "./app-files-lib";

const w = (rel: string, is_dir = false): WalkEntry => ({ rel, is_dir, size: null, mtime: null });

describe("buildTree", () => {
  it("nests by rel, folders first then files, case-insensitive", () => {
    const tree = buildTree([
      w("zeta.py"),
      w("Alpha.md"),
      w("scripts", true),
      w("scripts/b.py"),
      w("scripts/a.py"),
      w("assets", true),
    ]);
    expect(tree.map((n) => n.name)).toEqual(["assets", "scripts", "Alpha.md", "zeta.py"]);
    expect(tree[1].children.map((n) => n.rel)).toEqual(["scripts/a.py", "scripts/b.py"]);
  });

  it("implies a folder the walk never emitted", () => {
    const tree = buildTree([w("deep/er/file.txt")]);
    expect(tree[0].rel).toBe("deep");
    expect(tree[0].children[0].rel).toBe("deep/er");
    expect(tree[0].children[0].children[0].name).toBe("file.txt");
  });
});

describe("safeRel", () => {
  it("accepts plain relative posix paths and nothing else", () => {
    expect(safeRel("a/b.csv")).toBe("a/b.csv");
    expect(safeRel(null)).toBeNull();
    expect(safeRel("")).toBeNull();
    expect(safeRel("/etc/passwd")).toBeNull();
    expect(safeRel("../x")).toBeNull();
    expect(safeRel("a/../b")).toBeNull();
    expect(safeRel("a//b")).toBeNull();
    expect(safeRel("a\\b")).toBeNull();
    expect(safeRel("./a")).toBeNull();
  });
});

describe("ancestorsOf", () => {
  it("lists every folder above a rel path, shallowest first", () => {
    expect(ancestorsOf("a/b/c.txt")).toEqual(["a", "a/b"]);
    expect(ancestorsOf("top.txt")).toEqual([]);
  });
});

describe("contentTemplates", () => {
  const t = (mode: string, path: string | null = "/t/" + mode): TemplateEntry => ({
    mode,
    path,
    icon: null,
  });
  it("drops companions, the listing sentinel and unknown sentinels; keeps _render", () => {
    const out = contentTemplates([
      t("html"),
      t("claude"),
      t("_render", null),
      t("_listing", null),
      t("_mystery", null),
      t("code"),
    ]);
    expect(out.map((e) => e.mode)).toEqual(["html", "_render", "code"]);
  });
});

describe("renderSrc", () => {
  it("renders the file itself for _render and through the template otherwise", () => {
    expect(renderSrc("/x/a b.html", { mode: "_render", path: null, icon: null })).toBe(
      "/render?path=%2Fx%2Fa%20b.html",
    );
    expect(renderSrc("/x/a.csv", { mode: "table", path: "/tpl/table", icon: null })).toBe(
      "/render?path=%2Ftpl%2Ftable&_file=%2Fx%2Fa.csv",
    );
  });
});
