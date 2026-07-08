// Preferences page (SPEC §20) — the `/view/_prefs` sentinel route, entered
// from the sidebar's bottom-left gear. Four sections, each a thin client
// over an existing backend:
//   Logs             — where this process logs (GET /api/prefs) + reveal
//   Execution engine — the persisted /api/run engine pref (PUT /api/prefs);
//                      applies to the next run, no restart. Locked while
//                      FUSED_RENDER_ENGINE forces the process.
//   Deployments      — per-env `fused share list` with Revoke (deploy.py)
//   Template registry— the merged extension→templates bindings (read-only)
import { useEffect, useRef, useState } from "react";
import {
  getDeployConfig,
  getPrefs,
  getTemplateRegistry,
  listShares,
  putEnginePref,
  revealPath,
  revokeMount,
} from "../lib/api";
import type {
  DeployConfig,
  Prefs,
  RegistryResult,
  ShareMount,
} from "../lib/api";
import { basename } from "../lib/format";

function LogsSection({ prefs }: { prefs: Prefs }) {
  const [error, setError] = useState<string | null>(null);
  const reveal = async () => {
    setError(null);
    try {
      await revealPath(prefs.log.path);
    } catch (e) {
      // e.g. the file rotated away, or an unsupported platform.
      setError((e as Error).message);
    }
  };
  return (
    <section className="prefs-section">
      <h2>Logs</h2>
      <p className="deploy-muted">
        This server writes its log to <code>{prefs.log.path}</code> (a file per run; set{" "}
        <code>FUSED_RENDER_LOG_DIR</code> to keep logs somewhere persistent).
      </p>
      <button type="button" onClick={reveal}>
        Open logs location
      </button>
      {error && <div className="deploy-error">{error}</div>}
    </section>
  );
}

function EngineSection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const engine = prefs.engine;
  const locked = engine.forced_by !== null;

  const select = async (value: "builtin" | "fused") => {
    if (busy || value === engine.selected) return;
    setBusy(true);
    setError(null);
    try {
      onChange(await putEnginePref(value));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Execution engine</h2>
      <p className="deploy-muted">
        How <code>fused.runPython</code> runs a page's Python. <b>Both engines run on this
        machine</b> — neither uses your configured Fused environments (those are only deploy
        targets, chosen in a page's Deploy dialog). Changes apply to the next run — no restart.
      </p>
      <label className={"prefs-radio" + (locked ? " locked" : "")}>
        <input
          type="radio"
          name="engine"
          checked={engine.selected === "builtin"}
          disabled={locked || busy}
          onChange={() => select("builtin")}
        />
        <span>
          <b>Local (built-in)</b> — a fresh subprocess per call, in the environment that
          launched this server.
        </span>
      </label>
      <label
        className={"prefs-radio" + (locked || !engine.fused_available ? " locked" : "")}
        title={
          engine.fused_available
            ? undefined
            : "The fused package is not importable in the server's environment — install it from a page's Deploy dialog, or pip install \"fused-render[fused]\""
        }
      >
        <input
          type="radio"
          name="engine"
          checked={engine.selected === "fused"}
          disabled={locked || busy || !engine.fused_available}
          onChange={() => select("fused")}
        />
        <span>
          <b>Fused engine</b> — the fused package's local runner: PEP 723 inline requirements
          resolved into cached venvs (<code>~/.openfused/venvs</code>), plus{" "}
          <code>@fused.udf</code> / <code>result</code> entrypoints. Local too — no cloud, no
          environment.
          {!engine.fused_available && (
            <span className="deploy-muted"> (unavailable — the fused package isn't installed)</span>
          )}
        </span>
      </label>
      <div className="deploy-muted">
        Currently running: <b>{engine.effective === "fused" ? "Fused engine" : "Local (built-in)"}</b>
        {locked && (
          <>
            {" "}
            — locked by <code>FUSED_RENDER_ENGINE={engine.forced_by}</code> for this process; the
            switch applies once the variable is removed.
          </>
        )}
        {!locked && engine.selected === "fused" && engine.effective === "builtin" && (
          <> — falling back to Local while the fused package is unavailable.</>
        )}
      </div>
      {error && <div className="deploy-error">{error}</div>}
    </section>
  );
}

function DeploymentsSection() {
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
    <section className="prefs-section">
      <h2>Deployments</h2>
      <p className="deploy-muted">
        Everything <code>fused share list</code> reports on the chosen environment — from any
        app or machine. Rows with a file name were deployed from this app. Revoking takes the
        link down (a page can be deployed again from its Deploy dialog).
      </p>
      {config && envs.length === 0 && (
        <div className="deploy-muted">
          No hosted environments configured — see a page's Deploy dialog for setup.
        </div>
      )}
      {envs.length > 0 && (
        <div className="deploy-form-row">
          <label htmlFor="prefs-env-select">Environment</label>
          <select
            id="prefs-env-select"
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
    </section>
  );
}

function RegistrySection() {
  const [registry, setRegistry] = useState<RegistryResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getTemplateRegistry()
      .then((r) => alive && setRegistry(r))
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  const sourceLabel = (s: string) =>
    s === "builtin" ? "built-in" : s === "user-override" ? "user override" : "user";

  return (
    <section className="prefs-section">
      <h2>Template registry</h2>
      <p className="deploy-muted">
        Which templates open each file pattern (first entry is the default mode). Read-only
        here — add or override bindings in <code>{registry?.user_registry ?? "~/.fused-render/templates/registry.json"}</code>.
      </p>
      {error && <div className="deploy-error">{error}</div>}
      {registry?.error && <div className="deploy-error">{registry.error}</div>}
      {registry && (
        <table className="prefs-registry-table">
          <tbody>
            {registry.entries.map((e) => (
              <tr key={e.source + e.pattern} className={e.error ? "has-error" : undefined}>
                <td className="registry-pattern">
                  <code>{e.pattern}</code>
                </td>
                <td className="registry-templates">
                  {e.disabled ? (
                    <span className="deploy-muted">disabled (no preview)</span>
                  ) : (
                    e.templates.map((t, i) => (
                      <code key={t + i} className={i === 0 ? "default-mode" : undefined} title={i === 0 ? "default mode" : undefined}>
                        {t}
                      </code>
                    ))
                  )}
                  {e.error && <span className="registry-error"> {e.error}</span>}
                </td>
                <td>
                  <span className={"registry-source " + e.source}>{sourceLabel(e.source)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export default function Preferences() {
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getPrefs()
      .then((p) => alive && setPrefs(p))
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="prefs-page">
      {error && <div className="deploy-error">{error}</div>}
      {!prefs && !error && <div className="deploy-muted">Loading…</div>}
      {prefs && (
        <>
          <EngineSection prefs={prefs} onChange={setPrefs} />
          <LogsSection prefs={prefs} />
          <DeploymentsSection />
          <RegistrySection />
        </>
      )}
    </div>
  );
}
