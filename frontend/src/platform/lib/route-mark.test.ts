// `routeMark` — what `useNavEpoch` compares a Back/Forward against, and
// therefore the rule that decides whether a traversal REMOUNTS the route.
//
// It exists because a page may push history entries of its own: the task side
// peek pushes one per open, swap and close so Back can undo them, and every one
// of those entries is the same page. Bumping the route epoch for those remounted
// the Tasks page to close a panel — losing the search text, the filters, the
// expanded rows and the scroll position the panel was opened from.
//
// Pure, so the rule is checked without a history to walk.
import { describe, expect, it } from "bun:test";
import { routeMark } from "./hooks";

describe("routeMark", () => {
  it("is the whole URL when nothing is ignored — today's behaviour, unchanged", () => {
    expect(routeMark("/tasks", "?view=board")).toBe("/tasks?view=board");
    expect(routeMark("/tasks", "")).toBe("/tasks");
  });

  it("drops the named params, so a peek-only traversal reads as the same route", () => {
    const before = routeMark("/tasks", "?view=board", ["peek"]);
    const after = routeMark("/tasks", "?view=board&peek=sess-1", ["peek"]);
    expect(after).toBe(before);
  });

  it("keeps every OTHER param, so a filter or a view change is still a change", () => {
    expect(routeMark("/tasks", "?view=board&peek=sess-1", ["peek"])).toBe("/tasks?view=board");
    expect(routeMark("/tasks", "?view=list&peek=sess-1", ["peek"])).not.toBe(
      routeMark("/tasks", "?view=board&peek=sess-1", ["peek"]),
    );
  });

  it("still separates two different PATHS", () => {
    expect(routeMark("/tasks", "?peek=a", ["peek"])).not.toBe(
      routeMark("/apps", "?peek=a", ["peek"]),
    );
  });

  it("leaves a bare path bare rather than trailing a lone question mark", () => {
    expect(routeMark("/tasks", "?peek=sess-1", ["peek"])).toBe("/tasks");
  });
});
