import { describe, expect, it } from "bun:test";
import { lastChat, lastChatId, setLastChat } from "./last-chat";

describe("last-chat", () => {
  it("names a row by project and task number, not by key", () => {
    const queued = { project: "/p", task_id: "TASK-3", key: "pending:e1" };
    const ran = { project: "/p", task_id: "TASK-3", key: "sess-1" };
    expect(lastChatId(queued)).toBe(lastChatId(ran));
    // The number is per project: the same number elsewhere is another chat.
    expect(lastChatId({ ...ran, project: "/q" })).not.toBe(lastChatId(ran));
    // No number yet: the key stands in.
    expect(lastChatId({ project: "/p", task_id: "", key: "new:/p/a.py" })).toContain("new:/p/a.py");
  });

  it("a press moves the mark", () => {
    setLastChat("a");
    expect(lastChat()).toBe("a");
    setLastChat("b");
    expect(lastChat()).toBe("b");
  });
});
