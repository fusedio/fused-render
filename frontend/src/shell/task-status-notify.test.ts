// The decision table behind §5's "interactive turns and needs-input" moments
// — pure, no DOM (task-status-notify.ts's own header explains the split).
import { describe, expect, test } from "bun:test";
import { notificationForTransition, taskDestination } from "./task-status-notify";
import type { TaskPulseTask } from "@platform/lib/api";

function task(over: Partial<TaskPulseTask> = {}): TaskPulseTask {
  return {
    key: "t1",
    status: "in_progress",
    unread: 0,
    last_active: 0,
    happened_at: 0,
    project: "/proj",
    task_id: "T097",
    title: "update the changelog",
    target: "/proj",
    session_id: "",
    ...over,
  } as TaskPulseTask;
}

describe("notificationForTransition", () => {
  test("a task's first sighting is never a transition when its happened_at predates the watch start (backfill)", () => {
    // watchStartS is AFTER happened_at (default 0) — this is the ordinary
    // "200 already-done rows on a fresh tab" case: nothing here is news.
    expect(
      notificationForTransition(undefined, task({ status: "needs_attention" }), 1000),
    ).toBeNull();
    expect(notificationForTransition(undefined, task({ status: "blocked" }), 1000)).toBeNull();
    expect(notificationForTransition(undefined, task({ status: "done" }), 1000)).toBeNull();
    expect(notificationForTransition(undefined, task({ status: "in_progress" }), 1000)).toBeNull();
  });

  // 2026-09-17 fix: "a finished Claude task usually raises no notification at
  // all" — a run whose entire lifetime (start AND finish) fits inside one
  // poll gap is a first sighting of its own key, already in a terminal
  // column, per tasksPulse.ts's one-row-per-session_id shape. Its own
  // `happened_at` landing AFTER this document's watch start is what makes it
  // news rather than backfill.
  test("a task first sighted already done, with happened_at after the watch start, still notifies", () => {
    const t = task({ status: "done", happened_at: 500, target: "/somewhere" });
    const n = notificationForTransition(undefined, t, 100);
    expect(n?.tone).toBe("info");
    expect(n?.detail).toBe("Finished");
    expect(n?.page).toBe(taskDestination(t));
  });

  test("a task first sighted already blocked, with happened_at after the watch start, still notifies", () => {
    const t = task({ status: "blocked", happened_at: 500, target: "/proj" });
    const n = notificationForTransition(undefined, t, 100);
    expect(n?.tone).toBe("error");
    expect(n?.title).toContain("failed");
    expect(n?.page).toBe(taskDestination(t));
  });

  // needs_attention is deliberately NOT part of the backfill/news split —
  // only done/blocked are, per the spec's table. attentionRows already owns
  // a permanent row for a parked task; first sighting stays silent here
  // regardless of timing.
  test("a task first sighted already needing attention never notifies here, even with a fresh happened_at", () => {
    expect(
      notificationForTransition(
        undefined,
        task({ status: "needs_attention", happened_at: 500 }),
        100,
      ),
    ).toBeNull();
  });

  test("no status change is not a transition", () => {
    expect(notificationForTransition("in_progress", task({ status: "in_progress" }), 0)).toBeNull();
  });

  // REVERSED 2026-09-16 (user: "the user does want to open the app along
  // with claude template to go back") — a finished task is now retained AND
  // clickable, not a plain "it's over" popup that vanishes. `isRetained`
  // (notifications.ts) keys retention on `Boolean(action || page)`, so `page`
  // being set is what actually keeps it — this is asserted directly rather
  // than assumed.
  //
  // SECOND REVERSAL, 2026-09-17: this branch used to also carry `source`,
  // which let a finished task's POPUP be presence-suppressed. A finished
  // Claude task is never presence-suppressed any more — dropping `source`
  // means `isPopupSuppressed`/`jobRows` (jobs.ts) has nothing to key off, so
  // the popup always pops. The `recent: true` flag from the now-removed
  // "Recent" section is gone too — the row just lands as an ordinary row.
  test("in_progress -> done is retained, clickable, and never presence-suppressed", () => {
    const t = task({ status: "done", target: "/somewhere" });
    const n = notificationForTransition("in_progress", t, 0);
    expect(n?.tone).toBe("info");
    expect(n?.source).toBeUndefined();
    expect(n?.page).toBe(taskDestination(t));
  });

  // Regression: dropping `source` (above) to stop presence-suppressing a
  // finished task silently deleted the card's ONLY caption source too, since
  // `notifications.ts`'s old `toStored` computed the caption from `source`
  // alone. The card the user saw was a single bold line with no creator
  // context at all ("why do you always want to make the notification
  // smaller? ... I don't want a single line of text"). `origin` restores the
  // caption without reintroducing suppression — see notifications.ts's own
  // `origin` field.
  test("in_progress -> done carries a project-first caption via `origin`, not `source`", () => {
    const t = task({ status: "done", project: "/sandbox/Transcripto", target: "/other/index.html" });
    const n = notificationForTransition("in_progress", t, 0);
    expect(n?.origin).toBe("Transcripto");
    expect(n?.source).toBeUndefined();
  });

  test("in_progress -> done falls back to `target` for its caption when the task has no project", () => {
    const t = task({ status: "done", project: "", target: "/sandbox/Transcripto/index.html" });
    const n = notificationForTransition("in_progress", t, 0);
    // project-first, but target-first WOULD have produced "index" here —
    // the exact bug `attentionRows`'s own comment names — proving this
    // reads the entry-page basename fallback in format.ts, not a coincidence.
    expect(n?.origin).toBe("Transcripto");
  });

  test("in_progress -> done strips a leading repeat of its own caption from the title", () => {
    const t = task({
      status: "done",
      project: "/sandbox/Transcripto",
      title: "Transcripto YouTube transcriber",
    });
    const n = notificationForTransition("in_progress", t, 0);
    expect(n?.origin).toBe("Transcripto");
    expect(n?.title).toBe("YouTube transcriber");
    // "finished" moved to `detail`, not the title.
    expect(n?.detail).toBe("Finished");
    expect(n?.title).not.toContain("finished");
  });

  test("in_progress -> done leaves a title untouched when it doesn't start with its own caption", () => {
    const t = task({ status: "done", project: "/sandbox/Transcripto", title: "render the tiles" });
    const n = notificationForTransition("in_progress", t, 0);
    expect(n?.title).toBe("render the tiles");
    expect(n?.detail).toBe("Finished");
  });

  test("in_progress -> done with no title falls back to a generic noun, caption stripping is a no-op", () => {
    const t = task({ status: "done", project: "/sandbox/Transcripto", title: "" });
    const n = notificationForTransition("in_progress", t, 0);
    expect(n?.title).toBe("A task");
    expect(n?.origin).toBe("Transcripto");
  });

  // 2026-09-18 fix (user: "these 2 fused-render notifications should have
  // been grouped together as count"): two finished tasks in the same folder
  // usually have DIFFERENT titles ("hi", "New session"), so `notifications.
  // ts`'s ordinary caption+title family never collapsed them. This call site
  // is the one that opts into the coarser, per-folder `familyKey`.
  test("in_progress -> done sets a per-folder familyKey so two different finished tasks in the same folder collapse", () => {
    const t1 = task({ status: "done", project: "/sandbox/fused-render", title: "hi" });
    const t2 = task({ status: "done", project: "/sandbox/fused-render", title: "New session" });
    const n1 = notificationForTransition("in_progress", t1, 0);
    const n2 = notificationForTransition("in_progress", t2, 0);
    expect(n1?.familyKey).toBeTruthy();
    expect(n1?.familyKey).toBe(n2?.familyKey);
  });

  test("in_progress -> done with no caption at all sets no familyKey, so it falls back to the ordinary page/title family", () => {
    const t = task({ status: "done", project: "", target: "" });
    const n = notificationForTransition("in_progress", t, 0);
    expect(n?.familyKey).toBeUndefined();
  });

  // ALREADY-OPEN GATE (F8, 2026-09-18): "we never want to show notifications
  // for tasks when the claude template / app is already opened" — the row
  // itself is never dropped (unlike the F7 gate above), only its popup: a
  // finished task whose destination the injected predicate reports as open
  // sets `quiet: true` instead of returning `null`. The predicate receives
  // an already fs-path-normalized string (`recentFsPath(taskDestination(t))`,
  // never the raw `/explorer/view/...?_side=claude...` href) — asserted
  // directly below rather than assumed.
  describe("in_progress -> done is quiet (popup-suppressed, still retained) when its destination is already open", () => {
    test("a finished task whose destination is open sets quiet: true", () => {
      const t = task({ status: "done", session_id: "s1", target: "/proj/index.html" });
      const n = notificationForTransition("in_progress", t, 0, false, () => true);
      expect(n?.quiet).toBe(true);
      // Still a normal, retained, clickable row — only the popup is affected.
      expect(n?.page).toBe(taskDestination(t));
      expect(n?.tone).toBe("info");
    });

    test("the predicate receives the destination normalized to a bare fs path, not the raw href", () => {
      const t = task({ status: "done", session_id: "s1", target: "/proj/index.html" });
      let seen: string | undefined;
      notificationForTransition("in_progress", t, 0, false, (page) => {
        seen = page;
        return false;
      });
      expect(seen).toBe("/proj/index.html");
      // Sanity: the RAW destination this normalizes from is a query-bearing
      // /explorer/view/ href, not the bare fs path itself — proving the
      // normalization step is doing real work, not a no-op.
      expect(taskDestination(t)).toContain("?_side=claude");
      expect(taskDestination(t)).not.toBe(seen);
    });

    test("the same task with nothing open pops normally (quiet is falsy)", () => {
      const t = task({ status: "done", session_id: "s1", target: "/proj/index.html" });
      const n = notificationForTransition("in_progress", t, 0, false, () => false);
      expect(n?.quiet).toBeFalsy();
    });

    test("a blocked task still raises with its destination open — quiet only applies to the finished branch", () => {
      const t = task({ status: "blocked", session_id: "s1", target: "/proj/index.html" });
      const n = notificationForTransition("in_progress", t, 0, false, () => true);
      expect(n?.tone).toBe("error");
      expect((n as { quiet?: boolean })?.quiet).toBeUndefined();
    });

    test("a needs_attention task still raises with its destination open", () => {
      const t = task({ status: "needs_attention", session_id: "s1", target: "/proj/index.html" });
      const n = notificationForTransition("in_progress", t, 0, false, () => true);
      expect(n?.title).toContain("needs your input");
      expect((n as { quiet?: boolean })?.quiet).toBeUndefined();
    });

    test("a task whose destination is the /tasks fallback still pops even with /tasks open", () => {
      const t = task({ status: "done", session_id: "", target: "", project: "" });
      expect(taskDestination(t)).toBe("/tasks");
      const n = notificationForTransition("in_progress", t, 0, false, () => true);
      expect(n?.quiet).toBeFalsy();
    });

    // F9 fix (Finding 4): a task WITH a session id but empty target AND
    // empty project normalizes to the explorer root ("/") rather than
    // "/tasks" — the SAME degenerate-destination problem under a different
    // spelling, missed by the original `destination !== "/tasks"` carve-out.
    test("a task whose destination normalizes to the explorer root still pops even with '/' open", () => {
      const t = task({ status: "done", session_id: "s1", target: "", project: "" });
      expect(taskDestination(t)).toBe("/explorer/view/?_side=claude&session_id=s1");
      const n = notificationForTransition("in_progress", t, 0, false, () => true);
      expect(n?.quiet).toBeFalsy();
    });

    test("defaults to nothing-open (quiet falsy) when no predicate is passed", () => {
      const t = task({ status: "done", session_id: "s1", target: "/proj/index.html" });
      const n = notificationForTransition("in_progress", t, 0);
      expect(n?.quiet).toBeFalsy();
    });

    test("composes with the F7 terminal-session gate: a cli task with its destination open still stays fully silent", () => {
      const t = task({ status: "done", entrypoint: "cli", session_id: "s1", target: "/proj/index.html" });
      expect(notificationForTransition("in_progress", t, 0, false, () => true)).toBeNull();
    });
  });

  // TERMINAL-SESSION SCOPING (2026-09-18): the reported bug — a plain
  // interactive-terminal `claude` session raising a fused-render notice —
  // scoped by `task.entrypoint`, threaded through as a plain argument since
  // this function stays pure. See task-status-notify.ts's own comment on the
  // gate for why "cli" is the only value that suppresses, and why unknown
  // fails open.
  describe("in_progress -> done is scoped to non-terminal sessions by default", () => {
    test("a cli-entrypoint task notifies nothing with the preference off (the default)", () => {
      const t = task({ status: "done", entrypoint: "cli" });
      expect(notificationForTransition("in_progress", t, 0)).toBeNull();
      expect(notificationForTransition("in_progress", t, 0, false)).toBeNull();
    });

    test("a cli-entrypoint task notifies once the preference is on", () => {
      const t = task({ status: "done", entrypoint: "cli" });
      const n = notificationForTransition("in_progress", t, 0, true);
      expect(n?.tone).toBe("info");
      expect(n?.detail).toBe("Finished");
    });

    test("an sdk-cli-entrypoint task notifies regardless of the preference", () => {
      const t = task({ status: "done", entrypoint: "sdk-cli" });
      expect(notificationForTransition("in_progress", t, 0, false)?.tone).toBe("info");
      expect(notificationForTransition("in_progress", t, 0, true)?.tone).toBe("info");
    });

    test("a task with no entrypoint at all notifies regardless of the preference (fails open)", () => {
      const t = task({ status: "done" });
      expect(notificationForTransition("in_progress", t, 0, false)?.tone).toBe("info");
      expect(notificationForTransition("in_progress", t, 0, true)?.tone).toBe("info");
    });

    test("needs_attention and in_progress -> blocked are unaffected by entrypoint", () => {
      const blocked = task({ status: "blocked", entrypoint: "cli" });
      expect(notificationForTransition("in_progress", blocked, 0, false)?.tone).toBe("error");
      const attention = task({ status: "needs_attention", entrypoint: "cli" });
      expect(notificationForTransition("in_progress", attention, 0, false)?.title)
        .toContain("needs your input");
    });
  });

  test("in_progress -> blocked is a never-suppressed, retained, actioned failure", () => {
    const n = notificationForTransition(
      "in_progress",
      task({ status: "blocked", session_id: "s1", target: "/proj" }),
      0,
    );
    expect(n?.tone).toBe("error");
    expect(n?.source).toBeUndefined();
    expect(n?.page).toBe(taskDestination(task({ status: "blocked", session_id: "s1", target: "/proj" })));
    expect(n?.title).toContain("failed");
  });

  test("any transition into needs_attention is a plain, non-retained alert", () => {
    // Deliberately no tone/tier: attentionRows (tasks-lib.ts) already retains
    // this fact persistently — this notify() is only the moment-of alert, so
    // it must resolve to "transient" (pops, does not retain) rather than
    // notify()'s ordinary tone:"error" shape, which would always-retain a
    // second, duplicate row for the same task.
    const n = notificationForTransition("in_progress", task({ status: "needs_attention" }), 0);
    expect(n?.tone).toBeUndefined();
    expect(n?.tier).toBeUndefined();
    expect(n?.page).toBeUndefined();
    expect(n?.action).toBeUndefined();
    expect(n?.title).toContain("needs your input");
  });

  test("needs_attention fires the same way regardless of the prior status", () => {
    expect(notificationForTransition("upcoming", task({ status: "needs_attention" }), 0)?.title)
      .toContain("needs your input");
    expect(notificationForTransition("done", task({ status: "needs_attention" }), 0)?.title)
      .toContain("needs your input");
  });

  test("a transition not named by the spec's table stays silent", () => {
    expect(notificationForTransition("upcoming", task({ status: "in_progress" }), 0)).toBeNull();
    expect(notificationForTransition("blocked", task({ status: "in_progress" }), 0)).toBeNull();
    expect(notificationForTransition("needs_attention", task({ status: "in_progress" }), 0)).toBeNull();
  });

  test("falls back to a generic noun when the task has no title", () => {
    const n = notificationForTransition("in_progress", task({ status: "blocked", title: "" }), 0);
    expect(n?.title).toBe("A task failed");
  });
});

describe("taskDestination", () => {
  test("prefers the task's own chat, then its folder, then the Tasks page", () => {
    expect(taskDestination(task({ session_id: "s1", target: "/proj" }))).toContain("/proj");
    expect(taskDestination(task({ session_id: "", target: "/proj" }))).toContain("/proj");
    expect(taskDestination(task({ session_id: "", target: "", project: "" }))).toBe("/tasks");
  });
});
