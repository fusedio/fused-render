import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// app-name.ts imports the API module, which needs a browser; the source is
// evaluated here with its imports removed and the relay call, the timers and
// the console handed in, so each test supplies its own relay.
import { troubleKind } from "@platform/lib/trouble";

const SRC = readFileSync(join(import.meta.dir, "app-name.ts"), "utf8").replace(
  /^import .*$/gm,
  "",
);
const mod = new Bun.Transpiler({ loader: "ts" }).transformSync(SRC).replace(/^export /gm, "");

type Ai = (
  prompt: string,
  systemPrompt?: string,
  model?: string,
  opts?: { signal?: AbortSignal },
) => Promise<string>;
interface Timers {
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (id: unknown) => void;
}
interface Mod {
  parseModelName: (reply: string) => string | null;
  nameFromPrompt: (prompt: string) => string;
  claudeGap: (err: unknown) => string | null;
  suggestAppName: (
    prompt: string,
  ) => Promise<{ name: string | null; suggestion: string; claude: string | null }>;
}
const quiet = { warn: () => {} };
const load = (ai: Ai, timers: Partial<Timers> = {}): Mod =>
  new Function(
    "troubleKind",
    "aiComplete",
    "setTimeout",
    "clearTimeout",
    "console",
    `${mod}\nreturn { parseModelName, nameFromPrompt, claudeGap, suggestAppName };`,
  )(
    troubleKind,
    ai,
    timers.setTimeout ?? globalThis.setTimeout,
    timers.clearTimeout ?? globalThis.clearTimeout,
    quiet,
  ) as Mod;

const { parseModelName, nameFromPrompt, claudeGap } = load(async () => "");

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
  test("a question is not a description", () => {
    expect(nameFromPrompt("What's your name?")).toBe("");
    expect(nameFromPrompt("How does this work? A recipe organizer")).toBe("recipe-organizer");
  });
  test("a clause that only names the stack is passed over", () => {
    expect(nameFromPrompt("Use React and Tailwind. Build a recipe organizer")).toBe("recipe-organizer");
    expect(nameFromPrompt("a react dashboard")).toBe("react-dashboard");
    expect(nameFromPrompt("Use React and Tailwind")).toBe("");
  });
});

// suggestAppName over a stubbed relay: what the composer is actually handed.
describe("suggestAppName", () => {
  const PROMPT = "Make me a habit tracker";
  const run = (ai: Ai, timers: Partial<Timers> = {}) => load(ai, timers).suggestAppName(PROMPT);

  test("the model's name wins", async () => {
    expect(await run(async () => "habit-log")).toEqual({ name: "habit-log", suggestion: "", claude: null });
  });
  test("a refusal or prose falls to the subject words", async () => {
    const words = { name: "habit-tracker", suggestion: "", claude: null };
    expect(await run(async () => "none")).toEqual(words);
    expect(await run(async () => "Sure, happy to help!")).toEqual(words);
  });
  test("a failure that is not about Claude Code falls to the subject words", async () => {
    expect(
      await run(async () => {
        throw relayError("ai_error", "claude CLI exited with code 1");
      }),
    ).toEqual({ name: "habit-tracker", suggestion: "", claude: null });
  });
  test("Claude Code missing or signed out: no name, the words offered, the reason given", async () => {
    expect(
      await run(async () => {
        throw relayError("ai_unavailable", "claude binary not found on PATH; install Claude Code");
      }),
    ).toEqual({ name: null, suggestion: "habit-tracker", claude: "install" });
    expect(
      await run(async () => {
        throw relayError("ai_error", "Not logged in · Please run /login");
      }),
    ).toEqual({ name: null, suggestion: "habit-tracker", claude: "login" });
  });
  test("no answer by the deadline: the call is aborted and the subject words are the name", async () => {
    let waited = -1;
    let aborted = false;
    const named = await run(
      // A relay that never answers; it only ever settles by the abort.
      (_p, _s, _m, opts) =>
        new Promise<string>((_resolve, reject) => {
          opts!.signal!.addEventListener("abort", () => {
            aborted = true;
            reject(new DOMException("aborted", "AbortError"));
          });
        }),
      {
        // The deadline, fired at once instead of six seconds on.
        setTimeout: (fn, ms) => {
          waited = ms;
          queueMicrotask(fn);
          return 0;
        },
      },
    );
    expect(waited).toBe(6000);
    expect(aborted).toBe(true);
    expect(named).toEqual({ name: "habit-tracker", suggestion: "", claude: null });
  });
});
