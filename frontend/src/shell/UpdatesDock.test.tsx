// The Updates chip's wording per state (pure `updatesChip` / `updatesDetail`)
// and the popover's composition, rendered through `UpdatesCardView` with a
// fixed status — no poll, no network, no restart store.
import { expect, test } from "bun:test";
import { create, type ReactTestRendererJSON } from "react-test-renderer";

import type { UpdateStatus } from "@platform/lib/api";
import { downloadProgressLine, UpdatesCardView, updatesChip, updatesDetail } from "@shell/UpdatesDock";

const base: UpdateStatus = {
  state: "idle",
  method: "dmg",
  latest_version: "9.9.9",
  progress: null,
  progress_total: null,
  error: null,
  manual_command: null,
};
const st = (over: Partial<UpdateStatus>): UpdateStatus => ({ ...base, ...over });

test("at rest the chip is the running version; no updater reads the same", () => {
  expect(updatesChip(null, "ready", "0.6.22")).toEqual({ label: "v0.6.22", tone: "idle" });
  expect(updatesChip(st({ state: "idle" }), "ready", "0.6.22")).toEqual({ label: "v0.6.22", tone: "idle" });
  expect(updatesChip(null, "ready", null).label).toBe("Updates");
});

test("a manual press keeps the version on the chip (the Check button carries the words), with a busy line while in flight", () => {
  expect(updatesChip(st({}), "ready", "1", "checking")).toEqual({ label: "v1", tone: "idle", progress: null });
  expect(updatesChip(st({}), "ready", "1", "current").label).toBe("v1");
  expect(updatesChip(st({ state: "checking" }), "ready", "1").progress).toBeNull();
});

test("every update state has its own label, tone and progress", () => {
  expect(updatesChip(st({ state: "available" }), "ready", "1")).toEqual({ label: "Update available", tone: "on" });
  expect(updatesChip(st({ state: "installing", phase: "downloading", progress: 50, progress_total: 200 }), "ready", "1")).toEqual({
    label: "Downloading",
    tone: "on",
    progress: 0.25,
  });
  expect(updatesChip(st({ state: "installing", phase: "downloading", progress: 50, progress_total: null }), "ready", "1").progress).toBeNull();
  expect(updatesChip(st({ state: "installing", phase: "installing" }), "ready", "1")).toEqual({ label: "Installing", tone: "on", progress: null });
  expect(updatesChip(st({ state: "installed" }), "ready", "1")).toEqual({ label: "Restart to update", tone: "on" });
  expect(updatesChip(st({ state: "installed" }), "quitting", "1")).toEqual({ label: "Restarting…", tone: "on", progress: null });
  expect(updatesChip(st({ state: "error", error: "boom" }), "ready", "1")).toEqual({ label: "Update failed", tone: "failure" });
});

test("the detail sentence names the version and carries one action", () => {
  expect(updatesDetail(st({ state: "available" }), "ready")?.action?.label).toBe("Download");
  expect(updatesDetail(st({ state: "available", check_only: true }), "ready")?.action).toBeNull();
  expect(updatesDetail(st({ state: "installed" }), "ready")?.action?.label).toBe("Restart now");
  expect(updatesDetail(st({ state: "error", error: "no network" }), "ready")).toMatchObject({ detail: "no network", error: true });
  expect(updatesDetail(st({ state: "idle" }), "ready")).toBeNull();
  expect(downloadProgressLine(st({ state: "installing", phase: "downloading", progress: 12 * 1024 ** 2, progress_total: 180 * 1024 ** 2 }))).toBe("12 MB of 180 MB");
  expect(downloadProgressLine(st({ state: "installing", phase: "installing", progress: 5 }))).toBe("");
});

function texts(node: ReactTestRendererJSON | ReactTestRendererJSON[] | string | null): string[] {
  if (node === null) return [];
  if (typeof node === "string") return [node];
  if (Array.isArray(node)) return node.flatMap(texts);
  return (node.children ?? []).flatMap((c) => texts(c as ReactTestRendererJSON | string));
}

function view(status: UpdateStatus | null, over: Partial<Parameters<typeof UpdatesCardView>[0]> = {}) {
  return create(
    <UpdatesCardView
      status={status}
      stage="ready"
      version="0.6.22"
      phase="rest"
      onCheck={() => {}}
      autoDownload={true}
      onToggleAuto={() => {}}
      collapsed={false}
      onToggle={() => {}}
      {...over}
    />,
  );
}

test("the Check button sits in the bar beside the chip, even collapsed; the popover has the version and the auto-download row", () => {
  const closed = view(st({}), { collapsed: true });
  expect(texts(closed.toJSON()).join(" | ")).toContain("v0.6.22");
  expect(closed.root.findByProps({ className: "upd-check-btn" }).props["data-hint"]).toBe("Check for updates");
  const all = texts(view(st({})).toJSON()).join(" | ");
  expect(all).toContain("Running v0.6.22");
  expect(all).toContain("No update is waiting.");
  expect(all).toContain("Automatically download updates");
  expect(view(st({}), { phase: "current" }).root.findByProps({ className: "upd-check-btn is-current" }).props["data-hint"]).toBe("Up to date · v0.6.22");
  expect(texts(view(st({}), { phase: "failed" }).toJSON()).join(" | ")).toContain("Couldn't check for updates");
  expect(texts(view(st({}), { phase: "checking" }).toJSON()).join(" | ")).toContain("Checking…");
});

test("no updater: the version still shows, the Check button is there but disabled", () => {
  const r = view(null, { hasUpdater: false });
  const all = texts(r.toJSON()).join(" | ");
  expect(all).toContain("Running v0.6.22");
  const btn = r.root.findByProps({ className: "upd-check-btn" });
  expect(btn.props.disabled).toBe(true);
  expect(btn.props["data-hint"]).toContain("aren’t managed");
  expect(all).toContain("Updates aren’t managed from inside the app on this build");
  expect(all).not.toContain("Automatically download updates");
});

test("updater present but store not heard yet: no 'not managed' flash, the Check button stands", () => {
  const r = view(null, { hasUpdater: true });
  const btn = r.root.findByProps({ className: "upd-check-btn" });
  expect(btn.props.disabled).toBe(false);
  expect(btn.props["data-hint"]).toBe("Check for updates");
  expect(texts(r.toJSON()).join(" | ")).not.toContain("managed from inside");
});

test("an update found shows the decision and keeps the Check button", () => {
  const r = view(st({ state: "available" }));
  const all = texts(r.toJSON()).join(" | ");
  expect(all).toContain("v9.9.9 is ready to download.");
  expect(all).toContain("Download");
  expect(r.root.findByProps({ className: "upd-check-btn" }).props["data-hint"]).toBe("Check for updates");
});

test("collapsed: no panel; a pending update keeps the Check button", () => {
  const all = texts(view(st({}), { collapsed: true }).toJSON()).join(" | ");
  expect(all).not.toContain("Running");
  const pending = view(st({ state: "available" }), { collapsed: true });
  expect(texts(pending.toJSON()).join(" | ")).toContain("Update available");
  expect(pending.root.findAllByProps({ className: "upd-check-btn" }).length).toBe(1);
});
