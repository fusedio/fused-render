// Settings › Connections (PRODUCT §7): which connected services and local app
// tools this specialist may use without asking. Same meaning as a bot's
// connections; the sign-in itself is the app's one global sign-in, so a row
// that is not signed in is drawn off, with a "Sign in" link to that page.
import { useEffect, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { errorText, getConnections, putConnections, type ServiceRow } from "@platform/lib/specialists-api";
import { CONNECTIONS_HREF } from "../lib/api-extra";

/** The service's name for a reader: its title, or its key made readable. */
export function serviceTitle(s: Pick<ServiceRow, "key" | "title">): string {
  const t = (s.title || "").trim();
  if (t && t !== s.key) return t;
  const words = s.key
    .replace(/^[a-z]+:/i, "")
    .replace(/[_-]+/g, " ")
    .trim();
  return words ? words[0].toUpperCase() + words.slice(1) : s.key;
}

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

  const anyOff = (services ?? []).some((s) => !s.connected);
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
      {services === null && !error ? <div className="spx-muted">Loading…</div> : null}
      {services && !services.length ? (
        <div className="spx-empty-note">
          No services are connected yet.{" "}
          <a
            href={CONNECTIONS_HREF}
            className="spx-link"
            onClick={(e) => {
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
              e.preventDefault();
              void navigateUrl(CONNECTIONS_HREF);
            }}
          >
            Connect one in Settings
          </a>
          .
        </div>
      ) : null}
      <ul className="spx-plain-list spx-services">
        {(services ?? []).map((s) => {
          const off = !s.connected && !s.allowed;
          const id = `spx-conn-${specialistId}-${s.key}`;
          return (
            <li key={s.key} className={"spx-service-row" + (!s.connected ? " is-off" : "")}>
              <label className="spx-check" htmlFor={id}>
                <input
                  id={id}
                  type="checkbox"
                  checked={s.allowed}
                  disabled={busy !== null || off}
                  onChange={() => void toggle(s)}
                />
                <span className="spx-strong">{serviceTitle(s)}</span>
              </label>
              <span className="spx-service-meta spx-small">
                <span className="spx-muted">{s.kind === "app" ? "Local app" : "Connected service"}</span>
                {s.connected ? null : (
                  <>
                    <span className="spx-muted"> · not signed in · </span>
                    <a
                      href={CONNECTIONS_HREF}
                      className="spx-link"
                      aria-label={`Sign in to ${serviceTitle(s)}`}
                      onClick={(e) => {
                        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                        e.preventDefault();
                        void navigateUrl(CONNECTIONS_HREF);
                      }}
                    >
                      Sign in
                    </a>
                  </>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      {anyOff ? (
        <p className="spx-muted spx-small">Sign in once in the app's Connections settings; then allow the service here.</p>
      ) : null}
    </div>
  );
}
