import { describe, expect, it } from "bun:test";
import type { DriveStep } from "driver.js";
import {
  TOURS,
  autoStartTourFor,
  presentSteps,
  seenKey,
  tourById,
  type FlowStep,
} from "@platform/lib/tours/registry";

const step = (element: string): DriveStep => ({ element, popover: { title: element } });

describe("registry", () => {
  it("has a unique id and a title for every tour", () => {
    const ids = TOURS.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const t of TOURS) expect(t.title.length).toBeGreaterThan(0);
  });

  it("gives every tour steps that all target a selector string", () => {
    // Follow-up chains included: presentSteps filters those too, and a
    // non-string element would silently drop out of every run.
    for (const t of TOURS) {
      const steps = [...t.steps()];
      for (let f = t.followUp; f; f = f.followUp) steps.push(...f.steps());
      expect(steps.length).toBeGreaterThan(0);
      for (const s of steps) expect(typeof s.element).toBe("string");
    }
  });

  it("gives every tour a startPath its own matcher accepts", () => {
    // The replay's contract: navigating to startPath puts you on a route this
    // tour is about, so the poll that follows is waiting for chrome that is
    // actually coming. A startPath the tour would not match is a replay that
    // navigates and then polls to no purpose. A FUNCTION startPath (explorer:
    // the home folder's view URL, asked of the server) can't be checked here —
    // it fetches — so the invariant covers the string ones and only insists the
    // dynamic ones exist.
    for (const t of TOURS) {
      if (typeof t.startPath === "function") continue;
      expect(t.startPath.startsWith("/")).toBe(true);
      expect(t.matches(t.startPath)).toBe(true);
    }
  });

  it("sends the explorer replay to a folder view, not the launcher", () => {
    // "/explorer" is the recents/sessions/repos launcher with none of the
    // tour's chrome: matches() must refuse it (a replay standing there has to
    // navigate away first), and startPath must be the dynamic kind — no fixed
    // folder path exists to write down.
    const t = tourById("explorer")!;
    expect(t.matches("/explorer")).toBe(false);
    expect(t.matches("/explorer/view/Users/someone/Desktop")).toBe(true);
    expect(typeof t.startPath).toBe("function");
  });

  it("replays the AI tour from the playground tab, not the bare prefix", () => {
    // The matcher accepts "/ai-models" too, so the invariant test above cannot
    // catch a "simplification" back to the bare prefix — which App.tsx
    // redirects off, racing the replay's first steps.
    expect(tourById("ai")?.startPath).toBe("/ai-models/playground");
  });

  it("looks a tour up by id", () => {
    expect(tourById("home")?.title).toBe("Home");
    expect(tourById("nope")).toBeNull();
  });

  it("namespaces seen keys under v2, never the old single key", () => {
    expect(seenKey("home")).toBe("fused.tour.v2.home");
  });
});

describe("autoStartTourFor", () => {
  it("matches each surface on its own route", () => {
    expect(autoStartTourFor("/home")?.id).toBe("home");
    expect(autoStartTourFor("/tasks")?.id).toBe("tasks");
    expect(autoStartTourFor("/ai-models")?.id).toBe("ai");
    expect(autoStartTourFor("/ai-models/benchmark")?.id).toBe("ai");
  });

  it("matches nothing on a route no tour is about", () => {
    expect(autoStartTourFor("/")).toBeNull();
    expect(autoStartTourFor("/apps")).toBeNull();
    expect(autoStartTourFor("/preferences")).toBeNull();
    // A near-miss must not match: the AI tour is about the page's own tabs.
    expect(autoStartTourFor("/ai-models-something")).toBeNull();
  });

  it("never auto-fires the explorer tour, on its own route or any other", () => {
    expect(autoStartTourFor("/explorer")).toBeNull();
    expect(autoStartTourFor("/explorer/view/tmp")).toBeNull();
    // …while the tour itself still knows which route it is about, so the
    // replay menu's steps are found there.
    expect(tourById("explorer")?.matches("/explorer/view/tmp")).toBe(true);
  });
});

describe("presentSteps", () => {
  const steps = [step(".a"), step(".b"), step(".c")];

  it("keeps only the steps whose target is on screen, in order", () => {
    const kept = presentSteps(steps, (s) => s !== ".b");
    expect(kept.map((s) => s.element)).toEqual([".a", ".c"]);
  });

  it("returns nothing when no target is present", () => {
    expect(presentSteps(steps, () => false)).toEqual([]);
  });

  it("drops a step whose target is not a selector string", () => {
    const lazy: DriveStep = { element: () => null as unknown as Element };
    expect(presentSteps([lazy, step(".a")], () => true).map((s) => s.element)).toEqual([".a"]);
  });

  it("carries an interactive step's extras through the filter", () => {
    const flow: FlowStep[] = [{ ...step(".a"), advanceOn: ".send", onEnter: () => {} }];
    const kept = presentSteps(flow, () => true);
    expect(kept[0].advanceOn).toBe(".send");
    expect(typeof kept[0].onEnter).toBe("function");
  });
});

describe("interactive steps", () => {
  const aiSteps = tourById("ai")?.steps() ?? [];

  it("asks the AI tour's composer step for a send, by click and by Enter", () => {
    const waiting = aiSteps.filter((s) => s.advanceOn);
    expect(waiting.length).toBe(1);
    expect(waiting[0].element).toBe(".pg-composer");
    // Enter in the composer submits without touching the button, so the step
    // has to name the box as well as the button.
    expect(waiting[0].advanceOnEnter).toBe(".pg-composer textarea");
    expect(typeof waiting[0].onEnter).toBe("function");
  });

  it("puts the reply step straight after the send it waits for", () => {
    const i = aiSteps.findIndex((s) => s.advanceOn);
    expect(aiSteps[i + 1]?.element).toBe(".pg-answer-block");
  });

  it("leaves every other tour's steps plain, with nothing to wait for", () => {
    for (const t of TOURS.filter((t) => t.id !== "ai")) {
      for (const s of t.steps()) expect(s.advanceOn).toBeUndefined();
    }
  });
});

describe("home tour", () => {
  const steps = tourById("home")?.steps() ?? [];

  it("walks the search box, the strips as one region, a card, then bookmarks", () => {
    expect(steps.map((s) => s.element)).toEqual([
      ".home-hero",
      ".home-strips",
      // Real cards only — the loading skeletons wear .app-pcard too.
      "#home-sec-apps .app-pcard:not(.home-skel-card)",
      ".sidebar-bookmarks .sidebar-heading",
    ]);
  });
});

describe("tasks tour", () => {
  const tasks = tourById("tasks");

  it("shows the view bar and the create button, and nothing else", () => {
    expect(tasks?.steps().map((s) => s.element)).toEqual([
      ".schedule-view-seg",
      ".schedule-new",
    ]);
  });

  it("opens the modal on the create button, and names then creates the task", () => {
    const modal = tasks?.followUp;
    expect(modal?.trigger).toBe(".schedule-new");
    const steps = modal?.steps() ?? [];
    expect(steps.map((s) => s.element)).toEqual([".new-task-title", ".schedule-save"]);
    // One onEnter fills BOTH the title and the ask: the create below is real, so
    // the task has to carry an instruction.
    expect(typeof steps[0].onEnter).toBe("function");
    expect(steps[1].advanceOn).toBe(".schedule-save");
    expect(steps[1].actionText).toBe("Create it");
  });

  it("chains a second follow-up onto the create, one step per tasks view", () => {
    const chain = tasks?.followUp?.followUp;
    expect(chain?.trigger).toBe(".schedule-save");
    const steps = chain?.steps() ?? [];
    // List row, board card, calendar grid — mutually exclusive on screen, so
    // presentSteps keeps exactly the one the user is looking at.
    expect(steps.map((s) => s.element)).toEqual([
      ".tasks-row",
      ".schedule-tv-board .tasks-card-wrap",
      ".schedule-cal",
    ]);
    // The row's press is the stretched link, not the row div — so that is what
    // the step waits for and what its action button clicks.
    expect(steps[0].advanceOn).toBe(".tasks-rowlink");
    expect(steps[0].actionText).toBe("Open it");
    expect(steps[1].advanceOn).toBe(".schedule-tv-board .schedule-tv-card");
    // The calendar step is a plain pointer — a chip opens through a popover.
    expect(steps[2].advanceOn).toBeUndefined();
    // End of the chain.
    expect(chain?.followUp).toBeUndefined();
  });
});
