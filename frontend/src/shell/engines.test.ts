import { describe, expect, it } from "bun:test";
import type { CapabilityEngine, Prefs } from "@platform/lib/api";
import {
  capabilityLabel,
  choiceReason,
  engineNote,
  ignoredWarning,
  servingLine,
  switchOutcome,
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
    effectiveShortLabel: "MLX Whisper",
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
        label: "Faster Whisper (CTranslate2)",
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
    effectiveLabel: "Faster Whisper (CTranslate2)",
    effectiveShortLabel: "Faster Whisper",
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
        label: "Faster Whisper (CTranslate2)",
        note: null,
        available: true,
        reason: null,
      },
    ],
  });
}

// What running on the backend that is actually serving this capability is
// LIKE. It used to sit over the Discover tab's capability sections, where only
// three of six runners had one to show and the page came out blotchy — and
// where the memory ceiling on MLX FLUX was nowhere near the control that
// answers it. Here it is beside the decision it is about.
describe("engineNote", () => {
  it("describes the engine that is actually serving", () => {
    expect(engineNote(mac())).toBe("Transcribes on the GPU.");
  });

  it("describes the EFFECTIVE engine, never the one that was asked for", () => {
    // The Windows box stored a preference for MLX Whisper and got CTranslate2.
    // "Transcribes on the GPU." under that row would be a sentence about a
    // backend this machine is not running — the same discipline `servingLine`
    // follows one line above it.
    expect(engineNote(windows())).toBe(null);
  });

  it("is null for a runner with nothing worth saying", () => {
    // Most runners have no note, and the row simply has no second line. An
    // empty string would still be a rendered element.
    expect(engineNote(mac({ effective: "faster-whisper" }))).toBe(null);
  });

  it("is null when nothing serves the capability", () => {
    // There is no engine, so there is nothing it is like. The row's own
    // "Not available on this machine." is the whole story.
    expect(engineNote(mac({ effective: null }))).toBe(null);
  });
});

describe("servingLine", () => {
  it("reports the EFFECTIVE runner, not the selected one", () => {
    // The control shows the choice; this line reports reality. They differ
    // whenever a preference could not be honoured, and the line that matters
    // is the one saying what actually transcribes.
    // The SHORT name: this line sits under the picker, which is the one
    // surface that keeps the platform qualifier. What the test is about is
    // WHICH runner gets named, and that is unchanged.
    expect(servingLine(windows())).toContain("Faster Whisper");
    expect(servingLine(windows())).not.toContain("CTranslate2");
    expect(servingLine(windows())).not.toContain("MLX");
  });

  it("says so plainly when nothing serves the capability here", () => {
    const row = mac({ effective: null, effectiveLabel: null, effectiveShortLabel: null });
    expect(servingLine(row)).toContain("Not available");
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

  it("names the choice and the reason, in one sentence", () => {
    const warning = ignoredWarning(windows()) ?? "";
    expect(warning).toContain("MLX Whisper");
    // The registry's own sentence, passed through rather than reworded — the
    // page cannot know this and must not paraphrase it.
    expect(warning).toContain("needs Apple Silicon");
    expect(warning).toContain("windows/amd64");
    // The whole line, asserted as content rather than by counting full stops:
    // the warning earns its place by being the only thing that contradicts the
    // still-selected option above it, and what must not come back is the
    // second sentence this used to carry — reassurance that the choice is kept
    // for another machine, which the selection itself already says.
    expect(warning).toBe(
      "MLX Whisper (Apple Silicon) is not used here — needs Apple Silicon — MLX runs on"
      + " Metal only (this is windows/amd64).",
    );
  });
});

describe("choiceReason", () => {
  it("always explains a disabled option", () => {
    const [mlx] = windows().choices;
    expect(choiceReason(mlx)).toContain("Apple Silicon");
  });

  it("falls back rather than leaving a dead control unexplained", () => {
    // A null reason on an unavailable runner should not be reachable, but a
    // menu item that cannot be picked and says nothing about why is the exact
    // failure the greying-out rule is there to prevent — and in a dropdown the
    // label is the only place it could have said anything.
    const reason = choiceReason({
      code: "x", label: "X", note: null, available: false, reason: null,
    });
    expect(reason).toBeTruthy();
  });

  it("says nothing at all about an option that IS available", () => {
    // The runner's note ("Transcribes on the GPU") is editorial and belongs on
    // the AI Models page, where somebody is deciding what to download. Here it
    // was one more sentence after every label on a settings page.
    const [mlx] = mac().choices;
    expect(mlx.note).toBeTruthy(); // the note exists…
    expect(choiceReason(mlx)).toBeNull(); // …and this control does not use it
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
      effectiveLabel: "Faster Whisper (CTranslate2)",
      effectiveShortLabel: "Faster Whisper",
    });
    expect(wouldChangeEngine(row, AUTO, AUTO)).toBe(true);
  });
});

// What a PUT did, which the page turns into two things: the sentence under the
// control, and whether the Local tab one click away is now showing an answer
// the switch invalidated — stale engine tags, stale Load refusals, and a Loaded
// badge on a model the server has just evicted.
describe("switchOutcome", () => {
  // Only the field this reads. A whole Prefs here would be forty unrelated
  // settings, each one a thing to edit when some other preference is added.
  function reply(unloaded?: string[]): Prefs {
    return { engines: { capabilities: [], auto: AUTO, unloaded } } as unknown as Prefs;
  }

  it("reports the eviction the SERVER reported, not one guessed from here", () => {
    const row = mac();
    expect(switchOutcome(row, "faster-whisper", AUTO, reply(["mlx-community/whisper"]))).toBe(
      "unloaded",
    );
  });

  it("is a plain switch when the engine moved and nothing was resident", () => {
    // The usual case on a fresh app: no transcription has run, so there is no
    // worker to evict — and the page must not claim there was one.
    const row = mac();
    expect(switchOutcome(row, "faster-whisper", AUTO, reply([]))).toBe("switched");
    expect(switchOutcome(row, "faster-whisper", AUTO, reply())).toBe("switched");
  });

  // The null is the whole reason the page can afford to re-read on the other
  // two: the listing is a walk over every blob in the cache, and a switch that
  // moved nothing must not pay for one.
  it("is null when the stored value moved and the effective engine did not", () => {
    const row = mac({ selected: "mlx-whisper" });
    expect(switchOutcome(row, AUTO, AUTO, reply())).toBeNull();
  });

  it("still reports an eviction the page had no way to predict", () => {
    // Residency is in no other field of the payload, so `unloaded` outranks
    // this side's reading of the switch rather than being checked against it.
    const row = mac({ selected: "mlx-whisper" });
    expect(switchOutcome(row, AUTO, AUTO, reply(["mlx-community/whisper"]))).toBe("unloaded");
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
