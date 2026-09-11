import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";

const {
  DEFAULT_EFFORT,
  DEFAULT_MODEL,
  DEFAULT_PERMISSION,
  EFFORTS,
  MODELS,
  MODEL_LABELS,
  PERMISSION_LABELS,
  PERMISSION_MODES,
  PERMISSION_SHORT,
  resolveEffort,
  resolveModel,
  resolvePermission,
} = await import("./composer-defaults");

test("the lists and labels are the CLI's vocabulary, verbatim (T:11823-11876)", () => {
  expect(MODELS).toEqual([
    "claude-fable-5-1",
    "fable",
    "opus",
    "sonnet",
    "haiku",
  ]);
  expect(MODELS.map((m) => MODEL_LABELS[m])).toEqual([
    "Fable 5.1",
    "Fable",
    "Opus",
    "Sonnet",
    "Haiku",
  ]);
  expect(EFFORTS).toEqual(["low", "medium", "high", "xhigh", "max"]);
  expect(PERMISSION_MODES).toEqual(["plan", "prompt", "acceptEdits", "auto"]);
  expect(PERMISSION_MODES.map((m) => PERMISSION_LABELS[m])).toEqual([
    "plan first",
    "ask every time",
    "auto-accept edits",
    "Claude decides",
  ]);
  expect(PERMISSION_MODES.map((m) => PERMISSION_SHORT[m])).toEqual([
    "plan",
    "ask",
    "edits",
    "decides",
  ]);
  expect([DEFAULT_MODEL, DEFAULT_EFFORT, DEFAULT_PERMISSION]).toEqual([
    "sonnet",
    "medium",
    "prompt",
  ]);
});

test("model precedence: param > detected > pref > constant", () => {
  expect(resolveModel("opus", "haiku", "fable")).toBe("opus");
  expect(resolveModel(undefined, "haiku", "fable")).toBe("haiku");
  expect(resolveModel(undefined, undefined, "fable")).toBe("fable");
  expect(resolveModel()).toBe(DEFAULT_MODEL);
  // "" is not an answer at any rank — it is what an unset param/read reads as.
  expect(resolveModel("", "", "")).toBe(DEFAULT_MODEL);
});

test("a pinned full id is an ORDINARY value and survives a reload (T:11890)", () => {
  expect(resolveModel("claude-fable-5-1")).toBe("claude-fable-5-1");
});

test("an unknown answer falls back rather than blanking the pill (T:11895)", () => {
  expect(resolveModel("claude-9-turbo")).toBe(DEFAULT_MODEL);
  expect(resolveModel(undefined, "not-a-model")).toBe(DEFAULT_MODEL);
  expect(resolveModel(undefined, undefined, "also-not")).toBe(DEFAULT_MODEL);
});

test("effort ranks param > detected only — prefs never reach it (T:11905)", () => {
  expect(resolveEffort("max", "low")).toBe("max");
  expect(resolveEffort(undefined, "low")).toBe("low");
  expect(resolveEffort()).toBe(DEFAULT_EFFORT);
  expect(resolveEffort("turbo")).toBe(DEFAULT_EFFORT);
});

test("permission is the param or the STRICTEST default — never detected", () => {
  expect(resolvePermission("auto")).toBe("auto");
  expect(resolvePermission("acceptEdits")).toBe("acceptEdits");
  expect(resolvePermission("plan")).toBe("plan");
  expect(resolvePermission()).toBe("prompt");
  expect(resolvePermission("yolo")).toBe("prompt");
});
