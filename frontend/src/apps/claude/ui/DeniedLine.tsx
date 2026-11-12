// The line under a tool chip that auto mode's classifier refused: why, and the
// one button that lets this exact call run. A refusal never reaches the approve
// tool, so there is no card to answer — this is the whole of the user's say.
import { useState } from "react";

import type { ToolSegment } from "../protocol/types";
import { useAllowDenied } from "./allowDeniedContext";

export function DeniedLine({ seg }: { seg: ToolSegment }) {
  const allow = useAllowDenied();
  const [posting, setPosting] = useState(false);
  const [failed, setFailed] = useState("");
  const denied = seg.denied;
  if (!denied) return null;
  const allowed = !!denied.allowed;
  const click = () => {
    setPosting(true);
    setFailed("");
    allow!(seg.id)
      .catch((err: unknown) => setFailed(err instanceof Error ? err.message : "Could not allow that."))
      .finally(() => setPosting(false));
  };
  return (
    <div className="chip-denied">
      <span className="chip-denied-why">
        Blocked by auto mode{denied.reason ? <> · {denied.reason}</> : null}
      </span>
      {allow && seg.id ? (
        <button
          type="button"
          className="chip-denied-btn"
          disabled={allowed || posting}
          onClick={click}
        >
          {allowed ? "Allowed" : posting ? "Allowing…" : "Allow"}
        </button>
      ) : null}
      {failed ? (
        <span className="chip-denied-err" role="alert">
          {failed}
        </span>
      ) : null}
    </div>
  );
}
