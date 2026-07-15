// Fused account page — the `/view/_account` sentinel route, entered from the
// sidebar footer (docs/PLAN-fused-account.md M18a). The in-app home of the
// one-time `fused cloud login` the Deploy surface needs, plus sign-out — no
// more copying CLI commands into a terminal. Everything real happens
// server-side (fused_render/account.py drives the fused CLI; credentials
// live in the CLI's own store, never in this app).
//
// States, in checking order (mirrors DeployModal's):
//   1. loading      — status fetch in flight
//   2. CLI missing  — install panel (one-click when the server can pip
//                     install the pinned [fused] extra, else the manual hint)
//   3. signed out   — Sign in (spawns the CLI's browser flow; lib/account.ts)
//   4. signed in    — account summary (orgs/roles via the ?probe=1 deep
//                     check), hosted environments (read-only — in-app env
//                     setup lands with M18b), Sign out
import { useEffect, useRef, useState } from "react";
import { accountLogout, cancelAccountLogin, getAccountStatus, installFused } from "../lib/api";
import type { AccountStatus } from "../lib/api";
import { useFusedLogin } from "../lib/account";

export default function Account() {
  const [status, setStatus] = useState<AccountStatus | null>(null);
  // The deep check (`fused cloud orgs`) can take seconds — the page renders
  // from the fast presence-only status first, then fills the probe in.
  const [probing, setProbing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"install" | "logout" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  // Sequence-guarded like DeployModal's load: a superseded fetch (focus
  // refresh racing a slow probe) must not land its stale response over a
  // newer one.
  const loadSeq = useRef(0);
  const load = async (background = false) => {
    const seq = ++loadSeq.current;
    if (!background) setLoadError(null);
    try {
      const fast = await getAccountStatus();
      if (!alive.current || seq !== loadSeq.current) return;
      setStatus(fast);
      setLoadError(null);
      if (fast.logged_in && fast.cli.found) {
        setProbing(true);
        try {
          const full = await getAccountStatus(true);
          if (!alive.current || seq !== loadSeq.current) return;
          setStatus(full);
        } catch {
          // The fast status is already on screen; a failed probe just leaves
          // the summary without org detail.
        } finally {
          if (alive.current) setProbing(false);
        }
      }
    } catch (e) {
      if (!alive.current || seq !== loadSeq.current || background) return;
      setLoadError((e as Error).message);
    }
  };
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const signin = useFusedLogin(() => void load(true));

  // Freshness on return: re-read when the tab regains focus/visibility (the
  // deploy-dot pattern) — this is also what flips the page after the sign-in
  // round-trip lands in ANOTHER tab and the user comes back to this one.
  const loadRef = useRef(load);
  loadRef.current = load;
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") void loadRef.current(true);
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  // A sign-in can be in flight without this page having started it (the
  // Deploy modal, or another tab): poll until it resolves either way. The
  // hook's own polling covers the sign-in started HERE (signin.connecting).
  const inFlightElsewhere =
    status !== null && status.login_in_flight && !status.logged_in && !signin.connecting;
  useEffect(() => {
    if (!inFlightElsewhere) return;
    const id = window.setInterval(() => void loadRef.current(true), 2000);
    return () => window.clearInterval(id);
  }, [inFlightElsewhere]);

  const onInstall = async () => {
    setBusy("install");
    setActionError(null);
    try {
      await installFused();
      if (!alive.current) return;
      await load(); // re-probe: the CLI should now be found
    } catch (e) {
      if (alive.current) setActionError((e as Error).message);
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const onLogout = async () => {
    setBusy("logout");
    setActionError(null);
    try {
      const fresh = await accountLogout();
      if (!alive.current) return;
      setStatus(fresh);
    } catch (e) {
      if (alive.current) {
        setActionError((e as Error).message);
        void load(true); // pull the true post-action state
      }
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const onCancelElsewhere = async () => {
    setActionError(null);
    try {
      await cancelAccountLogin();
    } catch (e) {
      if (alive.current) setActionError((e as Error).message);
    }
    void load(true);
  };

  const body = () => {
    if (loadError) {
      return (
        <section className="prefs-section">
          <h2>Fused account</h2>
          <div className="deploy-error">{loadError}</div>
          <button type="button" onClick={() => void load()}>
            Retry
          </button>
        </section>
      );
    }
    if (!status) {
      return (
        <section className="prefs-section">
          <h2>Fused account</h2>
          <div className="deploy-muted">Loading…</div>
        </section>
      );
    }

    if (!status.cli.found) {
      return (
        <section className="prefs-section">
          <h2>Fused account</h2>
          <p>
            Signing in and deploying use the <code>fused</code> CLI, which is not installed
            in the server's Python environment.
          </p>
          {status.cli.installable ? (
            <button
              type="button"
              className="deploy-primary"
              onClick={onInstall}
              disabled={busy !== null}
            >
              {busy === "install" ? "Installing fused…" : "Install fused into this environment"}
            </button>
          ) : (
            <p className="deploy-muted">
              {status.cli.reason ?? "It cannot be installed automatically."} Install it
              manually: <code>{status.cli.install_hint}</code>
            </p>
          )}
          {actionError && <div className="deploy-error">{actionError}</div>}
        </section>
      );
    }

    if (!status.logged_in) {
      return (
        <section className="prefs-section">
          <h2>Fused account</h2>
          <p className="deploy-muted">
            Deploying pages to a hosted URL publishes through your Fused account. Signing in
            is a one-time browser round-trip; credentials are stored by the fused CLI on
            this machine — never by fused-render.
          </p>
          {signin.connecting ? (
            <div className="deploy-form-row">
              <span className="deploy-muted">
                Waiting for the browser sign-in… finish signing in in the tab that just
                opened.
              </span>
              <button type="button" onClick={() => void signin.cancel()}>
                Cancel
              </button>
            </div>
          ) : inFlightElsewhere ? (
            <div className="deploy-form-row">
              <span className="deploy-muted">
                A browser sign-in is already in progress (started from another page or tab).
              </span>
              <button type="button" onClick={() => void onCancelElsewhere()}>
                Cancel it
              </button>
            </div>
          ) : (
            <button type="button" className="deploy-primary" onClick={() => void signin.begin()}>
              Sign in to Fused
            </button>
          )}
          {signin.error && <div className="deploy-error">{signin.error}</div>}
          {actionError && <div className="deploy-error">{actionError}</div>}
        </section>
      );
    }

    const probe = status.probe;
    return (
      <>
        <section className="prefs-section">
          <h2>Fused account</h2>
          <p>
            Signed in to Fused.
            {probing && <span className="deploy-muted"> Checking the account…</span>}
          </p>
          {probe && !probe.ok && (
            <div className="deploy-note">
              Couldn't verify the account with the Fused control plane: {probe.error}
            </div>
          )}
          {probe && probe.ok && probe.admitted === false && (
            <div className="deploy-note">
              This account isn't admitted to Fused yet — deploys to a managed environment
              will fail until it is.
            </div>
          )}
          {probe && probe.ok && probe.orgs.length > 0 && (
            <table className="deploy-shares-table">
              <thead>
                <tr>
                  <th>Organization</th>
                  <th>Environment</th>
                  <th>State</th>
                  <th>Role</th>
                </tr>
              </thead>
              <tbody>
                {probe.orgs.map((o, i) => (
                  <tr key={i}>
                    <td>{o.org ?? "—"}</td>
                    <td>{o.env ?? "—"}</td>
                    <td>{o.provision_state ?? "—"}</td>
                    <td>{o.role ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="deploy-form-row">
            <button
              type="button"
              className="deploy-danger"
              onClick={onLogout}
              disabled={busy !== null}
              title="Removes the fused CLI's stored sign-in on this machine"
            >
              {busy === "logout" ? "Signing out…" : "Sign out"}
            </button>
          </div>
          {actionError && <div className="deploy-error">{actionError}</div>}
        </section>
        <section className="prefs-section">
          <h2>Hosted environments</h2>
          {status.envs.length === 0 ? (
            <p className="deploy-muted">
              No hosted environments yet. In-app environment setup is coming; for now, create
              the managed one in a terminal with <code>{status.setup_cli} cloud setup</code>{" "}
              (environments are read from <code>{status.envs_file}</code>).
            </p>
          ) : (
            <>
              <p className="deploy-muted">
                Deploy targets from the fused CLI's environment store (
                <code>{status.envs_file}</code>).
              </p>
              <table className="deploy-shares-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Backend</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {status.envs.map((e) => (
                    <tr key={e.name}>
                      <td>{e.name}</td>
                      <td>{e.backend === "fused" ? "fused — managed" : e.backend}</td>
                      <td className="deploy-muted">
                        {e.name === status.default_env ? "default" : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </section>
      </>
    );
  };

  return <div className="prefs-page">{body()}</div>;
}
