import { describe, expect, test } from "bun:test";
import type { AppInfo } from "@platform/lib/api";
import { mergeApps } from "./mergeApps";

const app = (path: string, title = path) => ({ path, name: path, title }) as unknown as AppInfo;

describe("mergeApps", () => {
  test("home first, deduped by path, then catalog order", () => {
    const out = mergeApps([app("/c"), app("/a")], [app("/a"), app("/b"), app("/c"), app("/d")], "opened");
    expect(out.map((a) => a.path)).toEqual(["/c", "/a", "/b", "/d"]);
  });
  test("name sort orders the merged list", () => {
    const out = mergeApps([app("/z", "Zed"), app("/m", "Mid")], [app("/a", "Alpha")], "name");
    expect(out.map((a) => a.title)).toEqual(["Alpha", "Mid", "Zed"]);
  });
});
