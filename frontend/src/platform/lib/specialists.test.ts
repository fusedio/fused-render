// The platform half of specialists: the flag's stale-answer rule, and a
// breakout request's specialist reaching its card.
import { afterEach, describe, expect, test } from "bun:test";

import {
  feedSpecialistsEnabled,
  publishSpecialistsEnabled,
  resetSpecialistsFlagForTests,
  specialistsEnabledNow,
  specialistsFlagStamp,
} from "./specialists-flag";
import { getBreakouts, openBreakout, resetBreakouts } from "./breakouts";

describe("specialists flag", () => {
  afterEach(() => resetSpecialistsFlagForTests());

  test("another reader's answer is dropped when a publish happened while it was in flight", () => {
    const stamp = specialistsFlagStamp();
    publishSpecialistsEnabled(true); // the toggle
    feedSpecialistsEnabled(false, stamp); // a prefs read that departed before it
    expect(specialistsEnabledNow()).toBe(true);
    feedSpecialistsEnabled(false, specialistsFlagStamp());
    expect(specialistsEnabledNow()).toBe(false);
  });
});

test("a breakout opened with a specialist carries it on its card", () => {
  const specialist = { id: "canvas", name: "Canvas", home: "/w/specialists/canvas" };
  openBreakout({ sessionId: "s1", file: "/w/proj", context: { block: "", count: 0 }, specialist });
  expect(getBreakouts()[0]!.specialist).toEqual(specialist);
  resetBreakouts();
});
