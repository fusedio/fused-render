// Settings › Connections (PRODUCT §7): which connected services and local app
// tools this specialist may use without asking. Same meaning as a bot's
// connections; the sign-in itself is the app's one global sign-in.
import { useEffect, useState } from "react";
import { errorText, getConnections, putConnections, type ServiceRow } from "@platform/lib/specialists-api";

export function ConnectionsTab({
  specialistId,
  api = { getConnections, putConnections },
}: {
  specialistId: string;
  api?: { getConnections: typeof getConnections; putConnections: typeof putConnections };
}) {
  const [services, setServices] = useState<ServiceRow[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const get = api.getConnections;
  const put = api.putConnections;

  useEffect(() => {
    let live = true;
    get(specialistId).then(
      (r) => live && setServices(r.services || []),
      (e) => live && setError(errorText(e)),
    );
    return () => {
      live = false;
    };
  }, [specialistId, get]);

  const toggle = async (s: ServiceRow) => {
    setBusy(s.key);
    setError("");
    try {
      const r = await put(specialistId, s.allowed ? { remove: [s.key] } : { add: [s.key] });
      setServices(r.services || []);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="spx-tab">
      <p className="spx-muted spx-small">
        Allowed services run in its main chat without a card. Anything else asks every time. In a breakout, sending or
        changing anything in a service always asks. The nightly update only reads.
      </p>
      {error ? (
        <div className="spx-error" role="alert">
          {error}
        </div>
      ) : null}
      {services === null ? <div className="spx-muted">Loading…</div> : null}
      {services && !services.length ? (
        <div className="spx-muted">No services are connected. Connect one in Settings › Connections first.</div>
      ) : null}
      <ul className="spx-plain-list">
        {(services ?? []).map((s) => (
          <li key={s.key} className="spx-service-row">
            <label className="spx-check">
              <input
                type="checkbox"
                checked={s.allowed}
                disabled={busy !== null || (!s.connected && !s.allowed)}
                onChange={() => void toggle(s)}
              />
              <span className="spx-strong">{s.title}</span>
            </label>
            <span className="spx-muted spx-small">
              {s.kind === "app" ? "Local app" : "Connected service"}
              {s.connected ? "" : " · not signed in"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
