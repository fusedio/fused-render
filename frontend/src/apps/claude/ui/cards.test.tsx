// The three cards' CONTROLS: which buttons a card offers, what each one sends,
// and what it says once the answer has landed. The subprocess is blocked on
// every one of these, so a button that sends the wrong thing — or stops
// offering a way back after a failed send — is the worst bug on the page.
//
// react-test-renderer, the ChatFrame.test.tsx pattern: no DOM, so the tests
// drive the rendered `onClick`/`onChange` props directly and hand the textarea
// handlers a stand-in node (see `fakeField`).
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { RadioGroup } from "@platform/shadcn/ui/radio-group";

import { PermCard } from "./PermCard";
import { PlanCard } from "./PlanCard";
import { QuestionCard } from "./QuestionCard";
import type { PermissionRow } from "../protocol/types";

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement): ReturnType<typeof create> {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

type Json = ReactTestRendererJSON;
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- host props are
// untyped by react-test-renderer; every read below is guarded by the walk.
type Props = Record<string, any>;

function walk(node: Json | string | null, hit: (n: Json) => void): void {
  if (!node || typeof node === "string") return;
  hit(node);
  for (const k of node.children ?? []) walk(k as Json, hit);
}

function textOf(node: Json | string | null): string {
  if (!node) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((k) => textOf(k as Json)).join("");
}

function all(r: ReturnType<typeof create>, type: string): Json[] {
  const out: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (n.type === type) out.push(n);
  });
  return out;
}

function withClass(r: ReturnType<typeof create>, cls: string): Json[] {
  const out: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (String((n.props as Props)?.className ?? "").split(" ").includes(cls)) out.push(n);
  });
  return out;
}

/** The REACT props of a component instance. Base UI's controls put only DOM
 *  props on the node they render, so `onCheckedChange` / `onValueChange` are
 *  only reachable through the element that was asked for. */
function instances(r: ReturnType<typeof create>, type: React.ElementType): Array<{ props: Props }> {
  return r.root.findAllByType(type as never) as unknown as Array<{ props: Props }>;
}

function labels(r: ReturnType<typeof create>): string[] {
  return all(r, "button").map((b) => textOf(b));
}

function press(r: ReturnType<typeof create>, label: string): void {
  const btn = all(r, "button").find((b) => textOf(b) === label);
  if (!btn) throw new Error("no button labelled " + JSON.stringify(label) + " in " + labels(r).join(" | "));
  act(() => {
    (btn.props as Props).onClick(fakeEvent());
  });
}

function fakeEvent(over: Props = {}): Props {
  return {
    preventDefault() {},
    stopPropagation() {},
    nativeEvent: {},
    currentTarget: {},
    target: {},
    ...over,
  };
}

/** Enough of a <textarea> for `growField` to measure without a DOM. */
function fakeField(value: string): Props {
  return {
    value,
    selectionStart: value.length,
    selectionEnd: value.length,
    scrollTop: 0,
    scrollHeight: 20,
    offsetHeight: 20,
    clientHeight: 20,
    style: {},
    scrollIntoView() {},
    focus() {},
  };
}

function row(over: Partial<PermissionRow> = {}): PermissionRow {
  return {
    id: "req-1",
    tool: "Edit",
    input: { file_path: "/a/b.ts", old_string: "x", new_string: "y" },
    created_at: 0,
    decision: "",
    scope: "",
    mode: "",
    answers: {},
    ...over,
  };
}

const noop = async () => {};

// ── PermCard ──────────────────────────────────────────────────────────────
test("PermCard offers permChoices in order and sends the click's verdict", async () => {
  const sent: unknown[] = [];
  const r = mount(
    <PermCard
      row={row()}
      liveMode="prompt"
      onDecide={async (...args) => {
        sent.push(args);
      }}
    />,
  );
  expect(labels(r)).toEqual([
    "Allow",
    "Allow all Edit in this reply",
    "Allow, and let Claude decide from here",
    "Deny",
  ]);
  expect(textOf(withClass(r, "perm-head")[0])).toBe("Claude wants to use Edit");
  press(r, "Allow all Edit in this reply");
  await act(async () => {});
  expect(sent).toEqual([["req-1", "allow", "session", undefined]]);
});

test("PermCard escalation carries the mode; Deny carries neither", async () => {
  // One card per verdict: a card that has sent its answer stays disabled, which
  // is the point — the subprocess has already been told.
  const sent: unknown[] = [];
  const onDecide = async (...args: unknown[]) => {
    sent.push(args);
  };
  const bash = () => row({ tool: "Bash", input: { command: "ls" } });
  const a = mount(<PermCard row={bash()} onDecide={onDecide} />);
  press(a, "Allow, and let Claude decide from here");
  await act(async () => {});
  const b = mount(<PermCard row={bash()} onDecide={onDecide} />);
  press(b, "Deny");
  await act(async () => {});
  expect(sent).toEqual([
    ["req-1", "allow", "once", "auto"],
    ["req-1", "deny", "once", undefined],
  ]);
});

test("PermCard puts the buttons back when the send fails — the run is still blocked", async () => {
  // THE REAL FAILURE SHAPE, and it is not a rejection: `decidePermission`
  // resolves either way, and the controller writes the reason onto the ROW
  // (run-controller `failSend`, T:13996-14002). A card that only watched its own
  // `catch` sat at "sending…" with every button disabled for good.
  const r = mount(
    <PermCard row={row({ tool: "Read", input: { file_path: "/a" } })} onDecide={noop} />,
  );
  press(r, "Allow");
  await act(async () => {});
  expect(textOf(withClass(r, "perm-status")[0])).toBe("sending…");
  act(() => {
    r.update(
      <PermCard
        row={row({
          tool: "Read",
          input: { file_path: "/a" },
          sendError: "Could not send that: bridge is gone",
        })}
        onDecide={noop}
      />,
    );
  });
  expect(textOf(withClass(r, "perm-status")[0])).toBe("Could not send that: bridge is gone");
  expect(all(r, "button").every((b) => !(b.props as Props).disabled)).toBe(true);
});

test("a rejecting onDecide is still handled — a direct caller is not the controller", async () => {
  const r = mount(
    <PermCard
      row={row({ tool: "Read", input: { file_path: "/a" } })}
      onDecide={async () => {
        throw new Error("bridge is gone");
      }}
    />,
  );
  press(r, "Allow");
  await act(async () => {});
  expect(textOf(withClass(r, "perm-status")[0])).toBe("Could not send that: bridge is gone");
  expect(all(r, "button").every((b) => !(b.props as Props).disabled)).toBe(true);
});

test("QuestionCard and PlanCard read the row's sendError too", () => {
  const failed = "Could not send that: the reply has already ended.";
  const q = mount(
    <QuestionCard
      row={row({ tool: "AskUserQuestion", input: oneQ, sendError: failed })}
      onAnswer={noop}
      onDismiss={() => {}}
    />,
  );
  expect(textOf(withClass(q, "perm-status")[0])).toBe(failed);
  expect(all(q, "button").every((b) => !(b.props as Props).disabled)).toBe(true);
  const p = mount(
    <PlanCard
      row={row({ tool: "ExitPlanMode", input: { plan: "do it" }, sendError: failed })}
      onDecide={noop}
    />,
  );
  expect(textOf(withClass(p, "perm-status")[0])).toBe(failed);
  expect(all(p, "button").every((b) => !(b.props as Props).disabled)).toBe(true);
});

test("PermCard disables every button while the answer is in flight", () => {
  let release = () => {};
  const r = mount(
    <PermCard
      row={row()}
      onDecide={() =>
        new Promise<void>((ok) => {
          release = ok;
        })
      }
    />,
  );
  press(r, "Allow");
  expect(all(r, "button").every((b) => (b.props as Props).disabled === true)).toBe(true);
  expect(textOf(withClass(r, "perm-status")[0])).toBe("sending…");
  release();
});

test("a resolved PermCard is a receipt: past tense, no controls, the landed verdict", () => {
  const cases: Array<[Partial<PermissionRow>, string]> = [
    [{ decision: "allow" }, "✓ Allowed"],
    [{ decision: "allow", scope: "session" }, "✓ Allowed — not asking again for Edit in this reply"],
    [{ decision: "allow", mode: "auto" }, "✓ Allowed — approvals set to “Claude decides”"],
    [{ decision: "expired" }, "◦ Unanswered — the reply ended before you decided"],
    [{ decision: "deny" }, "✗ Denied"],
  ];
  for (const [over, said] of cases) {
    const r = mount(<PermCard row={row(over)} onDecide={noop} />);
    expect(labels(r)).toEqual([]);
    expect(textOf(withClass(r, "perm-head")[0])).toBe("Claude wanted to use Edit");
    expect(textOf(withClass(r, "perm-status")[0])).toBe(said);
  }
});

// ── QuestionCard ──────────────────────────────────────────────────────────
const ASK = "AskUserQuestion";
const oneQ = {
  questions: [
    {
      question: "Which one?",
      options: [
        { label: "First", description: "the first" },
        { label: "Second" },
      ],
    },
  ],
};

test("a one-shot question answers on the click, with no submit button", async () => {
  const sent: unknown[] = [];
  const r = mount(
    <QuestionCard
      row={row({ tool: ASK, input: oneQ })}
      onAnswer={async (...args) => {
        sent.push(args);
      }}
      onDismiss={() => {}}
    />,
  );
  expect(labels(r)).toEqual(["Firstthe first", "Second", "Other…Answer in your own words"]);
  press(r, "Second");
  await act(async () => {});
  expect(sent).toEqual([["req-1", { "Which one?": ["Second"] }, undefined]]);
});

test("the Other row swaps in place, and Enter sends the typed text as `custom`", async () => {
  const sent: unknown[] = [];
  const r = mount(
    <QuestionCard
      row={row({ tool: ASK, input: oneQ })}
      onAnswer={async (...args) => {
        sent.push(args);
      }}
      onDismiss={() => {}}
    />,
  );
  press(r, "Other…Answer in your own words");
  const field = withClass(r, "qtype")[0];
  expect(field).toBeDefined();
  act(() => {
    (field.props as Props).onChange(
      fakeEvent({ target: { value: "neither, actually" }, currentTarget: fakeField("neither, actually") }),
    );
  });
  const typed = withClass(r, "qtype")[0];
  // A blank box does nothing at all: an empty answer would reach the model as a
  // question the user "answered" with silence.
  act(() => {
    (typed.props as Props).onKeyDown(fakeEvent({ key: "Enter", shiftKey: false }));
  });
  await act(async () => {});
  expect(sent).toEqual([
    [
      "req-1",
      { "Which one?": ["neither, actually"] },
      { "Which one?": "neither, actually" },
    ],
  ]);
});

test("an empty Other box sends nothing on Enter, and Esc gives the options back", () => {
  const sent: unknown[] = [];
  const r = mount(
    <QuestionCard
      row={row({ tool: ASK, input: oneQ })}
      onAnswer={async (...args) => {
        sent.push(args);
      }}
      onDismiss={() => {}}
    />,
  );
  press(r, "Other…Answer in your own words");
  act(() => {
    (withClass(r, "qtype")[0].props as Props).onKeyDown(fakeEvent({ key: "Enter" }));
  });
  expect(sent).toEqual([]);
  act(() => {
    (withClass(r, "qtype")[0].props as Props).onKeyDown(fakeEvent({ key: "Escape" }));
  });
  expect(withClass(r, "qtype")).toHaveLength(0);
  expect(labels(r)).toContain("Other…Answer in your own words");
});

const twoQ = {
  questions: [
    { question: "A?", options: [{ label: "a1" }, { label: "a2" }], multiSelect: true },
    { question: "B?", options: [{ label: "b1" }] },
  ],
};

test("a multi-question card refuses to submit until every question has an answer", async () => {
  const sent: unknown[] = [];
  const r = mount(
    <QuestionCard
      row={row({ tool: ASK, input: twoQ })}
      onAnswer={async (...args) => {
        sent.push(args);
      }}
      onDismiss={() => {}}
    />,
  );
  expect(labels(r)).toContain("Send answer");
  press(r, "Send answer");
  await act(async () => {});
  expect(sent).toEqual([]);
  expect(textOf(withClass(r, "perm-status")[0])).toBe("Pick an answer for every question.");

  // Tick one option in each block, through the controls' own handlers.
  act(() => {
    instances(r, Checkbox)[0].props.onCheckedChange(true);
  });
  act(() => {
    instances(r, RadioGroup)[0].props.onValueChange("0");
  });
  press(r, "Send answer");
  await act(async () => {});
  expect(sent).toEqual([["req-1", { "A?": ["a1"], "B?": ["b1"] }, {}]]);
});

test("an open, empty Other box is a different mistake from an untouched question", () => {
  const r = mount(
    <QuestionCard
      row={row({ tool: ASK, input: { questions: [twoQ.questions[0]] } })}
      onAnswer={noop}
      onDismiss={() => {}}
    />,
  );
  // The LAST tick is the Other row — it is always the final option.
  const boxes = instances(r, Checkbox);
  act(() => {
    boxes[boxes.length - 1].props.onCheckedChange(true);
  });
  press(r, "Send answer");
  expect(textOf(withClass(r, "perm-status")[0])).toBe(
    "Type your own answer, or pick one of the options.",
  );
});

test("a payload nothing can answer offers Dismiss and shows what arrived", () => {
  const dismissed: string[] = [];
  const input = { questions: [{ question: "A?", options: [] }] };
  const r = mount(
    <QuestionCard
      row={row({ tool: ASK, input })}
      onAnswer={noop}
      onDismiss={(id) => dismissed.push(id)}
    />,
  );
  expect(labels(r)).toEqual(["Dismiss"]);
  expect(textOf(withClass(r, "perm-sub")[0])).toContain("did not arrive in a shape");
  expect(textOf(all(r, "pre")[0])).toBe(JSON.stringify(input, null, 2));
  press(r, "Dismiss");
  expect(dismissed).toEqual(["req-1"]);
});

test("a resolved question shows the choice, not an approval", () => {
  const r = mount(
    <QuestionCard
      row={row({ tool: ASK, input: oneQ, decision: "allow", answers: { "Which one?": "First" } })}
      onAnswer={noop}
      onDismiss={() => {}}
    />,
  );
  expect(textOf(withClass(r, "perm-head")[0])).toBe("Claude asked you");
  expect(textOf(withClass(r, "perm-status")[0])).toBe("✓ You chose: First");
  expect(withClass(r, "perm-status")[0].props.className).toContain("chose");
});

// ── PlanCard ──────────────────────────────────────────────────────────────
const PLAN = "ExitPlanMode";

test("Approve plan sends a plain allow, and the picker's mode only when switchable", async () => {
  const sent: unknown[] = [];
  const onDecide = async (...args: unknown[]) => {
    sent.push(args);
  };
  const r1 = mount(
    <PlanCard row={row({ tool: PLAN, input: { plan: "# do it" } })} pickerMode="plan" onDecide={onDecide} />,
  );
  press(r1, "Approve plan");
  await act(async () => {});
  const r2 = mount(
    <PlanCard
      row={row({ tool: PLAN, input: { plan: "# do it" } })}
      pickerMode="acceptEdits"
      onDecide={onDecide}
    />,
  );
  press(r2, "Approve plan");
  await act(async () => {});
  expect(sent).toEqual([
    ["req-1", "allow", undefined, undefined],
    ["req-1", "allow", "acceptEdits", undefined],
  ]);
});

test("Keep planning carries the note and never a mode", async () => {
  const sent: unknown[] = [];
  const r = mount(
    <PlanCard
      row={row({ tool: PLAN, input: { plan: "# do it" } })}
      onDecide={async (...args) => {
        sent.push(args);
      }}
    />,
  );
  const note = withClass(r, "plan-note")[0];
  expect(note.props.maxLength).toBe(2000);
  act(() => {
    (note.props as Props).onChange(fakeEvent({ target: { value: "smaller steps" } }));
  });
  press(r, "Keep planning");
  await act(async () => {});
  expect(sent).toEqual([["req-1", "deny", undefined, "smaller steps"]]);
});

test("a resolved plan reads as a conversation, not a refusal", () => {
  const cases: Array<[PermissionRow["decision"], string]> = [
    ["allow", "✓ Plan approved"],
    ["deny", "◦ Sent back for revision"],
    ["expired", "◦ Unanswered — the reply ended before you decided"],
  ];
  for (const [decision, said] of cases) {
    const r = mount(
      <PlanCard row={row({ tool: PLAN, input: { plan: "# p" }, decision })} onDecide={noop} />,
    );
    expect(labels(r)).toEqual([]);
    expect(withClass(r, "plan-note")).toHaveLength(0);
    expect(textOf(withClass(r, "perm-head")[0])).toBe("Claude had a plan");
    expect(textOf(withClass(r, "perm-status")[0])).toBe(said);
  }
});

test("a plan with no usable plan string dumps it rather than implying one was read", () => {
  const r = mount(<PlanCard row={row({ tool: PLAN, input: { plan: 7 } })} onDecide={noop} />);
  expect(withClass(r, "plan-body")).toHaveLength(0);
  expect(textOf(all(r, "pre")[0])).toBe(JSON.stringify({ plan: 7 }, null, 2));
});

// QA round 3a, defect 3: `{"planFilePath": "/Users/…/plans/make-a-3-step-….md"}`
// was rendered verbatim inside the card, on the first render and on every "Keep
// planning" revision. The disclosure dump is for input the model CHOSE and the
// user is being asked to approve; the CLI's own scratch path is neither, and an
// absolute internal path in a user-facing card is an id in the UI.
test("the plan card never dumps the CLI's own planFilePath", () => {
  const r = mount(
    <PlanCard
      row={row({
        tool: PLAN,
        input: {
          plan: "## Steps\n1. one\n2. two\n3. three",
          planFilePath: "/Users/someone/.claude/plans/make-a-3-step-plan-abc.md",
        },
      })}
      onDecide={noop}
    />,
  );
  expect(all(r, "pre")).toHaveLength(0);
  expect(textOf(r.toJSON() as Json)).not.toContain("planFilePath");
  expect(textOf(r.toJSON() as Json)).not.toContain(".claude/plans");
  // The plan itself still renders, and the card still offers both verdicts.
  expect(withClass(r, "plan-body")).toHaveLength(1);
  expect(labels(r)).toContain("Approve plan");
});

test("planFilePath is covered even when the plan itself is unusable", () => {
  // The plan falls into the dump (the card must not imply one was read); the
  // path still does not.
  const r = mount(
    <PlanCard
      row={row({ tool: PLAN, input: { plan: 7, planFilePath: "/tmp/p.md" } })}
      onDecide={noop}
    />,
  );
  expect(withClass(r, "plan-body")).toHaveLength(0);
  expect(textOf(all(r, "pre")[0])).toBe(JSON.stringify({ plan: 7 }, null, 2));
});

// QA round 3a, defect 4: `.qopt` is a flex row whose ONE child is `.qbody`
// (`flex: 1; min-width: 0`). A textarea hoisted out to be `.qbody`'s sibling
// becomes a second flex item whose `width: 100%` flex-base crushes the label
// column to min-content, and the caption then wrapped one character per line
// beside the box. This pins the STRUCTURE T builds (T:14346-14351): label,
// caption and field all inside the one column.
test("the open Other row keeps its caption and field in a single .qbody column", () => {
  const r = mount(
    <QuestionCard
      row={row({ tool: ASK, input: oneQ })}
      onAnswer={noop}
      onDismiss={() => {}}
    />,
  );
  press(r, "Other…Answer in your own words");
  const open = withClass(r, "qother")[0];
  expect(String((open.props as Props).className)).toContain("typing");
  // Exactly ONE flex item in the row, and it owns everything.
  const children = (open.children ?? []).filter((k) => typeof k !== "string") as Json[];
  expect(children).toHaveLength(1);
  const body = children[0]!;
  expect(String((body.props as Props).className)).toBe("qbody");
  // The caption is inside it, beside the label and the field — not a sibling of
  // the textarea in the flex row.
  const inBody: string[] = [];
  walk(body, (n) => {
    const cls = String((n.props as Props)?.className ?? "");
    if (cls === "lbl" || cls === "desc" || cls === "qtype") inBody.push(cls);
  });
  expect(inBody).toEqual(["lbl", "desc", "qtype"]);
  expect(textOf(body)).toContain("Answer in your own words");
  // And the field is reachable exactly once, from inside the column.
  expect(withClass(r, "qtype")).toHaveLength(1);
});
