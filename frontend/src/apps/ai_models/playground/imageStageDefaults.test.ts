// ---- which default the image stage starts a control at -------------------------
// Three claims about `ImageStage` that live in React state initialisers and in one
// effect's dirty-check, so no pure function holds them and no screenshot can: the
// stage must start `guidance` at the model's own curated number where the catalog
// names one, and fall back to `DEFAULTS.guidance` only where it does not.
//
// The stakes are the whole reason the field exists. `DEFAULTS.guidance` is 1,
// which is right for the guidance-distilled FLUX.2 klein rows and wrong for an
// ordinary SD1.5 UNet like `segmind/tiny-sd`, where the same argument reaches the
// pipeline as real classifier-free guidance: rendering that model at 1 instead of
// its curated 7.5 measures a mean absolute pixel difference of 22.8 on a 0-255
// scale — a visibly less prompt-adherent image, not a rounding difference.
//
// Read out of the source, which is this repo's habit for exactly this kind of
// claim (see local/repoCardControls.test.ts, and shell/tasks-lib.test.ts's "where
// the marks are drawn"). `ImageStage` has no render harness of its own — standing
// one up means mocking `client.ts`, `jobs.ts`, `webcam.ts`, `pickFile`, `rawUrl`
// and `autoGrow` — so the derivation is pinned here rather than left unheld.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const HERE = import.meta.dir;
const STAGE = readFileSync(join(HERE, "ImageStage.tsx"), "utf8");

describe("the image stage's starting guidance", () => {
  it("derives from the curated entry, falling back to the stage's own", () => {
    expect(STAGE).toContain(
      "const modelGuidance = entry.defaults?.guidance ?? DEFAULTS.guidance;",
    );
  });

  it("starts the control at that derivation, not at the bare fallback", () => {
    // `numParam` reads the URL first; the second argument is what a stage with no
    // `guidance` in its link starts at, which is the case this is about.
    expect(STAGE).toContain('numParam("guidance", modelGuidance, ...GUIDANCE_RANGE)');
  });

  it("measures 'the user changed it' against that derivation too", () => {
    // The effect writes `guidance` into the link only when it differs from the
    // starting point. Comparing against `DEFAULTS.guidance` on a model whose
    // start is 7.5 would stamp `guidance=7.5` into every link as though somebody
    // had dragged the slider there.
    expect(STAGE).toContain("guidance: guidance !== modelGuidance ? String(guidance) : null");
  });

  it("offers that derivation as the slider's reset target", () => {
    expect(STAGE).toContain("fallback={modelGuidance}");
  });

  it("leaves DEFAULTS.guidance as the fallback alone, used nowhere else", () => {
    // One mention defines it, one consumes it in the derivation above. A third
    // would be a control that had drifted back to the distilled assumption.
    const uses = STAGE.match(/DEFAULTS\.guidance/g) ?? [];
    expect(uses.length).toBe(2);
  });
});
