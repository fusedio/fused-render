// The one "run it if there's somewhere to run it, otherwise copy it" fork
// every "Open in Claude"/"Continue in terminal" affordance shares. See the
// doc comment on runOrCopyInTerminal.ts for which callers this is (and is
// not) for.
import { afterEach, beforeEach, expect, test } from "bun:test";

import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import {
  peekPendingTerminalRequest,
  registerTerminalDrawerMounted,
  resetTerminalDockForTests,
} from "@platform/lib/terminalDockStore";
import { _resetNotificationsForTest, getPopupNotification } from "@platform/lib/notifications";
import { runOrCopyInTerminal } from "@platform/lib/runOrCopyInTerminal";

let writes: string[] = [];
beforeEach(() => {
  writes = [];
  (navigator as unknown as { clipboard: unknown }).clipboard = {
    writeText: (s: string) => {
      writes.push(s);
      return Promise.resolve();
    },
  };
});
afterEach(() => {
  resetTerminalDockForTests();
  _resetNotificationsForTest();
});

test("with a drawer mounted, runs the command there instead of copying", async () => {
  const unregister = registerTerminalDrawerMounted();
  const ran = await runOrCopyInTerminal("claude", { cwd: "/tmp/proj" });
  expect(ran).toBe(true);
  expect(peekPendingTerminalRequest()).toEqual({ cwd: "/tmp/proj", command: "claude" });
  expect(writes).toEqual([]);
  unregister();
});

test("with no drawer mounted, copies `command` to the clipboard by default", async () => {
  const ran = await runOrCopyInTerminal("claude update");
  expect(ran).toBe(false);
  expect(writes).toEqual(["claude update"]);
  expect(getPopupNotification()?.title).toBe("Command copied — paste it in your terminal");
});

test("copyCommand overrides what's copied, for a run command that relies on cwd", async () => {
  const ran = await runOrCopyInTerminal("claude", {
    cwd: "/tmp/proj",
    copyCommand: "cd '/tmp/proj' && claude",
  });
  expect(ran).toBe(false);
  expect(writes).toEqual(["cd '/tmp/proj' && claude"]);
});

test("a failed clipboard write notifies the failure instead", async () => {
  (navigator as unknown as { clipboard: unknown }).clipboard = {
    writeText: () => Promise.reject(new Error("denied")),
  };
  const ran = await runOrCopyInTerminal("claude update");
  expect(ran).toBe(false);
  expect(getPopupNotification()?.title).toBe("Could not copy the command");
});

test("ranMessage is only said when it actually ran", async () => {
  const unregister = registerTerminalDrawerMounted();
  await runOrCopyInTerminal("claude", { cwd: "/tmp/proj", ranMessage: "Opened in terminal" });
  expect(getPopupNotification()?.title).toBe("Opened in terminal");
  unregister();
});

test("with no ranMessage, a successful run stays silent", async () => {
  const unregister = registerTerminalDrawerMounted();
  await runOrCopyInTerminal("claude", { cwd: "/tmp/proj" });
  expect(getPopupNotification()).toBeNull();
  unregister();
});
