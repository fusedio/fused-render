// The Playground's fit badge — what it SAYS, pulled out of PlaygroundTab.tsx
// for the reason `modelSize.ts`'s own header gives: there is no DOM harness in
// this repo by design, so the part with a rule in it (which words does a
// verdict draw) lives in a module a test can drive directly.
//
// SPEC AI-16c: `fit` widened from a bare verdict string to
// `{verdict, basis, footprintBytes}`, and the copy now splits on BOTH — not
// just on `verdict` the way it used to. A "measured" verdict is a FACT: this
// model actually ran here, and `footprintBytes` is what it cost, so it is
// worded as one ("Ran here, tight (28 GB)"), figure included. "declared" (a
// curator's `resident_gb` estimate) and "download" (nothing better than the
// download's own byte count) keep the ORIGINAL hedge wording, with no figure
// — both are still guesses about what the model will cost resident, and
// printing a number beside a guess would lend it the weight of a measurement
// it has not earned.
//
// A MEASURED "no" is reachable and is not a contradiction (AI-16b/AI-16c): the
// footprint store only ever holds models that ran, and the budget it is
// judged against is what was LEFT after the reserve — so "Ran here, over
// budget" describes a run that happened while nothing else was competing for
// memory, worded as what it is rather than as a prediction of failure.
import { formatSize } from "@platform/lib/format";
import type { AiFitVerdict } from "@platform/lib/api";

const FIT_COPY: Record<
  AiFitVerdict["basis"],
  Record<AiFitVerdict["verdict"], (footprintBytes: number) => string>
> = {
  measured: {
    easy: (bytes) => `Ran comfortably here (${formatSize(bytes)})`,
    tight: (bytes) => `Ran here, tight (${formatSize(bytes)})`,
    no: (bytes) => `Ran here, over budget (${formatSize(bytes)})`,
  },
  declared: {
    easy: () => "Runs comfortably here",
    tight: () => "Tight fit on this machine",
    no: () => "Likely too big for this machine",
  },
  download: {
    easy: () => "Runs comfortably here",
    tight: () => "Tight fit on this machine",
    no: () => "Likely too big for this machine",
  },
};

export interface FitNote {
  text: string;
  /** A Tailwind background-color class for the badge's dot. The two BAD
   *  verdicts are tinted (D461 reserves the loud RUNNING green elsewhere);
   *  "easy" is not. */
  dot: string;
  /** Says which of the two questions the badge is answering — "this ran and
   *  here is what it cost" or "here is a guess" — the complaint AI-16c exists
   *  to answer: a reader who could not tell which of those two a bare badge
   *  meant. */
  title: string;
}

/** The badge's `{text, dot, title}` for `fit`, or `null` — and null is the
 *  only thing that draws nothing, matching `fit` itself being null when
 *  nothing at all is known about a model. */
export function fitNote(fit: AiFitVerdict | null | undefined): FitNote | null {
  if (!fit) return null;
  return {
    text: FIT_COPY[fit.basis][fit.verdict](fit.footprintBytes),
    dot:
      fit.verdict === "easy"
        ? "bg-emerald-500"
        : fit.verdict === "tight"
          ? "bg-[var(--warning)]"
          : "bg-[var(--error)]",
    title:
      fit.basis === "measured"
        ? "Measured on this machine"
        : "Judged against this machine's memory",
  };
}
