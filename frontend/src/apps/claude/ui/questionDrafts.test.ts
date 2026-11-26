import { afterEach, expect, test } from "bun:test";

import {
  clearQuestionDraft,
  readQuestionDraft,
  resetQuestionDraftsForTests,
  writeQuestionDraft,
  type QuestionDraft,
} from "./questionDrafts";

afterEach(() => resetQuestionDraftsForTests());

function draft(over: Partial<QuestionDraft> = {}): QuestionDraft {
  return {
    n: 2,
    picked: [["a"], []],
    otherOpen: [false, true],
    otherText: ["", "typed"],
    skipped: [false, false],
    active: 1,
    ...over,
  };
}

test("a draft reads back for the same request and question count only", () => {
  writeQuestionDraft("q1", draft());
  expect(readQuestionDraft("q1", 2)).toEqual(draft());
  // A row whose questions changed length is a different card.
  expect(readQuestionDraft("q1", 3)).toBeNull();
  expect(readQuestionDraft("other", 2)).toBeNull();
});

test("an untouched card stores nothing, and clear forgets it", () => {
  writeQuestionDraft("q1", draft());
  writeQuestionDraft("q1", draft({ picked: [[], []], otherOpen: [false, false], otherText: ["", ""], active: 0 }));
  expect(readQuestionDraft("q1", 2)).toBeNull();
  writeQuestionDraft("q2", draft());
  clearQuestionDraft("q2");
  expect(readQuestionDraft("q2", 2)).toBeNull();
});

test("only the newest drafts are kept", () => {
  for (let i = 0; i < 45; i++) writeQuestionDraft("q" + i, draft());
  expect(readQuestionDraft("q0", 2)).toBeNull();
  expect(readQuestionDraft("q4", 2)).toBeNull();
  expect(readQuestionDraft("q5", 2)).not.toBeNull();
  expect(readQuestionDraft("q44", 2)).not.toBeNull();
});
