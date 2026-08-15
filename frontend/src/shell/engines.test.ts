import { describe, expect, it } from "bun:test";
import type { CapabilityEngine } from "@platform/lib/api";
import {
  capabilityLabel,
  choiceReason,
  ignoredWarning,
  servingLine,
  wouldChangeEngine,
} from "@shell/engines";

const AUTO = "auto";

// An Apple Silicon machine: both whisper runners available, MLX first.
function mac(over: Partial<CapabilityEngine> = {}): CapabilityEngine {
  return {
    capability: "automatic-speech-recognition",
    selected: AUTO,
    effective: "mlx-whisper",
    effectiveLabel: "MLX Whisper (Apple Silicon)",
    ignoredReason: null,
    choices: [
      {
        code: "mlx-whisper",
        label: "MLX Whisper (Apple Silicon)",
        note: "Transcribes on the GPU.",
        available: true,
        reason: null,
      },
      {
        code: "faster-whisper",
        label: "faster-whisper (CTranslate2)",
        note: null,
        available: true,
        reason: null,
      },
    ],
    ...over,
  };
}

// The same preferences file, opened on a Windows box — the case the whole
// ignore-rather-than-honour rule exists for.
function windows(): CapabilityEngine {
  return mac({
    selected: "mlx-whisper",
    effective: "faster-whisper",
    effectiveLabel: "faster-whisper (CTranslate2)",
    ignoredReason:
      "needs Apple Silicon — MLX runs on Metal only (this is windows/amd64)",
    choices: [
      {
        code: "mlx-whisper",
        label: "MLX Whisper (Apple Silicon)",
        note: "Transcribes on the GPU.",
        available: false,
        reason: "needs Apple Silicon — MLX runs on Metal only (this is windows/amd64)",
      },
      {
        code: "faster-whisper",
        label: "faster-whisper (CTranslate2)",
        note: null,
        available: true,
        reason: null,
      },
    ],
  });
}

describe("servingLine", () => {
  it("reports the EFFECTIVE runner, not the selected one", () => {
    // The control shows the choice; this line reports reality. They differ
    // whenever a preference could not be honoured, and the line that matters
    // is the one saying what actually transcribes.
    expect(servingLine(windows())).toContain("faster-whisper (CTranslate2)");
    expect(servingLine(windows())).not.toContain("MLX");
  });

  it("says so plainly when nothing serves the capability here", () => {
    const row = mac({ effective: null, effectiveLabel: null });
    expect(servingLine(row)).toContain("Nothing on this machine");
  });
});

describe("ignoredWarning", () => {
  it("is silent while the selection is in force", () => {
    // Including "auto", which is honoured by definition — a warning on every
    // fresh machine teaches the user to ignore warnings.
    expect(ignoredWarning(mac())).toBeNull();
    expect(ignoredWarning(mac({ selected: "faster-whisper", effective: "faster-whisper" })))
      .toBeNull();
  });

  it("names the choice, the reason, and that the choice is KEPT", () => {
    const warning = ignoredWarning(windows()) ?? "";
    expect(warning).toContain("MLX Whisper");
    // The registry's own sentence, passed through rather than reworded — the
    // page cannot know this and must not paraphrase it.
    expect(warning).toContain("needs Apple Silicon");
    expect(warning).toContain("windows/amd64");
    // The half a user needs in order not to re-set it on the way back: a
    // synced prefs.json is the reason this preference can be un-honourable.
    expect(warning.toLowerCase()).toContain("kept");
  });
});

describe("choiceReason", () => {
  it("always explains a disabled option", () => {
    const [mlx] = windows().choices;
    expect(choiceReason(mlx)).toContain("Apple Silicon");
  });

  it("falls back rather than leaving a dead control unexplained", () => {
    // A null reason on an unavailable runner should not be reachable, but a
    // radio that cannot be clicked and says nothing about why is the exact
    // failure the greying-out rule is there to prevent.
    const reason = choiceReason({
      code: "x", label: "X", note: null, available: false, reason: null,
    });
    expect(reason).toBeTruthy();
  });

  it("shows the note for an option that IS available", () => {
    const [mlx] = mac().choices;
    expect(choiceReason(mlx)).toBe("Transcribes on the GPU.");
  });
});

describe("wouldChangeEngine", () => {
  it("is true for a usable runner that is not the one running", () => {
    // This is what earns the warning before writing: the switch unloads the
    // resident model and changes the AI Models suggestions.
    expect(wouldChangeEngine(mac(), "faster-whisper", AUTO)).toBe(true);
  });

  it("is false for the option already selected", () => {
    expect(wouldChangeEngine(mac(), AUTO, AUTO)).toBe(false);
  });

  it("is false for a runner this machine cannot run", () => {
    // Choosing it changes what is STORED and nothing else — no model is
    // unloaded and no suggestion list moves, so warning would be a lie.
    const row = windows();
    expect(wouldChangeEngine({ ...row, selected: AUTO }, "mlx-whisper", AUTO)).toBe(false);
  });

  it("is false when clearing an override that was already being ignored", () => {
    // The Windows machine resolves to faster-whisper with the MLX preference
    // stored; going back to auto resolves to faster-whisper too. Nothing moves.
    expect(wouldChangeEngine(windows(), AUTO, AUTO)).toBe(false);
  });

  it("is false when clearing an override that names what auto picks anyway", () => {
    // The common case on the machine the preference was SET on: a Mac user
    // explicitly picks mlx-whisper, which is also what registry order resolves
    // to. Going back to Automatic stores a different value and changes nothing
    // — no worker is stale, so nothing is unloaded, and the suggestion list is
    // identical. Claiming otherwise teaches the user the message means nothing.
    const row = mac({ selected: "mlx-whisper" });
    expect(wouldChangeEngine(row, AUTO, AUTO)).toBe(false);
  });

  it("is true when clearing an override that WAS in force", () => {
    const row = mac({
      selected: "faster-whisper",
      effective: "faster-whisper",
      effectiveLabel: "faster-whisper (CTranslate2)",
    });
    expect(wouldChangeEngine(row, AUTO, AUTO)).toBe(true);
  });
});

describe("capabilityLabel", () => {
  it("reads the Hub's tags as English", () => {
    expect(capabilityLabel("automatic-speech-recognition")).toBe("Speech to text");
  });

  it("renders an unknown capability as ITSELF rather than hiding it", () => {
    // A capability added server-side should appear here — ugly but present —
    // instead of vanishing from the only page that can configure it.
    expect(capabilityLabel("video-generation")).toBe("video-generation");
  });
});
