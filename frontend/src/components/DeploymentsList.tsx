// The env-wide deployments list (SPEC DP-13 / AC-11): everything
// `fused share list` reports on a chosen hosted environment, joined back to
// the local pages that deployed it, with per-mount Revoke. Lives on the Fused
// account page beside the environments table (moved from Preferences, where
// it predated the account surface — Preferences keeps only the Deploy-button
// toggle). Works signed-out too: an AWS env's share list needs AWS
// credentials, not a managed-Fused sign-in.
import { useEffect, useRef, useState } from "react";
import { getDeployConfig, listShares, revokeMount } from "../lib/api";
import type { DeployConfig, ShareMount } from "../lib/api";
import { basename } from "../lib/format";

export default function DeploymentsList() {
  const [config, setConfig] = useState<DeployConfig | null>(null);
  const [env, setEnv] = useState<string | null>(null);
  const [mounts, setMounts] = useState<ShareMount[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);
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
      {error && <div className="deploy-error">{error}</div>}
      {mounts && mounts.length === 0 && (
        <div className="deploy-muted">Nothing is deployed on this environment.</div>
      )}
      {mounts && mounts.length > 0 && (
        <table className="deploy-shares-table">
          <tbody>
            {mounts.map((m) => (
              <tr key={m.token}>
                <td className="share-page" title={m.page ?? "Deployed by the CLI, another app, or another machine"}>
                  {m.page ? basename(m.page) : <span className="deploy-muted">not from this app</span>}
                </td>
                <td className="share-token" title={m.token}>
                  {m.token}
                </td>
                <td>
                  <span className={"share-status " + m.status}>{m.status}</span>
                </td>
                <td>
                  {m.url ? (
                    <a href={m.url} target="_blank" rel="noreferrer">
                      Open ↗
                    </a>
                  ) : (
                    <span
                      className="deploy-muted"
                      title="`fused share list` doesn't report URLs; a link shows once this app has recorded (or can derive) one for this environment"
                    >
                      —
                    </span>
                  )}
                </td>
                <td>
                  {m.status !== "revoked" && (
                    <button
                      type="button"
                      className="deploy-danger"
                      onClick={() => onRevoke(m.token)}
                      disabled={revoking !== null || loading}
                      title="Take this link down"
                    >
                      {revoking === m.token ? "Revoking…" : "Revoke"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
