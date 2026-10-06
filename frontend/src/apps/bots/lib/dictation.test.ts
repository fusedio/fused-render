import { describe, expect, test } from "bun:test";
import { jobNote } from "./dictation";

describe("jobNote", () => {
  test("no job yet, or a job with nothing to say: the plain state", () => {
    expect(jobNote(null)).toBe("Transcribing…");
    expect(jobNote({})).toBe("Transcribing…");
    expect(jobNote({ detail: "   " })).toBe("Transcribing…");
  });
  test("the job's own detail wins: it is the sentence the server wrote for this phase", () => {
    expect(jobNote({ detail: "Preparing MLX Whisper — downloading torch (1m40s)…" }))
      .toBe("Preparing MLX Whisper — downloading torch (1m40s)…");
  });
  test("waiting on a model with no detail yet: say it is the first-run install, not a hang", () => {
    expect(jobNote({ waiting_for: "sys:ai-model:mlx-community--whisper-tiny.en-8bit" }))
      .toBe("Preparing the speech model (first time only)…");
    expect(jobNote({ waiting_for: "sys:capture:abc" })).toBe("Transcribing…");
  });
});
