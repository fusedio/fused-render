// An approval event's text is one sentence the backend composes: "About to {preview}. {why} Approve?", with `detail`
// carrying the bare preview (agent_engine._gate, steps_engine). The card shows the preview on its own as the body and
// the why as its sub line.

export interface ApprovalParts {
  body: string;
  /** "command" when the preview is a "$ " shell line, else "request". */
  bodyWhat: "command" | "request";
  sub?: string;
}

export function approvalParts(e: { text: string; detail?: unknown }): ApprovalParts {
  const detail = typeof e.detail === "string" && e.detail.trim() ? e.detail : "";
  let body = detail || e.text;
  let bodyWhat: ApprovalParts["bodyWhat"] = "request";
  if (body.startsWith("$ ")) {
    body = body.slice(2);
    bodyWhat = "command";
  }
  let sub: string | undefined;
  const head = `About to ${detail}.`;
  if (detail && e.text.startsWith(head) && e.text.endsWith("Approve?")) {
    const why = e.text.slice(head.length, e.text.length - "Approve?".length).trim();
    if (why) sub = why;
  }
  return { body, bodyWhat, sub };
}
