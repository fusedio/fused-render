// AskUserQuestion — the model asking the USER something, parked on the same
// bridge as an approval because the CLI routes it through the prompt tool
// (T:14056-14475).
//
// What goes back is a CHOICE, not a verdict: every control sends `decide` with
// `answers`, keyed by the exact question text, whose value is the chosen
// option's own `label`. There is deliberately no plain Allow (an allow with no
// answers reaches the model as "the user did not answer"), no "allow all in this
// reply" and no mode switch — a question is one exchange.
//
// Every string on the card is model-authored and goes in as a text node, with
// ONE deliberate exception: every question also gets an "Other" row this window
// adds (D407). The typed text rides a SEPARATE `custom` record rather than
// being smuggled in as a label, so `answers` stays strictly "a string the
// request itself offered".
import { useRef, useState } from "react";

import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { RadioGroup, RadioGroupItem } from "@platform/shadcn/ui/radio-group";
import { cn } from "@platform/lib/utils";

import type { ChatController } from "../protocol/controller-api";
import { OTHER_MAX_H, questionModel, questionOptions } from "../protocol/summaries";
import type { PermissionRow, Question, QuestionOption } from "../protocol/types";

/** The row Claude did not write, last always: it is the answer for when none of
 *  the ones above are, and the backend matches a multi-select join in exactly
 *  this order (T:14330-14333). */
const OTHER: QuestionOption = { label: "Other…", description: "Answer in your own words" };

export interface QuestionCardProps {
  row: PermissionRow;
  onAnswer: ChatController["answerQuestion"];
  /** T:14118 — a payload nothing can answer is dismissed, which lands as a
   *  `deny` and lets the reply continue. */
  onDismiss: ChatController["dismissCard"];
}

/** T:14196-14232 `otherField`'s grow: the caret's at-end-ness is read BEFORE
 *  the height changes, because setting a height re-lays-out the textarea and
 *  drops its scrollTop to 0 — past the ceiling every keystroke scrolled the
 *  view back to the first line and the user typed blind. */
function growField(field: HTMLTextAreaElement | null): void {
  if (!field) return;
  const end = (field.value || "").length;
  const atEnd = field.selectionStart === end && field.selectionEnd === end;
  const was = field.scrollTop;
  field.style.height = "auto";
  // scrollHeight is padding-box and the height being set is border-box; without
  // the difference added back the box lands one border-width short of its own
  // content and reports a scrollbar on a single line.
  const wanted = field.scrollHeight + (field.offsetHeight - field.clientHeight);
  field.style.height = Math.min(OTHER_MAX_H, wanted) + "px";
  field.scrollTop = atEnd ? field.scrollHeight : was;
  // `nearest` is the whole point: it moves whichever ancestor is scrolling by
  // the least it can, and does nothing when the row is already fully visible,
  // so it cannot yank the transcript away from someone reading it.
  field.scrollIntoView({ block: "nearest" });
}

/** After the commit that made the row visible. A hidden textarea reports a
 *  scrollHeight of 0, so both callers have to wait for the paint; `setTimeout`
 *  is the fallback for a host with no rAF (a test runner). */
function afterPaint(fn: () => void): void {
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(fn);
  else setTimeout(fn, 0);
}

function OptionText({ option, className }: { option: QuestionOption; className?: string }) {
  return (
    <span className={className}>
      <span className="lbl">{option.label}</span>
      {typeof option.description === "string" && option.description ? (
        <span className="desc">{option.description}</span>
      ) : null}
    </span>
  );
}

/** The editor an "Other" row opens into. A TEXTAREA, not a one-line input: an
 *  answer the model did not anticipate is the one most likely to be a sentence,
 *  and a single line scrolls it away horizontally — the user cannot read back
 *  what they are about to send. Hoisted out of the card so a keystroke does not
 *  remount it and take the caret with it. */
function OtherField({
  fieldRef,
  value,
  placeholder,
  disabled,
  onValue,
  onKeys,
}: {
  fieldRef: (el: HTMLTextAreaElement | null) => void;
  value: string;
  placeholder: string;
  disabled: boolean;
  onValue: (next: string) => void;
  onKeys: (ev: React.KeyboardEvent<HTMLTextAreaElement>) => void;
}) {
  return (
    <textarea
      ref={fieldRef}
      className="qtype"
      rows={1}
      wrap="soft"
      placeholder={placeholder}
      disabled={disabled}
      value={value}
      onChange={(ev) => {
        onValue(ev.target.value);
        growField(ev.currentTarget);
      }}
      onKeyDown={onKeys}
      // Inside a <label> the field is interactive content, so a click on it must
      // not activate the tick — a click that silently unticked the row the user
      // is typing into would be unexplainable.
      onClick={(ev) => ev.stopPropagation()}
    />
  );
}

export function QuestionCard({ row, onAnswer, onDismiss }: QuestionCardProps) {
  const { questions, answerable, oneShot, extra } = questionModel(row.input);
  // SIZED FROM THE CURRENT ROW, not once at mount. A replayed row whose
  // `questions` array changes length (T rebuilds the card per id) left these
  // short, and `picked[i]` came back undefined for the questions past the end.
  const [picked, setPicked] = useState<string[][]>(() => questions.map(() => []));
  const [otherOpen, setOtherOpen] = useState<boolean[]>(() => questions.map(() => false));
  const [otherText, setOtherText] = useState<string[]>(() => questions.map(() => ""));
  const [sent, setSent] = useState(false);
  const [threw, setThrew] = useState("");
  const [invalid, setInvalid] = useState("");
  const fields = useRef<Array<HTMLTextAreaElement | null>>([]);
  const otherBtns = useRef<Array<HTMLButtonElement | null>>([]);
  const sizedFor = useRef(questions.length);
  if (sizedFor.current !== questions.length) {
    sizedFor.current = questions.length;
    setPicked(questions.map(() => []));
    setOtherOpen(questions.map(() => false));
    setOtherText(questions.map(() => ""));
  }

  const resolved = !!row.decision;
  // `answerQuestion` does not reject: the controller catches and writes
  // `row.sendError` (T:14108-14112). See PermCard's fuller note.
  const sendError = row.sendError || (threw ? "Could not send that: " + threw : "");
  const posting = sent && !sendError && !resolved;
  const typedOf = (i: number) => (otherOpen[i] ? (otherText[i] || "").trim() : "");
  const labelsOf = (i: number) => {
    const own = typedOf(i);
    const chose = picked[i] ?? [];
    return own ? [...chose, own] : chose;
  };

  async function post(run: () => Promise<void>) {
    setSent(true);
    setThrew("");
    setInvalid("");
    try {
      await run();
    } catch (err) {
      // The subprocess is still blocked, so the controls have to come back.
      setSent(false);
      setThrew(err instanceof Error ? err.message : String(err));
    }
  }

  /** One question answered in one click (the oneShot path) or one typed line. */
  const sendOne = (q: Question, value: string, typed?: boolean) =>
    post(() =>
      onAnswer(row.id, { [q.question]: [value] }, typed ? { [q.question]: value } : undefined),
    );

  function submit() {
    const answers: Record<string, string[]> = {};
    const custom: Record<string, string> = {};
    for (let i = 0; i < questions.length; i++) {
      const labels = labelsOf(i);
      if (!labels.length) {
        // An open, empty Other box is a different mistake from an untouched
        // question, and saying "pick an answer" to someone who has already
        // decided not to pick one is no help.
        setInvalid(
          otherOpen[i] && !typedOf(i)
            ? "Type your own answer, or pick one of the options."
            : "Pick an answer for every question.",
        );
        const field = fields.current[i];
        if (field) {
          growField(field);
          field.focus();
        }
        return;
      }
      answers[questions[i].question] = labels;
      const own = typedOf(i);
      if (own) custom[questions[i].question] = own;
    }
    void post(() => onAnswer(row.id, answers, custom));
  }

  const chosen = resolved
    ? Object.values(row.answers ?? {}).filter((v): v is string => typeof v === "string")
    : [];
  const status = resolved
    ? row.decision === "allow"
      ? chosen.length
        ? { cls: "chose", node: <>{"✓ You chose: "}<span className="val">{chosen.join(" · ")}</span></> }
        : { cls: "allow", node: <>✓ Answered</> }
      : row.decision === "expired"
        ? { cls: "expired", node: <>◦ Unanswered — the reply ended before you answered</> }
        : { cls: "deny", node: <>✗ Not answered</> }
    : sendError
      ? { cls: "deny", node: <>{sendError}</> }
      : invalid
        ? { cls: "deny", node: <>{invalid}</> }
        : posting
          ? { cls: "", node: <>sending…</> }
          : { cls: "", node: null };

  function setOne<T>(list: T[], i: number, value: T): T[] {
    const next = list.slice();
    next[i] = value;
    return next;
  }

  /** Opening the box: grow BEFORE focus, and only now — a hidden textarea
   *  reports a scrollHeight of 0, so a height measured while the row was closed
   *  would open it flat (T:14357-14359). */
  function openOther(i: number) {
    setOtherOpen((o) => setOne(o, i, true));
    afterPaint(() => {
      const field = fields.current[i];
      growField(field);
      field?.focus();
    });
  }

  function closeOther(i: number, focusBtn: boolean) {
    setOtherOpen((o) => setOne(o, i, false));
    if (focusBtn) afterPaint(() => otherBtns.current[i]?.focus());
  }

  /** T:14238-14252 — Enter answers, Shift+Enter breaks the line, a blank box
   *  does nothing at all, Esc gives the options back, and keydown stops here so
   *  the pane's own shortcuts do not read an answer as commands. */
  function fieldKeys(i: number, q: Question, ev: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      const value = (otherText[i] || "").trim();
      if (value) {
        if (oneShot) void sendOne(q, value, true);
        else submit();
      }
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      if (oneShot) closeOther(i, true);
      else closeOther(i, false);
    }
    ev.stopPropagation();
  }

  return (
    <div className={cn("turn", "perm", "ask", resolved && "resolved")}>
      <div className="perm-head">{resolved ? "Claude asked you" : "Claude is asking you"}</div>
      {answerable ? (
        <div className="qscroll">
          {questions.map((q, i) => {
            const opts = questionOptions(q);
            const multi = !!q.multiSelect;
            const rows = (
              <>
                {opts.map((option, oi) =>
                  oneShot ? (
                    <Button
                      key={oi}
                      type="button"
                      variant="ghost"
                      className="qopt"
                      disabled={posting || resolved}
                      onClick={() => void sendOne(q, option.label)}
                    >
                      <OptionText option={option} />
                    </Button>
                  ) : (
                    <label key={oi} className="qopt">
                      {multi ? (
                        <Checkbox
                          className="qtick"
                          disabled={posting || resolved}
                          checked={(picked[i] ?? []).includes(option.label)}
                          onCheckedChange={(on) =>
                            setPicked((p) =>
                              setOne(
                                p,
                                i,
                                on
                                  ? [...p[i], option.label]
                                  : p[i].filter((l) => l !== option.label),
                              ),
                            )
                          }
                        />
                      ) : (
                        <RadioGroupItem
                          className="qtick is-radio"
                          disabled={posting || resolved}
                          value={String(oi)}
                        />
                      )}
                      <OptionText option={option} />
                    </label>
                  ),
                )}
                {/* …and the row Claude did not write. */}
                {oneShot ? (
                  otherOpen[i] ? (
                    <div className="qopt qother typing">
                      {/* The SAME body the button carries, not a bare field: an
                          unlabelled input left a grey box the user could not
                          tell what they were answering (T:14346-14351).

                          THE FIELD LIVES INSIDE `.qbody`, never beside it
                          (T:14350 — `body.appendChild(field)`, then
                          `wrap.appendChild(body)`). `.qopt` is a flex row and
                          `.qbody` is its one `flex: 1; min-width: 0` child; a
                          textarea hoisted out to be `.qbody`'s SIBLING becomes a
                          second flex item whose `width: 100%` flex-base crushes
                          the label column to min-content, and the caption
                          ("Answer in your own words") then wrapped one character
                          per line beside the box (QA round 3a, defect 4). */}
                      <span className="qbody">
                        <OptionText option={OTHER} />
                        <OtherField
                          fieldRef={(el) => {
                            fields.current[i] = el;
                          }}
                          value={otherText[i] ?? ""}
                          placeholder={
                            oneShot ? "Type your answer, then press Enter" : "Type your answer here"
                          }
                          disabled={posting}
                          onValue={(next) => setOtherText((t) => setOne(t, i, next))}
                          onKeys={(ev) => fieldKeys(i, q, ev)}
                        />
                      </span>
                    </div>
                  ) : (
                    // A plain <button>, unlike the option buttons beside it:
                    // Esc in the box gives the options back and focuses THIS
                    // control again (T:14352), which needs a ref — and under
                    // React 18 a ref cannot reach a function component. The box
                    // it is styled by (`.qopt`) is ours either way.
                    <button
                      type="button"
                      className="qopt qother"
                      disabled={posting || resolved}
                      ref={(el) => {
                        otherBtns.current[i] = el;
                      }}
                      onClick={() => openOther(i)}
                    >
                      <OptionText option={OTHER} />
                    </button>
                  )
                ) : (
                  <label className={cn("qopt", "qother", otherOpen[i] && "typing")}>
                    {multi ? (
                      <Checkbox
                        className="qtick"
                        disabled={posting || resolved}
                        checked={!!otherOpen[i]}
                        onCheckedChange={(on) => (on ? openOther(i) : closeOther(i, false))}
                      />
                    ) : (
                      <RadioGroupItem className="qtick is-radio" disabled={posting || resolved} value="other" />
                    )}
                    <span className="qbody">
                      <OptionText option={OTHER} />
                      <OtherField
                        fieldRef={(el) => {
                          fields.current[i] = el;
                        }}
                        value={otherText[i] ?? ""}
                        placeholder={
                          oneShot ? "Type your answer, then press Enter" : "Type your answer here"
                        }
                        disabled={posting}
                        onValue={(next) => setOtherText((t) => setOne(t, i, next))}
                        onKeys={(ev) => fieldKeys(i, q, ev)}
                      />
                    </span>
                  </label>
                )}
              </>
            );
            return (
              <div key={i} className="qblock">
                {typeof q.header === "string" && q.header ? (
                  <div className="qhead">{q.header}</div>
                ) : null}
                <div className="qtext">{q.question}</div>
                {/* RESOLVED TAKES THE OPTIONS AWAY rather than merely disabling
                    them: T removes every control on resolve
                    (`controls.forEach(c => c.remove())`, T:14434), leaving the
                    question text and the verdict. A greyed list of options
                    under "✓ You chose: …" re-asks a question that is
                    answered. */}
                {resolved ? null : oneShot || multi ? (
                  <div className="qopts">{rows}</div>
                ) : (
                  // A radio group's single value covers the options AND the
                  // Other row: they are one group, so ticking a sibling unticks
                  // the box the user was typing in (T:14380-14384).
                  <RadioGroup
                    className="qopts"
                    value={otherOpen[i] ? "other" : indexOfPicked(opts, picked[i] ?? [])}
                    onValueChange={(value) => {
                      if (value === "other") {
                        setPicked((p) => setOne(p, i, []));
                        openOther(i);
                        return;
                      }
                      const oi = Number(value);
                      setOtherOpen((o) => setOne(o, i, false));
                      setPicked((p) => setOne(p, i, [opts[oi]?.label ?? ""]));
                    }}
                  >
                    {rows}
                  </RadioGroup>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        // Nothing here can be answered, so say that and show what arrived
        // verbatim rather than offering controls that could only send an
        // invalid answer.
        <>
          <div className="perm-sub">
            This question did not arrive in a shape this window can answer — dismissing it lets
            the reply continue.
          </div>
          <pre>{JSON.stringify(row.input, null, 2)}</pre>
        </>
      )}
      {/* Same rule as an approval card: no input key is invisible. */}
      {answerable && extra ? <pre>{JSON.stringify(extra, null, 2)}</pre> : null}
      {!oneShot && !resolved ? (
        <div className="perm-actions qsend">
          <Button
            type="button"
            variant="ghost"
            className={cn("perm-btn", answerable && "primary")}
            disabled={posting}
            onClick={answerable ? submit : () => onDismiss(row.id)}
          >
            {answerable ? "Send answer" : "Dismiss"}
          </Button>
        </div>
      ) : null}
      <div className={cn("perm-status", status.cls)}>{status.node}</div>
    </div>
  );
}

/** The radio group's value for whatever is ticked: the option's INDEX, so a
 *  model-authored label of "other" cannot collide with the row we add. */
function indexOfPicked(opts: QuestionOption[], picked: string[]): string {
  const i = opts.findIndex((o) => picked.includes(o.label));
  return i < 0 ? "" : String(i);
}

export default QuestionCard;
