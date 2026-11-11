// The open-canvas ask (D1471, F5): `canvas_screenshot` was called and no page
// of its canvas is open anywhere, so the permission server parked this card in
// `perm/` — the task reads blocked, the way an approval does — instead of a
// tool call that waits out its timeout.
//
//   * Open sends `allow`, then goes to the canvas page; that page's own
//     responder (apps/canvases/canvas-page-shot-responder) takes the picture;
//   * Not now sends `deny`; the tool returns at once and the agent carries on
//     without a screenshot. No answer is the same as Not now, at the ceiling.
//   * On the phone (the LAN door) there is no Open: the canvas page is not
//     served there, and opening it on the Mac is not the phone's to do.
import { useState } from "react";

import { GateCard, type GateStatus } from "@platform/ui/GateCard";
import { isLanHost } from "@platform/lib/host";
import { cn } from "@platform/lib/utils";

import type { ChatController } from "../protocol/controller-api";
import type { PermissionRow } from "../protocol/types";
import { useAgent } from "./agent-context";
import { answerHeld, queuedAnswerText } from "./PermCard";

/** The canvas page's path — `canvasPagePath(env, name)` with no options
 *  (apps/canvases/api; this app may not import that one, so a test pins the
 *  two together). */
export function openCanvasPath(env: string, name: string): string {
  return `/canvases/${env}/${name}`;
}

export interface OpenCanvasCardProps {
  row: PermissionRow;
  onDecide: ChatController["decidePermission"];
  navigate?: (url: string) => void;
}

function statusOf(row: PermissionRow): GateStatus {
  // Answered but held by the folder's queue: the tool has not seen it yet.
  if (answerHeld(row)) return { tone: "queued", text: queuedAnswerText(row.queuedAhead || "") };
  if (row.decision === "allow") return { tone: "allow", text: "✓ Opened" };
  if (row.reason === "cancelled") return { tone: "expired", text: "◦ Stopped before you decided" };
  // Only a click with no `reason` is the reader's Not now.
  if (row.decision === "expired" || (row.decision && row.reason)) {
    return { tone: "expired", text: "◦ Unanswered — continued without a screenshot" };
  }
  if (row.decision) return { tone: "deny", text: "Not now — continued without a screenshot" };
  return { tone: "", text: "" };
}

export function OpenCanvasCard({ row, onDecide, navigate }: OpenCanvasCardProps) {
  const agent = useAgent();
  const [sent, setSent] = useState(false);
  const [threw, setThrew] = useState("");
  const input = row.input as Record<string, unknown>;
  const env = typeof input.env === "string" ? input.env : "";
  const name = typeof input.name === "string" ? input.name : "";
  const resolved = !!row.decision || answerHeld(row);
  const sendError = row.sendError || (threw ? "Could not send that: " + threw : "");
  const posting = sent && !sendError && !resolved;

  async function send(open: boolean) {
    setSent(true);
    setThrew("");
    try {
      await onDecide(row.id, open ? "allow" : "deny", "once");
      if (open && env && name) navigate?.(openCanvasPath(env, name));
    } catch (err) {
      setSent(false);
      setThrew(err instanceof Error ? err.message : String(err));
    }
  }

  const status = resolved
    ? statusOf(row)
    : sendError
      ? { tone: "deny" as const, text: sendError }
      : { tone: "" as const, text: posting ? "sending…" : "" };
  return (
    <GateCard
      className={cn("turn", "perm", resolved && "resolved")}
      rootProps={{ "data-perm-id": row.id }}
      head={
        <>
          {resolved ? `${agent.label} wanted to open ` : `${agent.label} wants to open `}
          <span className="gate-tool">{name || "the canvas"}</span>
          {" to take a screenshot"}
        </>
      }
      bodyWhat="input"
      resolved={resolved}
      actions={[
        ...(isLanHost()
          ? []
          : [{ key: "open", text: "Open", kind: "primary" as const, disabled: posting || !env || !name, onClick: () => void send(true) }]),
        { key: "not-now", text: "Not now", kind: "default", disabled: posting, onClick: () => void send(false) },
      ]}
      status={status}
    />
  );
}

export default OpenCanvasCard;
