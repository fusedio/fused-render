// SourcePanel's confirm sheet: a "confirm" refusal from the server opens it,
// and the yes must re-post what the first POST carried — an action sent with
// args (Update proposal's `include`) must not come back bare after the yes.
import { expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { confirmArgs } = await import("./SourcePanel");

test("the yes re-posts the first POST's args", () => {
  expect(confirmArgs({ include: ["data.csv"] }, false, false)).toEqual({ include: ["data.csv"] });
});

test("Discard's withdraw box rides along with them when offered", () => {
  expect(confirmArgs(undefined, true, true)).toEqual({ withdraw: true });
  expect(confirmArgs({ include: ["a"] }, true, false)).toEqual({ include: ["a"], withdraw: false });
});

test("no args at all posts none", () => {
  expect(confirmArgs(undefined, false, true)).toBeUndefined();
  expect(confirmArgs({}, false, false)).toBeUndefined();
});
