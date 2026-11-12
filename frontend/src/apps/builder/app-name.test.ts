import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// app-name.ts imports the API module, which needs a browser; the two pure
// functions are read out of the source and evaluated on their own.
const SRC = readFileSync(join(import.meta.dir, "app-name.ts"), "utf8").replace(
  /^import .*$/m,
  "",
);
const mod = new Bun.Transpiler({ loader: "ts" }).transformSync(SRC).replace(/^export /gm, "");
const { parseModelName, nameFromPrompt } = new Function(
  `${mod}\nreturn { parseModelName, nameFromPrompt };`,
)() as {
  parseModelName: (reply: string) => string | null;
  nameFromPrompt: (prompt: string) => string;
};

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
