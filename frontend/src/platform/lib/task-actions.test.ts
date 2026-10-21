import { expect, test } from "bun:test";
import {
  RUN_BLOCKED_HINT,
  TASK_ACTION_LABELS,
  blockedActionLabel,
  terminalActionLabel,
} from "./task-actions";

test("the terminal hand-off names the agent only when there is more than one", () => {
  expect(terminalActionLabel(null, true)).toBe("Continue in terminal");
  expect(terminalActionLabel(null, false)).toBe("New session in terminal");
  expect(terminalActionLabel("Copilot", true)).toBe("Continue with Copilot in terminal");
  expect(terminalActionLabel("Copilot", false)).toBe("New Copilot session in terminal");
});

test("a refused row says why in its label, lower-cased after the dash", () => {
  expect(blockedActionLabel(TASK_ACTION_LABELS.delete, RUN_BLOCKED_HINT)).toBe(
    "Delete task — stop the run first",
  );
  expect(blockedActionLabel(TASK_ACTION_LABELS.archive, "Finish or discard the notes first")).toBe(
    "Archive task — finish or discard the notes first",
  );
  expect(blockedActionLabel(TASK_ACTION_LABELS.delete, null)).toBe("Delete task");
});
