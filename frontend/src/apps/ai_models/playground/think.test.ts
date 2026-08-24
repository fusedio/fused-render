import { expect, test } from "bun:test";

import { splitThink } from "./think";

test("a reply with no tags is all answer", () => {
  expect(splitThink("42, and here is why.")).toEqual({
    think: null,
    answer: "42, and here is why.",
    thinking: false,
  });
});

test("a closed pair splits into deliberation and answer", () => {
  expect(splitThink("<think>\ncount the cats\n</think>\nThree cats.")).toEqual({
    think: "count the cats",
    answer: "Three cats.",
    thinking: false,
  });
});

test("a block still open mid-stream is all deliberation", () => {
  expect(splitThink("<think>\ncount the ca")).toEqual({
    think: "\ncount the ca",
    answer: "",
    thinking: true,
  });
});

test("a closing tag with nothing opening it means the block was open from the start", () => {
  // The Macaw-OptiQ / LFM2.5 case: the opening tag was prefilled into the
  // prompt, or the model never writes one. Without this, the whole trace and a
  // stray </think> landed in the answer.
  expect(splitThink("count the cats\n</think>\nThree cats.")).toEqual({
    think: "count the cats",
    answer: "Three cats.",
    thinking: false,
  });
});

test("an unopened block still streaming as prose is not yet reclassified", () => {
  // Nothing on the wire says a model is thinking until it closes the block, so
  // these tokens read as the answer and move into the disclosure when </think>
  // arrives. The retro-move is the price of not knowing.
  expect(splitThink("count the cats")).toEqual({
    think: null,
    answer: "count the cats",
    thinking: false,
  });
});

test("<thinking> from a hand-written system prompt splits the same way", () => {
  expect(splitThink("<thinking>weigh it up</thinking>Yes.")).toEqual({
    think: "weigh it up",
    answer: "Yes.",
    thinking: false,
  });
});

test("an unopened <thinking> block closes the same way as <think>", () => {
  expect(splitThink("weigh it up</thinking>Yes.")).toEqual({
    think: "weigh it up",
    answer: "Yes.",
    thinking: false,
  });
});

test("an empty closed block discloses nothing", () => {
  // A hybrid model declining to think, or a prefilled tag closed at once.
  expect(splitThink("<think></think>Three cats.")).toEqual({
    think: null,
    answer: "Three cats.",
    thinking: false,
  });
  expect(splitThink("\n</think>\nThree cats.")).toEqual({
    think: null,
    answer: "Three cats.",
    thinking: false,
  });
});

test("a reply that stops on the closing tag has no answer yet", () => {
  expect(splitThink("<think>count the cats</think>")).toEqual({
    think: "count the cats",
    answer: "",
    thinking: false,
  });
});

test("a second closing tag inside the answer is left alone", () => {
  // The first close ends the block; what the answer says about tags after that
  // is the answer's business.
  expect(splitThink("<think>a</think>Write </think> to close it.")).toEqual({
    think: "a",
    answer: "Write </think> to close it.",
    thinking: false,
  });
});
