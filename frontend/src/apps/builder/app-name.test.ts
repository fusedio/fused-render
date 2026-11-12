import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// app-name.ts imports the API module, which needs a browser; the two pure
// functions are read out of the source and evaluated on their own.
import { troubleKind } from "@platform/lib/trouble";

const SRC = readFileSync(join(import.meta.dir, "app-name.ts"), "utf8").replace(
  /^import .*$/gm,
  "",
);
const mod = new Bun.Transpiler({ loader: "ts" }).transformSync(SRC).replace(/^export /gm, "");
const { parseModelName, nameFromPrompt, claudeGap } = new Function(
  "troubleKind",
  `${mod}\nreturn { parseModelName, nameFromPrompt, claudeGap };`,
)(troubleKind) as {
  parseModelName: (reply: string) => string | null;
  nameFromPrompt: (prompt: string) => string;
  claudeGap: (err: unknown) => string | null;
};

// The relay's error envelope as postJson throws it (HttpError.body).
const relayError = (type: string, message: string) => ({
  status: 502,
  body: { ok: false, error: { type, message } },
});

describe("claudeGap", () => {
  test("no Claude Code on the machine", () => {
    expect(
      claudeGap(
        relayError(
          "ai_unavailable",
          "claude binary not found on PATH; install Claude Code or set FUSED_RENDER_CLAUDE_BIN to its location",
        ),
      ),
    ).toBe("install");
  });
  test("Claude Code signed out", () => {
    expect(claudeGap(relayError("ai_error", "Not logged in · Please run /login"))).toBe("login");
    expect(claudeGap(relayError("ai_error", "Invalid API key · Fix external API key"))).toBe("login");
  });
  test("any other failure is not about Claude Code being there", () => {
    expect(claudeGap(relayError("timeout", "claude CLI did not answer within 600s"))).toBeNull();
    expect(claudeGap(relayError("ai_error", "claude CLI exited with code 1"))).toBeNull();
    expect(claudeGap(new TypeError("Failed to fetch"))).toBeNull();
  });
});

describe("parseModelName", () => {
  test("a bare name, with the wrapping models add", () => {
    for (const r of ["gym-tracker", "`gym-tracker`", '"gym-tracker"', "gym-tracker.", "Name: gym_tracker", "CRM"])
      expect(parseModelName(r)).toBe(r === "CRM" ? "crm" : "gym-tracker");
  });
  test("the refusal token", () => {
    expect(parseModelName("none")).toBe("none");
    expect(parseModelName("None.")).toBe("none");
  });
  test("a name on its own line beside prose", () => {
    expect(parseModelName("Here is a name:\n\ngym-tracker")).toBe("gym-tracker");
    expect(parseModelName("gym-tracker\n\nLet me know!")).toBe("gym-tracker");
  });
  test("prose is never a name", () => {
    for (const r of [
      "Sure, I can help you name that project today!",
      "Sure!\n\nI'd call it gym-tracker.",
      "Got it",
      "Let me know!",
      "Gym Tracker",
      "",
    ])
      expect(parseModelName(r)).toBeNull();
  });
});

describe("nameFromPrompt", () => {
  test("the subject words of the first clause, without the ask", () => {
    expect(nameFromPrompt("todo list")).toBe("todo-list");
    expect(nameFromPrompt("Make me a tool that tracks gym workouts")).toBe("tracks-gym-workouts");
    expect(nameFromPrompt("A habit tracker. Layout: a top bar with today's date")).toBe("habit-tracker");
    expect(nameFromPrompt("build a dashboard — it should show sales")).toBe("dashboard");
  });
  test("no subject word, no name", () => {
    expect(nameFromPrompt("make me an app")).toBe("");
    expect(nameFromPrompt("hello, what can you do?")).toBe("");
  });
});
