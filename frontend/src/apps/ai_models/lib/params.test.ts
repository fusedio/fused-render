import { expect, test } from "bun:test";

import { installDomShim } from "@platform/lib/testDomShim";

// params.ts imports router.ts for `replaceSearch`, and router.ts reads
// `location` at module scope; bun has no DOM. See testDomShim.ts for why this
// is the one shared stub every suite in the run installs, rather than a stub
// hand-rolled per file. Nothing below touches the exact pathname — every case
// hands the codec its own search string.
installDomShim();

// A dynamic import, not a static one: static imports hoist ABOVE the shim
// above and router.ts would read `location` before it exists (chat-params.test
// takes the same route for the same reason).
const { numParam, readParam, searchString } = await import("./params");

// Every case drives the `search` argument rather than a real `location` — the
// same reason `tabHref` takes one (routes.ts): these are codecs, and a codec
// that can only be tested inside a browser is not being tested.

test("a missing or empty param keeps the fallback", () => {
  expect(numParam("temp", 0.7, 0, 2, "")).toBe(0.7);
  expect(numParam("temp", 0.7, 0, 2, "?temp=")).toBe(0.7);
  // `Number("")` is 0 — a temperature nobody chose, and the reason the
  // emptiness check exists at all.
  expect(numParam("temp", 0.7, 0, 2, "?temp=%20%20")).toBe(0.7);
});

test("a non-numeric param keeps the fallback", () => {
  expect(numParam("temp", 0.7, 0, 2, "?temp=hot")).toBe(0.7);
  expect(numParam("maxtok", 1024, 1, 32768, "?maxtok=NaN")).toBe(1024);
  expect(numParam("maxtok", 1024, 1, 32768, "?maxtok=Infinity")).toBe(1024);
});

test("an in-range param is taken as written", () => {
  expect(numParam("temp", 0.7, 0, 2, "?temp=1.4")).toBe(1.4);
  expect(numParam("maxtok", 1024, 1, 32768, "?maxtok=1")).toBe(1);
  expect(numParam("maxtok", 1024, 1, 32768, "?maxtok=32768")).toBe(32768);
});

test("an out-of-range param CLAMPS, because the server would refuse it", () => {
  // The load-bearing case. `_sampling_problem` (server/ai.py) REJECTS rather
  // than clamps, so a hand-edited or stale link used to seed the rail with a
  // value that made every single message 400 — a link that opens a chat which
  // can never send. Clamping on the way in is what keeps that link usable.
  expect(numParam("temp", 0.7, 0, 2, "?temp=5")).toBe(2);
  expect(numParam("temp", 0.7, 0, 2, "?temp=-1")).toBe(0);
  expect(numParam("topp", 0.95, 0, 1, "?topp=9")).toBe(1);
  expect(numParam("maxtok", 1024, 1, 32768, "?maxtok=0")).toBe(1);
  expect(numParam("maxtok", 1024, 1, 32768, "?maxtok=999999")).toBe(32768);
});

test("bounds are optional and absent ones do not clamp", () => {
  expect(numParam("seed", 0, undefined, undefined, "?seed=99999999")).toBe(99999999);
  expect(numParam("guidance", 1, 0, undefined, "?guidance=100")).toBe(100);
  expect(numParam("guidance", 1, 0, undefined, "?guidance=-3")).toBe(0);
});

test("readParam takes the search it is handed", () => {
  expect(readParam("model", "?model=a%2Fb")).toBe("a/b");
  expect(readParam("cap", "?model=x")).toBe(null);
});

test("a reset's search names only what it was given", () => {
  // The shape a cross-capability pick writes: the model survives, every stage
  // setting that was in the URL does not — and the caller says so by naming
  // what to KEEP, so a stage that adds a parameter needs no edit here.
  expect(searchString({ model: "mlx-community/whisper-small-mlx" })).toBe(
    "?model=mlx-community%2Fwhisper-small-mlx",
  );
  // Nulls are absences, not empty values: "?model=" is a model nobody picked.
  expect(searchString({ model: "a/b", prompt: null, seed: null })).toBe("?model=a%2Fb");
  // Nothing at all is no question mark, not a bare one — a trailing "?" is a
  // URL that differs from the clean one only in a character.
  expect(searchString({ model: null })).toBe("");
});
