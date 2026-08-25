import { describe, expect, it } from "bun:test";
import type { CapabilityEngine, Prefs } from "@platform/lib/api";
import {
  capabilityLabel,
  choiceReason,
  engineNote,
  ignoredWarning,
  parseAiIdleMinutes,
  servingLine,
  strandedSelection,
  switchOutcome,
  unloadCountdown,
  wouldChangeEngine,
} from "@apps/ai_models/lib/engines";

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
    expect(servingLine(windows(), AUTO)).toContain("Faster Whisper");
    expect(servingLine(windows(), AUTO)).not.toContain("CTranslate2");
    expect(servingLine(windows(), AUTO)).not.toContain("MLX");
  });

  it("says so plainly when nothing serves the capability here", () => {
    const row = mac({ effective: null, effectiveLabel: null, effectiveShortLabel: null });
    expect(servingLine(row, AUTO)).toContain("Not available");
  });

  it("is null when the concrete selection is honoured and matches what serves — the control already names it", () => {
    // A stored concrete engine, in force, naming exactly what is running: the
    // trigger's own label already says "Faster Whisper", and a line under it
    // reading "Using Faster Whisper." repeats it word for word.
    const row = mac({ selected: "faster-whisper", effective: "faster-whisper" });
    expect(servingLine(row, AUTO)).toBeNull();
  });

  it("still reports when the selection is auto, even though it matches effective", () => {
    // "Automatic" the trigger shows does not name the runner — the line under
    // it is the only place that does.
    expect(servingLine(mac(), AUTO)).toBe("Using MLX Whisper.");
  });

  it("still reports when the concrete selection is IGNORED, even though the picker still shows it", () => {
    // The control shows the (ignored) selected choice, not what is actually
    // running — so the serving line is the only place naming reality, exactly
    // as before.
    expect(servingLine(windows(), AUTO)).toBe("Using Faster Whisper.");
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

  it("does not say the name twice when the registry's own reason already names it", () => {
    // The stranded-selection case: `strandedSelection` renders the raw code
    // ("onnx-embed") as the option, and `ignoredReason` already reads
    // "onnx-embed is not a runner this build knows" — prefixing "onnx-embed is
    // not used here — " in front of that repeats the code. When the resolved
    // display name already occurs inside the reason, the sentence folds into
    // one "Ignored — …" instead.
    const row = mac({
      capability: "embeddings",
      selected: "onnx-embed",
      effective: "sentence-transformers",
      effectiveLabel: "Sentence Transformers",
      effectiveShortLabel: "Sentence Transformers",
      ignoredReason: "onnx-embed is not a runner this build knows",
      choices: [
        {
          code: "sentence-transformers",
          label: "Sentence Transformers",
          note: null,
          available: true,
          reason: null,
        },
      ],
    });
    expect(ignoredWarning(row)).toBe("Ignored — onnx-embed is not a runner this build knows.");
  });
});

describe("strandedSelection", () => {
  // A prefs.json naming an engine this build removed — the `transformers-text`
  // case D416 created. The server keeps `selected` as stored and reports the
  // drop, so the row is internally consistent; what the PAGE has to notice is
  // that no option matches, because a <select> in that state renders blank.
  function withdrawn(): CapabilityEngine {
    return mac({
      capability: "text-generation",
      selected: "transformers-text",
      effective: "llamacpp-text",
      effectiveLabel: "llama.cpp (CPU)",
      effectiveShortLabel: "llama.cpp (CPU)",
      ignoredReason: "transformers-text is not a runner this build knows",
      choices: [
        {
          code: "llamacpp-text",
          label: "llama.cpp (CPU)",
          note: null,
          available: true,
          reason: null,
        },
      ],
    });
  }

  // The OTHER value that strands, and the one that caught the copy overclaiming:
  // a code that IS registered and available, serving a different capability.
  // `describe_engines` filters `choices` by capability, so it goes missing from
  // the list exactly like a withdrawn code does — and `prefs._valid_engine_choice`
  // refuses this on write but says nothing about a prefs.json already on disk,
  // which is the same reachability argument that justifies handling the
  // withdrawn case at all. Verified against the real registry: a stored
  // `{"text-generation": "mlx-whisper"}` yields exactly this row.
  function wrongCapability(): CapabilityEngine {
    return mac({
      capability: "text-generation",
      selected: "mlx-whisper",
      effective: "llamacpp-text",
      effectiveLabel: "llama.cpp (CPU)",
      effectiveShortLabel: "llama.cpp (CPU)",
      ignoredReason: "MLX Whisper does not do text-generation",
      choices: [
        {
          code: "llamacpp-text",
          label: "llama.cpp (CPU)",
          note: null,
          available: true,
          reason: null,
        },
      ],
    });
  }

  it("reports a stored engine that no option matches", () => {
    expect(strandedSelection(withdrawn(), AUTO)).toBe("transformers-text");
  });

  it("also reports a registered engine that serves another capability", () => {
    // The predicate must fire here too — otherwise the picker goes blank for
    // this row, which is the whole bug this function exists to prevent.
    expect(strandedSelection(wrongCapability(), AUTO)).toBe("mlx-whisper");
  });

  it("does not let the caller claim a WITHDRAWAL it cannot establish", () => {
    // Both rows return a bare code and nothing else, so the option's copy is
    // identical for both — which is why it may not say "no longer available in
    // this version": `mlx-whisper` is registered, and on a Mac it is available
    // too. What separates them is `ignoredReason`, and that is the registry's
    // own sentence rather than anything this module composes.
    expect(typeof strandedSelection(withdrawn(), AUTO)).toBe("string");
    expect(typeof strandedSelection(wrongCapability(), AUTO)).toBe("string");
    expect(ignoredWarning(withdrawn())).toContain("not a runner this build knows");
    expect(ignoredWarning(wrongCapability())).toContain("does not do text-generation");
  });

  it("is silent for auto and for a choice that IS in the list", () => {
    // The two cases that must not render an extra option: `auto` has one
    // already, and an ordinary selection is one of the real choices. Neither is
    // stranded, however the row's `ignoredReason` reads — an override that is
    // merely unavailable HERE is still a listed option, which is the case
    // `windows()` covers and the one this must not be confused with.
    expect(strandedSelection(mac(), AUTO)).toBeNull();
    expect(strandedSelection(mac({ selected: "faster-whisper" }), AUTO)).toBeNull();
    expect(strandedSelection(windows(), AUTO)).toBeNull();
  });

  it("leaves the warning underneath saying what happened", () => {
    // The pair is the whole answer: the option says WHAT is stored, the warning
    // says why it is not in force. Either alone is a page that misleads — a
    // blank control with a reason under it, or a greyed option with no reason.
    const warning = ignoredWarning(withdrawn()) ?? "";
    expect(warning).toContain("transformers-text");
    expect(warning).toContain("not a runner this build knows");
    // The code and the reason both name "transformers-text", so the sentence
    // folds into one "Ignored — …" rather than repeating the code in front of
    // a reason that already contains it (`ignoredWarning`'s de-duplication).
    expect(warning).toBe("Ignored — transformers-text is not a runner this build knows.");
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

  it("names the fifth capability too", () => {
    expect(capabilityLabel("text-to-video")).toBe("Video generation");
  });

  it("renders an unknown capability as ITSELF rather than hiding it", () => {
    // A capability added server-side should appear here — ugly but present —
    // instead of vanishing from the only page that can configure it.
    expect(capabilityLabel("video-generation")).toBe("video-generation");
  });
});

describe("parseAiIdleMinutes", () => {
  it("accepts an in-range integer", () => {
    expect(parseAiIdleMinutes("45")).toBe(45);
    expect(parseAiIdleMinutes("0")).toBe(0);
    expect(parseAiIdleMinutes("1440")).toBe(1440);
  });

  it("rejects empty and whitespace-only input rather than reading it as zero", () => {
    // The bug: `Number("")` is `0` and `Number.isInteger(0)` is `true`, so
    // without this guard, clearing the field and blurring — the ordinary
    // intermediate state of editing a number input — silently PUT
    // `ai_idle_unload_minutes: 0` and turned the whole feature off.
    expect(parseAiIdleMinutes("")).toBeNull();
    expect(parseAiIdleMinutes("   ")).toBeNull();
  });

  it("rejects out-of-range and non-integer input", () => {
    expect(parseAiIdleMinutes("-1")).toBeNull();
    expect(parseAiIdleMinutes("1441")).toBeNull();
    expect(parseAiIdleMinutes("4.5")).toBeNull();
    expect(parseAiIdleMinutes("abc")).toBeNull();
  });
});

describe("unloadCountdown", () => {
  it("names the minutes when there is more than one", () => {
    expect(unloadCountdown(240)).toBe("unloads in 4 min");
  });

  it("rounds to the nearest minute rather than truncating", () => {
    // 269s is 4:29 — closer to 4 than to 5, and truncating would say 4 anyway,
    // so this pins ROUNDING specifically against the boundary just past it.
    expect(unloadCountdown(271)).toBe("unloads in 5 min");
  });

  it("says 'under a minute' rather than '0 min'", () => {
    // "0 min" reads as "already unloaded" or as a typo, neither of which is
    // true with seconds still on the clock.
    expect(unloadCountdown(45)).toBe("unloads in under a minute");
  });

  it("says nothing when the window is disabled or forced off", () => {
    // `null` is what `describe()` sends for both cases (AI-13) — a page has
    // no reason to distinguish "the user set 0" from "an env var did" here,
    // since neither one is counting down.
    expect(unloadCountdown(null)).toBeNull();
  });
});

