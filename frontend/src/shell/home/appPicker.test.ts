import { expect, test } from "bun:test";
import type { AppInfo } from "@platform/lib/api";
import { appFolderLine, filterPickerApps, pickerApps } from "./appPicker";

const app = (name: string, path: string, extra: Partial<AppInfo> = {}) =>
  ({ name, tag: "", path, entry_html: null, ...extra }) as AppInfo;

test("folder line contracts home to ~", () => {
  expect(appFolderLine(app("a", "/Users/me/Work/a"), "/Users/me")).toBe("~/Work");
  expect(appFolderLine(app("a", "/Users/me/a"), "/Users/me")).toBe("~");
  expect(appFolderLine(app("a", "/opt/apps/a"), "/Users/me")).toBe("/opt/apps");
  expect(appFolderLine(app("a", "/Users/me/Downloads/x/_shared"))).toBe("~/Downloads/x");
});

test("a .fused file shows its containing folder", () => {
  expect(appFolderLine(app("OpenMail", "/Users/me/Downloads/OpenMail.fused"), "/Users/me")).toBe("~/Downloads");
});

test("picker order puts an opened app first", () => {
  const list = [app("aaa", "/x/aaa"), app("zzz", "/x/zzz", { opened_at: 1000 })];
  expect(pickerApps(list)[0].name).toBe("zzz");
});

test("filter matches title, name and folder", () => {
  const list = [app("a1", "/Users/me/Work/a1", { title: "Photo Editor" }), app("b", "/Users/me/Misc/b")];
  expect(filterPickerApps(list, "photo", "/Users/me").length).toBe(1);
  expect(filterPickerApps(list, "a1", "/Users/me").length).toBe(1);
  expect(filterPickerApps(list, "~/misc", "/Users/me")[0].name).toBe("b");
});
