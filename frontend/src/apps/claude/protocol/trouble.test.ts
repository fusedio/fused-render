// The chat's mapping onto `platform/lib/trouble.ts`. The CLASSIFIER's own
// strings are pinned by tests/test_trouble_parity.py against T:13511-13602 —
// these tests are about the mapping and the two kinds the chat adds.
import { describe, expect, test } from "bun:test";

import { AgentError, AgentNeedsInstall } from "./agent";
import {
  isUnknownRun,
  OTHER_TARGET_ERROR,
  platformKindOf,
  troubleDetailsText,
  troubleFromError,
  troubleFromMessage,
  troubleInstructionsText,
  troubleLink,
  troubleWhat,
  UNKNOWN_RUN_ERROR,
} from "./trouble";

const ctx = { file: "/proj/app.py" };

describe("the named tier is unconditional (T:13534)", () => {
  test("the CLI missing", () => {
    expect(troubleFromMessage("claude cli not found").kind).toBe("cli-missing");
    expect(troubleFromMessage("Claude Code isn't installed").kind).toBe("cli-missing");
  });
  test("signed out", () => {
    expect(troubleFromMessage("please run /login").kind).toBe("login");
    expect(troubleFromMessage("invalid api key").kind).toBe("login");
  });
  test("the plan's limit", () => {
    expect(troubleFromMessage("usage limit reached — resets at 4pm").kind).toBe("limit");
  });
});

describe("the shape tier only counts when the message is ABOUT Claude (T:13540)", () => {
  test("a disk path problem is NOT a download link", () => {
    const t = troubleFromMessage("could not write the incident file: [Errno 2] No such file or directory");
    expect(t.kind).toBe("generic");
  });
  test("the same shape about claude is the CLI missing", () => {
    expect(troubleFromMessage("claude: command not found").kind).toBe("cli-missing");
  });
});

describe("the chat's own kinds", () => {
  test("an unrecognised failure from agent.py is `engine`, from elsewhere `generic`", () => {
    expect(troubleFromMessage("something odd", true).kind).toBe("engine");
    expect(troubleFromMessage("something odd", false).kind).toBe("generic");
  });

  test("a fetch that never left the machine is `network`", () => {
    expect(troubleFromMessage("Failed to fetch").kind).toBe("network");
    expect(troubleFromMessage("Load failed", true).kind).toBe("network");
  });

  test("poll's two refusals are one kind with one recovery (T:17790)", () => {
    expect(isUnknownRun(UNKNOWN_RUN_ERROR)).toBe(true);
    expect(isUnknownRun(OTHER_TARGET_ERROR)).toBe(true);
    expect(isUnknownRun("some other error")).toBe(false);
    expect(isUnknownRun(null)).toBe(false);
    expect(troubleFromMessage(UNKNOWN_RUN_ERROR).kind).toBe("unknown-run");
  });

  test("a venv that is not built is `needs-install`, never the installer flow", () => {
    const err = new AgentNeedsInstall(
      {
        key: "k",
        requirements: ["pandas", "numpy"],
        py: "/p/app.py",
        project: "/p",
        name: "proj",
        pyproject: "/p/pyproject.toml",
      },
      undefined,
    );
    const t = troubleFromError(err);
    expect(t.kind).toBe("needs-install");
    expect(t.detail).toBe("pandas\nnumpy");
  });

  test("an AgentError carries its traceback as the detail", () => {
    const t = troubleFromError(new AgentError({ message: "boom", traceback: "Traceback…" }));
    expect(t.kind).toBe("engine");
    expect(t.message).toBe("boom");
    expect(t.detail).toBe("Traceback…");
  });

  test("a bare throw still classifies", () => {
    expect(troubleFromError(new Error("please run /login")).kind).toBe("login");
    expect(troubleFromError("odd string").kind).toBe("generic");
  });
});

describe("rendering + copy blocks", () => {
  test("the chat kinds that have card copy keep it; the rest fall to `raw`", () => {
    expect(platformKindOf("cli-missing")).toBe("notfound");
    expect(platformKindOf("login")).toBe("login");
    expect(platformKindOf("limit")).toBe("limit");
    expect(platformKindOf("engine")).toBe("raw");
    expect(platformKindOf("unknown-run")).toBe("raw");
    expect(platformKindOf("needs-install")).toBe("raw");
  });

  test("`what` reads as the template's own sentence (T:13692)", () => {
    expect(troubleWhat("/proj/app.py")).toBe("using the chat on /proj/app.py");
    expect(troubleWhat(null)).toBe("using the chat on this folder");
  });

  test("the details block names what the app was doing and the target", () => {
    const t = troubleFromMessage("claude not found");
    const report = troubleDetailsText(t, ctx);
    expect(report).toContain("Fused Render — problem report");
    expect(report).toContain("What the app was doing: using the chat on /proj/app.py");
    expect(report).toContain("claude not found");
    expect(report).toContain("#troubleshooting-notfound");
  });

  test("the agent brief carries the per-case steps and the where-to-look block", () => {
    const brief = troubleInstructionsText(troubleFromMessage("please run /login"), ctx);
    expect(brief).toContain("Sign in: run `claude`, then `/login`");
    expect(brief).toContain("Where to look on this machine:");
    expect(brief).toContain("~/.fused-render");
    expect(brief).toContain('ls -t "${TMPDIR:-/tmp}"/fused-render-*.log | head -3');
  });

  test("the detail (a traceback) is what gets copied when there is one", () => {
    const t = troubleFromError(new AgentError({ message: "boom", traceback: "line 1\nline 2" }));
    expect(troubleDetailsText(t, ctx)).toContain("line 1\nline 2");
  });

  test("the help link is the deep link for the rendered kind", () => {
    expect(troubleLink(troubleFromMessage("usage limit"))).toBe(
      "https://render.fused.io/#troubleshooting-limit",
    );
    expect(troubleLink(troubleFromMessage(UNKNOWN_RUN_ERROR))).toBe(
      "https://render.fused.io/#troubleshooting-raw",
    );
  });
});
