import { describe, expect, test } from "bun:test";
import type { Preset } from "./api";
import { BLANKS, blankQ, filterCards, firstPick, matchQ, newBotInit, pickCards, presetNote, presetQ, queryWords } from "./presets";

const P = (key: string, name: string, skills: string[]): Preset =>
  ({ key, name, color: "#0a66c2", order: 0, model: "haiku", instructions: `Browse ${name}.`, apps: [], skills });
const PRESETS = [
  P("linkedin", "LinkedIn", ["Find recruiters", "Summarize my feed"]),
  P("youtube", "YouTube", ["Watch later digest"]),
  P("gdocs", "Google Docs", ["Draft a doc"]),
];
const cards = pickCards(PRESETS);
const keys = (cs: { key: string; q: string }[]) => cs.map((c) => c.key || c.q);

describe("search text", () => {
  test("a preset matches on name, key and playbook titles", () => {
    expect(presetQ(PRESETS[0])).toBe("linkedin linkedin find recruiters summarize my feed");
    expect(presetQ(PRESETS[2])).toBe("google docs gdocs draft a doc");
  });
  test("a blank matches on its name only (plus 'blank')", () => {
    expect(blankQ(BLANKS[0])).toBe("orange bot blank");
  });
  test("query words: lower-cased, split on whitespace, blanks dropped", () => {
    expect(queryWords("  Feed   LINKED ")).toEqual(["feed", "linked"]);
    expect(queryWords("")).toEqual([]);
  });
  test("every word must hit", () => {
    expect(matchQ("linkedin find recruiters", ["link", "recruit"])).toBe(true);
    expect(matchQ("linkedin find recruiters", ["link", "youtube"])).toBe(false);
    expect(matchQ("anything", [])).toBe(true);
  });
});

describe("filterCards", () => {
  test("the four blanks come first, then the presets as returned", () => {
    expect(cards.length).toBe(BLANKS.length + PRESETS.length);
    expect(cards.slice(0, 4).every((c) => c.key === "" && c.pick.kind === "blank")).toBe(true);
    expect(cards.slice(4).map((c) => c.key)).toEqual(["linkedin", "youtube", "gdocs"]);
  });
  test("an empty query shows everything and no 'no match' line", () => {
    const r = filterCards(cards, "  ");
    expect(r.shown.length).toBe(cards.length);
    expect(r.none).toBe(false);
  });
  test("a playbook word finds its preset", () => {
    expect(keys(filterCards(cards, "digest").shown)).toEqual(["youtube"]);
    expect(keys(filterCards(cards, "DOC").shown)).toEqual(["gdocs"]);
  });
  test("'blank' and a colour find the blank starters", () => {
    expect(filterCards(cards, "blank").shown.length).toBe(4);
    expect(keys(filterCards(cards, "pink").shown)).toEqual(["pink bot blank"]);
  });
  test("'bot' matches the blanks only (presets carry no 'bot' in their text)", () => {
    expect(filterCards(cards, "bot").shown.every((c) => c.pick.kind === "blank")).toBe(true);
  });
  test("nothing matches → none", () => {
    const r = filterCards(cards, "zzz");
    expect(r.shown).toEqual([]);
    expect(r.none).toBe(true);
  });
});

describe("firstPick (Enter)", () => {
  test("the first preset still showing, not a blank", () => {
    expect(firstPick(filterCards(cards, "").shown)?.key).toBe("linkedin");
  });
  test("a blank only when nothing else is left", () => {
    expect(firstPick(filterCards(cards, "red").shown)?.q).toBe("red bot blank");
  });
  test("nothing showing → undefined", () => {
    expect(firstPick([])).toBeUndefined();
  });
});

describe("newBotInit", () => {
  test("a preset fills title, name, model, instructions, brand face and the playbook note", () => {
    const v = newBotInit({ kind: "preset", preset: PRESETS[0] });
    expect(v).toEqual({
      title: "New LinkedIn bot", name: "LinkedIn bot", model: "haiku", instructions: "Browse LinkedIn.",
      face: { icon: "linkedin", color: "#0a66c2" }, preset: "linkedin",
      presetNote: "Comes with 2 playbooks: Find recruiters, Summarize my feed. Edit them under Skills once the bot exists.",
    });
  });
  test("a blank fills its name and face, sonnet, no note, no preset", () => {
    expect(newBotInit({ kind: "blank", blank: BLANKS[2] })).toEqual({
      title: "New Blue Bot", name: "Blue Bot", model: "sonnet", instructions: "", face: { shape: "square", color: "#2f7ae5" }, presetNote: "", preset: "",
    });
  });
  test("presetNote counts the playbooks", () => {
    expect(presetNote({ skills: ["a"] })).toBe("Comes with 1 playbooks: a. Edit them under Skills once the bot exists.");
  });
});

describe("Super Bot card", () => {
  test("not offered by default (a Super Bot exists), first when asked for", () => {
    expect(pickCards(PRESETS).some((c) => c.pick.kind === "super")).toBe(false);
    const withSuper = pickCards(PRESETS, true);
    expect(withSuper.length).toBe(cards.length + 1);
    expect(withSuper[0].pick.kind).toBe("super");
    expect(withSuper[0].key).toBe("");
  });
  test("found by 'mac', 'super' or 'shell'; Enter still prefers a preset", () => {
    const withSuper = pickCards(PRESETS, true);
    expect(filterCards(withSuper, "mac").shown.map((c) => c.pick.kind)).toEqual(["super"]);
    expect(filterCards(withSuper, "shell").shown.map((c) => c.pick.kind)).toEqual(["super"]);
    expect(firstPick(filterCards(withSuper, "").shown)?.key).toBe("linkedin");
    expect(firstPick(filterCards(withSuper, "mac").shown)?.pick.kind).toBe("super");
  });
  test("newBotInit: named Super Bot, a Claude model, its face, blank instructions and the note", () => {
    const v = newBotInit({ kind: "super" });
    expect(v.name).toBe("Super Bot");
    expect(v.title).toBe("New Super Bot");
    expect(v.model).toBe("sonnet");
    expect(v.instructions).toBe("");
    expect(v.preset).toBe("");
    expect(v.face).toEqual({ icon: "claude", color: "#262624" });
    expect(v.presetNote).toContain("One per Mac");
  });
});
