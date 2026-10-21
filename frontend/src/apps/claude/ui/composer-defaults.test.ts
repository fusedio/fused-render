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
  expect(MODELS).toEqual(["fable", "opus", "sonnet", "haiku"]);
  expect(MODELS.map((m) => MODEL_LABELS[m])).toEqual([
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

test("model precedence: param > detected > constant", () => {
  expect(resolveModel("opus", "haiku")).toBe("opus");
  expect(resolveModel(undefined, "haiku")).toBe("haiku");
  expect(resolveModel()).toBe(DEFAULT_MODEL);
  // "" is not an answer at any rank — it is what an unset param/read reads as.
  expect(resolveModel("", "", "")).toBe(DEFAULT_MODEL);
});

test("the chat's own RECORD outranks both", () => {
  // The params are a seed for a chat that does not exist yet (the New task
  // card's deep link, "Fix with AI", the Tasks peek's own two). The record is
  // what the app wrote down for a chat that does — every spawn, every send,
  // every pick — so it leads, or reopening a task undoes a pill its reader
  // moved mid-chat (Akshil, 2026-09-18).
  expect(resolveModel("opus", "haiku", "sonnet")).toBe("sonnet");
  expect(resolveEffort("max", "low", "high")).toBe("high");
  // "" is not an answer at this rank either: a chat with no record falls
  // straight back to the ranking it always had.
  expect(resolveModel("opus", "haiku", "")).toBe("opus");
  expect(resolveEffort("max", "low", "")).toBe("max");
  // …and a record naming something this build does not offer blanks nothing.
  expect(resolveModel(undefined, undefined, "claude-9-turbo"))
    .toBe(DEFAULT_MODEL);
});

test("the retired pinned Fable id still reads as Fable, wherever it comes from", () => {
  // The menu offered "claude-fable-5-1" beside "fable" until 2026-09-18; they
  // were the same model, so only the alias is left. The id is still out there
  // though — in `?model=` links, in this chat's own record, in transcripts — and
  // validated raw against a list that no longer holds it, every one of those is
  // an unknown model: a blank pill and a silent fall back to sonnet.
  expect(resolveModel("claude-fable-5-1")).toBe("fable");
  expect(resolveModel(undefined, "claude-fable-5-1")).toBe("fable");
  // …and wearing the CLI's context qualifier, which this menu does not offer
  // as a row of its own: still the Fable row, not the default.
  expect(resolveModel("claude-fable-5-1[1m]")).toBe("fable");
  expect(resolveModel(undefined, undefined, "fable[1m]")).toBe("fable");
  expect(resolveModel(undefined, undefined, "claude-fable-5-1"))
    .toBe("fable");
  // Any Fable spelling, not just that one id — a transcript names the dated
  // build, and `agent._short_model` folds its own side the same way.
  expect(resolveModel("claude-fable-5-1-20260401")).toBe("fable");
  expect(resolveModel("claude-fable-5")).toBe("fable");
  // …and the alias itself is untouched, which is the ordinary case.
  expect(resolveModel("fable")).toBe("fable");
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

test("permission ranks recorded > param > global pref > auto; unknown is the strictest", () => {
  expect(resolvePermission("auto")).toBe("auto");
  expect(resolvePermission("acceptEdits")).toBe("acceptEdits");
  expect(resolvePermission("plan")).toBe("plan");
  // Absent everywhere: the user's default, "auto" unless they changed it.
  expect(resolvePermission()).toBe("auto");
  expect(resolvePermission(undefined, undefined, "prompt")).toBe("prompt");
  expect(resolvePermission("plan", undefined, "prompt")).toBe("plan");
  expect(resolvePermission("plan", "acceptEdits", "prompt")).toBe("acceptEdits");
  // Present but not a mode: the safety fallback, never a looser rank below it.
  expect(resolvePermission("yolo")).toBe("prompt");
  expect(resolvePermission("yolo", undefined, "auto")).toBe("prompt");
  expect(resolvePermission(undefined, undefined, "yolo")).toBe("prompt");
});
