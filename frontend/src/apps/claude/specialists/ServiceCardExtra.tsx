// A CONNECTED SERVICE'S CARD in a specialist's main chat (PRODUCT §7, PLAN A.2
// "Connected services"): the ordinary approval card for an
// `mcp__specialist_services__*` tool, plus "Approve, don't ask again" — allow
// this call once AND add the service to the specialist's allowed list, so the
// live session's next call of that service is allowed by the policy endpoint.
// It is the ONLY thing that allows a service (the chat's own "Always approve"
// does not). In a breakout it is offered for reads only: a write always asks.
import { useEffect, useState } from "react";
import {
  errorText,
  getConnections,
  putConnections,
  serviceKeyForTool,
  SERVICE_TOOL_PREFIX,
  type ServiceRow,
} from "@platform/lib/specialists-api";
import type { CardActions } from "../ui/CardStack";

export function isServiceTool(tool: string): boolean {
  return tool.startsWith(SERVICE_TOOL_PREFIX);
}

/** Is `tool` listed as read-only by its service row? (CONTRACT: the optional
 *  `tools` on GET …/connections rows; absent = not known to be a read.) */
export function isKnownRead(tool: string, services: readonly ServiceRow[]): boolean {
  for (const s of services) for (const t of s.tools ?? []) if (t.name === tool) return t.write === false;
  return false;
}

export function ServiceCardExtra({
  specialistId,
  requestId,
  tool,
  actions,
  kind = "main",
  load = getConnections,
  save = putConnections,
}: {
  specialistId: string;
  requestId: string;
  tool: string;
  actions: Pick<CardActions, "decidePermission">;
  kind?: "main" | "breakout";
  // Seams for tests.
  load?: typeof getConnections;
  save?: typeof putConnections;
}) {
  const [services, setServices] = useState<readonly ServiceRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    let live = true;
    load(specialistId).then(
      (r) => {
        if (live) setServices(r.services || []);
      },
      () => {
        if (live) setServices([]);
      },
    );
    return () => {
      live = false;
    };
  }, [specialistId, load]);

  const key = services ? serviceKeyForTool(tool, services) : null;
  if (!key) return null;
  if (kind === "breakout" && !isKnownRead(tool, services ?? [])) return null;
  const title = services?.find((s) => s.key === key)?.title || key;

  const approveAlways = async () => {
    setBusy(true);
    setError("");
    try {
      await actions.decidePermission(requestId, "allow", "once");
      await save(specialistId, { add: [key] });
      setDone(true);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="spx-service-extra">
      {done ? (
        <span role="status">{title} is allowed for this specialist from now on.</span>
      ) : (
        <button type="button" className="spx-btn" onClick={() => void approveAlways()} disabled={busy}>
          Approve, don't ask again
        </button>
      )}
      {error ? (
        <span className="spx-chip-error" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
