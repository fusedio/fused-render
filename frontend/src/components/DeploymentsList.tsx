// The env-wide deployments list (SPEC DP-13 / AC-11): everything
// `fused share list` reports on a chosen hosted environment, joined back to
// the local pages that deployed it, with per-mount Revoke. Lives on the Fused
// account tab beside the environments table (moved from Preferences, where
// it predated the account surface — Preferences keeps only the "Deploy to
// Fused account" toggle). Works signed-out too for an AWS env, which only
// needs AWS credentials, not a managed-Fused sign-in — a `fused`-backend
// env's list does need that sign-in, so being signed out there surfaces as
// an expected quiet note rather than an error (see the `error` render below).
import { Fragment, useEffect, useRef, useState } from "react";
import { getDeployConfig, listShares, revokeMount } from "../lib/api";
import type { DeployConfig, ShareMount } from "../lib/api";
import { basename } from "../lib/format";
import DeploymentErrors from "./DeploymentErrors";
import RowActionsMenu from "./RowActionsMenu";
import type { MenuEntry } from "./ContextMenu";

export default function DeploymentsList() {
  const [config, setConfig] = useState<DeployConfig | null>(null);
  const [env, setEnv] = useState<string | null>(null);
  const [mounts, setMounts] = useState<ShareMount[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);
  // Which row's recent-errors panel is expanded (by token). Lazy: the panel
  // (and its `share errors` CLI call) only mounts when a row is opened, so
  // listing an env with many mounts doesn't fan out one subprocess per row.
  const [openErrors, setOpenErrors] = useState<string | null>(null);
  // Supersession guard, same discipline as DeployModal's load.
  const loadSeq = useRef(0);

  useEffect(() => {
    let alive = true;
    getDeployConfig()
      .then((cfg) => {
        if (!alive) return;
        setConfig(cfg);
        setEnv((prev) => prev ?? cfg.default_env);
      })
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  const load = async (target: string) => {
    const seq = ++loadSeq.current;
    setLoading(true);
    setError(null);
    try {
      const res = await listShares(target);
      if (seq !== loadSeq.current) return;
      setMounts(res.mounts);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError((e as Error).message);
      setMounts(null);
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  };

  useEffect(() => {
    // Collapse any open recent-errors panel when the environment changes: the
    // expanded token belongs to the previous env, so leaving it open would
    // silently re-mount the panel (and re-run its `share errors` call) for a
    // mount the user never chose to inspect on the new env.
    setOpenErrors(null);
    if (env !== null) void load(env);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [env]);

  const onRevoke = async (token: string) => {
    if (env === null) return;
    setRevoking(token);
    setError(null);
    try {
      await revokeMount(env, token);
      await load(env);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRevoking(null);
    }
  };

  const envs = config?.envs ?? [];
  return (
    <>
      <p className="deploy-muted">
        Everything <code>fused share list</code> reports on the chosen environment — from any
        app or machine. Rows with a file name were deployed from this app. Revoking takes the
        link down (a page can be deployed again from its Deploy dialog).
      </p>
      {config && envs.length === 0 && (
        <div className="deploy-muted">
          No hosted environments configured yet — nothing can be deployed until one exists.
        </div>
      )}
      {envs.length > 0 && (
        <div className="deploy-form-row">
          <label htmlFor="account-shares-env-select">Environment</label>
          <select
            id="account-shares-env-select"
            value={env ?? ""}
            onChange={(e) => setEnv(e.target.value)}
            disabled={loading || revoking !== null}
          >
            {envs.map((e) => (
              <option key={e.name} value={e.name}>
                {e.name} ({e.backend === "fused" ? "fused — managed" : e.backend})
              </option>
            ))}
          </select>
          <button type="button" onClick={() => env !== null && load(env)} disabled={loading}>
            Refresh
          </button>
        </div>
      )}
      {loading && <div className="deploy-muted">Loading share list…</div>}
      {error &&
        // A `fused`-backend env's share list needs the managed-Fused sign-in
        // (unlike an AWS env's, which only needs AWS credentials — see the
        // module comment); being signed out is an expected, not-yet-set-up
        // state here, not a failure, so it gets the quiet note treatment
        // rather than the red error card.
        (/not logged in/i.test(error) ? (
          <div className="deploy-note">{error}</div>
        ) : (
          <div className="deploy-error">{error}</div>
        ))}
      {mounts && mounts.length === 0 && (
        <div className="deploy-muted">Nothing is deployed on this environment.</div>
      )}
      {mounts && mounts.length > 0 && (
        <table className="deploy-shares-table">
          <tbody>
            {mounts.map((m) => {
              // One "⋯" per row instead of a separate Open link and Revoke
              // button: Open/Copy first, the destructive Revoke tucked behind a
              // separator. A revoked row with no URL has no entries, so the menu
              // renders a muted "—" (nothing to do).
              const url = m.url;
              const items: MenuEntry[] = [];
              if (url) {
                items.push({
                  label: "Open ↗",
                  onClick: () => window.open(url, "_blank", "noopener,noreferrer"),
                });
                items.push({
                  label: "Copy link",
                  onClick: () => {
                    void navigator.clipboard?.writeText(url);
                  },
                });
              } else {
                // Preserves the old "—" tooltip's explanation, now inside the menu.
                items.push({ label: "No link reported yet", disabled: true });
              }
              // Recent errors: owner-only diagnostics for the mount, expanded in
              // a panel below the row (loaded lazily when opened).
              items.push("separator");
              items.push({
                label: openErrors === m.token ? "Hide recent errors" : "Recent errors",
                onClick: () =>
                  setOpenErrors((cur) => (cur === m.token ? null : m.token)),
              });
              if (m.status !== "revoked") {
                items.push("separator");
                items.push({
                  label: "Revoke",
                  danger: true,
                  // Gate ONLY the destructive action while the list reloads or
                  // another row is being revoked — Open ↗ / Copy link are
                  // read-only and stay available (they were always clickable
                  // before this moved into the menu).
                  disabled: revoking !== null || loading,
                  onClick: () => void onRevoke(m.token),
                });
              }
              const rowLabel = m.page ? basename(m.page) : m.token;
              return (
                <Fragment key={m.token}>
                  <tr>
                    <td className="share-page" title={m.page ?? "Deployed by the CLI, another app, or another machine"}>
                      {m.page ? basename(m.page) : <span className="deploy-muted">not from this app</span>}
                    </td>
                    <td className="share-token" title={m.token}>
                      {m.token}
                    </td>
                    <td>
                      <span className={"share-status " + m.status}>{m.status}</span>
                    </td>
                    <td className="row-actions-cell">
                      {revoking === m.token ? (
                        <span className="deploy-muted">Revoking…</span>
                      ) : (
                        <RowActionsMenu items={items} label={`Actions for ${rowLabel}`} />
                      )}
                    </td>
                  </tr>
                  {openErrors === m.token && env && (
                    <tr className="deploy-errors-row">
                      <td colSpan={4}>
                        <DeploymentErrors env={env} token={m.token} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}
