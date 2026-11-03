// The open-card reveal targets the NEWEST `[data-perm-id]` card, not the whole
// tail wrapper (which also holds the working line).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

import type { ChatState } from "../protocol/controller-api";
import type { PermissionRow } from "../protocol/types";
import { Transcript } from "./Transcript";

const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

const row = (id: string): PermissionRow => ({
  id,
  tool: "Bash",
  input: { command: "ls" },
  created_at: 0,
  decision: "",
  scope: "",
  mode: "",
  answers: {},
  placement: "open",
} as PermissionRow);

function state(over: Partial<ChatState> = {}): ChatState {
  return {
    file: "/proj", sessionId: "s1", runId: null, lastRunId: null, status: "idle",
    turns: [], permissions: [], appState: [], skills: [], working: null, trouble: null,
    permissionMode: "prompt", queued: [], inbox: [], historyLoading: false, adopting: false,
    transcript: null, context: null, tasks: [], tasksRunId: null, ownRunEndedAt: 0,
    repaired: 0, transcriptGen: 0, rev: 1, ...over,
  } as ChatState;
}

const actions = {
  decidePermission: async () => {},
  answerQuestion: async () => {},
  decidePlan: async () => {},
  dismissCard: () => {},
  stopRun: async () => {},
  addNote: () => {},
};

test("a tall newest card is revealed top-first by ITS box, not the wrapper's", () => {
  const rect = (top: number, height: number) => ({ top, bottom: top + height, left: 0, right: 300, width: 300, height });
  // Rects move with the scroll, as real ones do: the reveal may run more than once.
  const first = { dataset: { permId: "p1" }, getBoundingClientRect: () => rect(0, 100) };
  const last = { dataset: { permId: "p2" }, getBoundingClientRect: () => rect(500 - port.scrollTop, 900) };
  const wrapper = {
    // The wrapper is taller still and starts higher: using it would land elsewhere.
    getBoundingClientRect: () => rect(0, 1600),
    querySelectorAll: (sel: string) => (sel === "[data-perm-id]" ? [first, last] : []),
  };
  const port: { scrollTop: number; [k: string]: unknown } = {
    scrollTop: 0, scrollHeight: 2000, clientHeight: 400,
    getBoundingClientRect: () => rect(0, 400),
    addEventListener() {}, removeEventListener() {},
    querySelectorAll: () => [], querySelector: () => null,
    classList: { add() {}, remove() {}, contains: () => false },
  };
  const G = globalThis as Record<string, unknown>;
  const realRO = G.ResizeObserver;
  G.ResizeObserver = class { observe() {} disconnect() {} };
  try {
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(
        <Transcript state={state({ permissions: [row("p1"), row("p2")] })} actions={actions} />,
        {
          createNodeMock: (el) => {
            const cls = String((el.props as { className?: string }).className ?? "");
            if (cls.includes("chat-tailpin")) return wrapper;
            return cls.includes("chat-logwrap") ? port : { ...port, scrollTop: 0 };
          },
        },
      );
    });
    mounted.push(r);
    expect(port.scrollTop).toBe(500 - 12);
  } finally {
    if (realRO === undefined) delete G.ResizeObserver;
    else G.ResizeObserver = realRO;
  }
});
