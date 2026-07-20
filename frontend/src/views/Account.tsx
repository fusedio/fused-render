// Fused account page — the `/view/_account` sentinel route, entered from the
// sidebar footer (SPEC §27, AC-1). The in-app home of the
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
//                     check), environment management (set default / forget the
//                     local pointer), the managed-env setup panel (M18b: the
//                     one-shot `fused cloud setup` as a polled job), Sign out
import { useEffect, useRef, useState } from "react";
import {
  accountLogout,
  cancelAccountLogin,
  deleteStoreEnv,
  getAccountSetup,
  getAccountStatus,
  installFused,
  setDefaultEnv,
  startAccountSetup,
} from "../lib/api";
import type { AccountSetupStatus, AccountStatus } from "../lib/api";
import { notifyAccountChanged, useFusedLogin } from "../lib/account";
import { useRefreshOnReturn } from "../lib/hooks";
import DeploymentsList from "../components/DeploymentsList";
import RowActionsMenu from "../components/RowActionsMenu";
import type { MenuEntry } from "../components/ContextMenu";

// The managed-env setup panel: pick the workspace (when the account has more
// than one), name the env, run `fused cloud setup` as a tracked server job,
// and stream the CLI's own progress lines while it provisions. The panel also
// ADOPTS a job already running server-side (page reopened mid-setup) so the
// progress view survives navigation.
function SetupPanel({
  status,
  probing,
  onChanged,
}: {
  status: AccountStatus;
  // The deep org probe is still in flight — the panel must not offer the
  // action yet: with probe null the workspace list is unknown, and a fast
  // click would run setup without --org/--env even for an account that has
  // several workspaces to choose from.
  probing: boolean;
  onChanged: () => void;
}) {
  const probe = status.probe;
  const orgs = probe?.ok ? probe.orgs.filter((o) => o.org && o.env) : [];
  const [pick, setPick] = useState(0);
  const [nameOverride, setNameOverride] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [job, setJob] = useState<{ job_id: string; env_name: string } | null>(null);
  const [progress, setProgress] = useState<AccountSetupStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [doneName, setDoneName] = useState<string | null>(null);
  // The local name is a nickname for this machine's env store, not something
  // the user must know — so it stays hidden behind "Edit name" and the common
  // path (import the discovered env under its default name) needs zero typing.
  const [editingName, setEditingName] = useState(false);

  const chosen = orgs.length > 0 ? orgs[Math.min(pick, orgs.length - 1)] : null;
  // Mirror the server's default-name rule (flow's convention) so the field
  // shows what an untouched setup will create; the override wins when edited.
  const derived = !chosen || chosen.env === "default" ? "fused" : `fused-${chosen.env}`;
  const envName = nameOverride ?? derived;

  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;

  // Adopt a server-side job already in flight (one job at a time, so it is
  // unambiguously "the" setup regardless of which page started it).
  useEffect(() => {
    let alive = true;
    getAccountSetup().then(
      (s) => {
        if (alive && s.state === "running" && s.job_id && s.env_name) {
          setJob({ job_id: s.job_id, env_name: s.env_name });
        }
      },
      () => {}
    );
    return () => {
      alive = false;
    };
  }, []);

  // Poll the running job (flow's cadence). job_id matching keeps a stale
  // job's terminal state from completing a newer attempt.
  useEffect(() => {
    if (!job) return;
    const id = window.setInterval(async () => {
      let s: AccountSetupStatus;
      try {
        s = await getAccountSetup();
      } catch {
        return; // transient — keep polling
      }
      if (s.job_id !== job.job_id) return;
      setProgress(s);
      if (s.state === "done") {
        setJob(null);
        setDoneName(job.env_name);
        onChangedRef.current();
      } else if (s.state === "failed") {
        setJob(null);
        setError(s.detail ?? "environment setup failed");
      }
    }, 1500);
    return () => window.clearInterval(id);
  }, [job]);

  const begin = async () => {
    setError(null);
    setDoneName(null);
    setStarting(true);
    try {
      const started = await startAccountSetup(
        chosen
          ? { org: chosen.org!, env: chosen.env!, env_name: envName }
          : { env_name: envName }
      );
      setProgress(null);
      setJob(started);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  };

  if (probe && probe.ok && probe.admitted === false) {
    return (
      <p className="deploy-muted">
        Environment setup needs an admitted account — this account isn't admitted to Fused
        yet.
      </p>
    );
  }

  if (job) {
    return (
      <>
        <div className="deploy-form-row">
          <span className="deploy-spinner" />
          <span className="deploy-muted">
            Setting up <code>{job.env_name}</code>… this provisions the managed environment
            and can take a few minutes.
          </span>
        </div>
        {progress?.detail && <pre className="account-setup-log">{progress.detail}</pre>}
      </>
    );
  }

  if (probing && !probe) {
    // Workspace list still unknown — hold the action. Offering it now would
    // let a click run setup un-targeted before the picker can even render.
    return (
      <div className="deploy-form-row">
        <span className="deploy-spinner" />
        <span className="deploy-muted">Checking your Fused account for existing environments…</span>
      </div>
    );
  }

  // An account that already has a workspace doesn't get anything "created" —
  // `cloud setup --org --env` CONNECTS the existing environment (mints its
  // access key, registers it locally). Lead with WHICH environment that is
  // (discovered from the server via `cloud orgs`) and make importing it one
  // click; the local name is a nickname with a working default, so it hides
  // behind "Edit name" rather than reading as required knowledge.
  const hasWorkspace = chosen !== null;
  return (
    <>
      <p className="deploy-muted">
        {hasWorkspace
          ? "Your account already has a hosted environment. Connecting imports it to this " +
            "machine — it stores the environment's access key with the fused CLI and " +
            "registers it as a deploy target. Nothing new is created."
          : "One-time setup: creates the managed Fused environment, stores its access key " +
            "with the fused CLI, and registers it as a deploy target."}
        {orgs.length === 0 &&
          probe?.ok &&
          " No workspace was found for this account, so setting up creates your personal one."}
        {!probing &&
          (!probe || !probe.ok) &&
          " (Workspace discovery failed — setup will discover it itself.)"}
      </p>
      {/* The discovered environment(s): a picker when the account can target
          more than one, else the single one shown read-only. Either way the
          user never types the org/env — it comes from the server. */}
      {orgs.length > 1 ? (
        <div className="deploy-form-row">
          <label htmlFor="account-workspace-select" className="deploy-muted">
            Environment
          </label>
          <select
            id="account-workspace-select"
            value={pick}
            onChange={(e) => {
              setPick(Number(e.target.value));
              setNameOverride(null); // re-derive the name for the new workspace
            }}
          >
            {orgs.map((o, i) => (
              <option key={i} value={i}>
                {o.org} / {o.env}
                {o.provision_state && o.provision_state !== "ready"
                  ? ` (${o.provision_state})`
                  : ""}
              </option>
            ))}
          </select>
        </div>
      ) : chosen ? (
        <div className="deploy-form-row">
          <span className="deploy-muted">Environment</span>
          <code>
            {chosen.org} / {chosen.env}
          </code>
          {chosen.provision_state && chosen.provision_state !== "ready" && (
            <span className="deploy-muted">({chosen.provision_state})</span>
          )}
        </div>
      ) : null}
      {/* Local nickname — demoted: a plain line + "Edit name", so the default
          import path needs no typing. Shown up front only when there's no
          workspace to import (a create, where naming is the point). */}
      <div className="deploy-form-row">
        {editingName || !hasWorkspace ? (
          <>
            <label htmlFor="account-env-name" className="deploy-muted">
              Local name
            </label>
            <input
              id="account-env-name"
              type="text"
              value={envName}
              onChange={(e) => setNameOverride(e.target.value)}
              size={14}
            />
          </>
        ) : (
          <span className="deploy-muted">
            Saved on this machine as <code>{envName}</code>.{" "}
            <button
              type="button"
              className="link-button"
              onClick={() => setEditingName(true)}
            >
              Edit name
            </button>
          </span>
        )}
      </div>
      <div className="deploy-form-row">
        <button type="button" className="btn btn-primary" onClick={begin} disabled={starting}>
          {starting
            ? "Starting…"
            : hasWorkspace && chosen
              ? `Connect ${chosen.org} / ${chosen.env}`
              : "Set up hosted environment"}
        </button>
      </div>
      {doneName && (
        <div className="deploy-note">
          Environment <b>{doneName}</b> is ready — pages can now deploy to it from the
          preview header's Deploy button.
        </div>
      )}
      {error && (
        <div className="deploy-error">
          {error}
          {/* The one setup failure with a known local remedy: macOS denies the
              key write when the "openfused" keychain item was created by a
              DIFFERENT fused install (or a previous ad-hoc-signed app build). */}
          {/keychain/i.test(error) && (
            <div className="deploy-muted">
              This usually means the key was first stored by a different fused install.
              Open Keychain Access, search for “openfused”, and either delete the item
              (connecting again recreates it) or set its Access Control to allow this
              app — then retry.
            </div>
          )}
        </div>
      )}
    </>
  );
}

export default function Account() {
  const [status, setStatus] = useState<AccountStatus | null>(null);
  // The deep check (`fused cloud orgs`) can take seconds — the page renders
  // from the fast presence-only status first, then fills the probe in.
  const [probing, setProbing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"install" | "logout" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // Env-row action in flight ("default:NAME" / "delete:NAME") — disables the
  // row buttons without blocking the rest of the page.
  const [envBusy, setEnvBusy] = useState<string | null>(null);
  const [envError, setEnvError] = useState<string | null>(null);
  // The setup panel is prominent while no managed env exists; afterwards it
  // collapses behind an "Add managed environment" toggle.
  const [showSetup, setShowSetup] = useState(false);

  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  // Sequence-guarded like DeployModal's load: a superseded fetch (focus
  // refresh racing a slow probe) must not land its stale response over a
  // newer one.
  const loadSeq = useRef(0);
  // Latest status for load()'s probe-caching decision (state reads inside an
  // async fn would be stale), and the last-seen logged_in for flip detection.
  const statusRef = useRef<AccountStatus | null>(null);
  statusRef.current = status;
  const wasLoggedIn = useRef<boolean | null>(null);
  const load = async (background = false, reprobe = false) => {
    const seq = ++loadSeq.current;
    if (!background) setLoadError(null);
    try {
      const fast = await getAccountStatus();
      if (!alive.current || seq !== loadSeq.current) return;
      // The deep probe (`fused cloud orgs`) is a control-plane call — don't
      // re-issue it on every focus/visibility refresh. A background refresh
      // keeps the orgs view it already has; the probe re-runs only when
      // missing (initial load, or right after a sign-in) or when the caller
      // forces it (reprobe — e.g. setup may have created a workspace). The
      // cache is valid only while logged_in held CONTINUOUSLY *and* the
      // credentials fingerprint is unchanged: a sign-out (even one observed
      // mid-refresh) OR a credential swap — a re-login as a different account
      // that never flipped logged_in false here — must drop it, or the
      // summary and workspace picker would show the previous account's orgs.
      const prev = statusRef.current;
      const sameCreds = prev != null && prev.creds_stamp === fast.creds_stamp;
      const keptProbe =
        background && !reprobe && fast.logged_in && prev?.logged_in && sameCreds
          ? (prev.probe ?? null)
          : null;
      setStatus(keptProbe ? { ...fast, probe: keptProbe } : fast);
      setLoadError(null);
      // Same-tab sidebar dot: announce sign-in state flips observed here —
      // a login completed from another page/tab has no other channel to it.
      if (wasLoggedIn.current !== null && wasLoggedIn.current !== fast.logged_in) {
        notifyAccountChanged();
      }
      wasLoggedIn.current = fast.logged_in;
      if (fast.logged_in && fast.cli.found && !keptProbe) {
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

  const signin = useFusedLogin((fresh) => {
    // Apply the status the poll already fetched — flipping to the signed-in
    // layout must not depend on one more fetch that could fail. The
    // background load then fills the probe in (fresh carries probe: null,
    // so load's cache check re-probes).
    setStatus(fresh);
    wasLoggedIn.current = fresh.logged_in;
    void load(true);
  });

  // Freshness on return: re-read when the tab regains focus/visibility (the
  // deploy-dot pattern) — this is also what flips the page after the sign-in
  // round-trip lands in ANOTHER tab and the user comes back to this one.
  const loadRef = useRef(load);
  loadRef.current = load;
  useRefreshOnReturn(() => void loadRef.current(true));

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
      // Invalidate any in-flight background load: one started BEFORE the
      // sign-out could resolve after this and resurrect the signed-in view
      // (stale logged_in + cached probe) over the logout.
      loadSeq.current++;
      setStatus(fresh);
      wasLoggedIn.current = fresh.logged_in; // keep the flip detector honest
      notifyAccountChanged(); // the sidebar dot must drop without a refocus
    } catch (e) {
      if (alive.current) {
        setActionError((e as Error).message);
        void load(true); // pull the true post-action state
      }
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const runEnvAction = async (key: string, action: () => Promise<AccountStatus>) => {
    setEnvBusy(key);
    setEnvError(null);
    try {
      const fresh = await action();
      // The env endpoints answer probe-less — keep the orgs view we already
      // have (make-default/forget don't change org membership), or the
      // signed-in summary and workspace picker would vanish on every click.
      // Bump the load sequence too: an in-flight pre-action load must not
      // land its stale store over the post-action one.
      if (alive.current) {
        loadSeq.current++;
        setStatus((prev) => (prev?.probe ? { ...fresh, probe: prev.probe } : fresh));
      }
    } catch (e) {
      if (alive.current) setEnvError((e as Error).message);
    } finally {
      if (alive.current) setEnvBusy(null);
    }
  };

  const onMakeDefault = (name: string) =>
    runEnvAction("default:" + name, () => setDefaultEnv(name));

  const onDeleteEnv = (name: string) => {
    // Local-pointer semantics, stated up front: nothing in the cloud is
    // touched, so this is reversible by re-running setup / env create.
    if (
      !window.confirm(
        `Forget environment “${name}”? This only removes the local entry from the fused ` +
          `CLI's environment store — cloud resources and stored keys are not touched.`
      )
    ) {
      return;
    }
    void runEnvAction("delete:" + name, () => deleteStoreEnv(name));
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
              className="btn btn-primary"
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

    // Environments and Deployments render in BOTH auth states: managing the
    // env store and an AWS env's share list need the CLI, not a managed-Fused
    // sign-in — an AWS-only user must not be forced through one to revoke a
    // link (SPEC AC-11). Only the managed-specific sections (account summary,
    // setup panel) gate on the sign-in.
    const envsSection = (
      <section className="prefs-section">
          <h2>Environments</h2>
          {status.store.envs.length === 0 ? (
            <p className="deploy-muted">
              The fused CLI's environment store (<code>{status.envs_file}</code>) is empty
              {status.logged_in
                ? " — connect or set up the managed environment below."
                : " — sign in to connect or set up the managed environment."}
            </p>
          ) : (
            <>
              <p className="deploy-muted">
                From the fused CLI's environment store (<code>{status.envs_file}</code>).
                Hosted environments are deploy targets; “Forget” removes only the local
                entry.
              </p>
              <table className="deploy-shares-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Backend</th>
                    <th></th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {status.store.envs.map((e) => {
                    // Collapse Make default + Forget into one "⋯" per row —
                    // fewer buttons on screen, destructive Forget behind an
                    // intentional click (it still shows its own confirm).
                    const isDefault = e.name === status.store.default;
                    const items: MenuEntry[] = [];
                    if (!isDefault) {
                      items.push({
                        label: "Make default",
                        onClick: () => void onMakeDefault(e.name),
                      });
                    }
                    items.push({
                      label: "Forget…",
                      danger: true,
                      onClick: () => onDeleteEnv(e.name),
                    });
                    return (
                      <tr key={e.name}>
                        <td>{e.name}</td>
                        <td>
                          {e.backend === "fused" ? "fused — managed" : e.backend}
                          {!e.hosted && <span className="deploy-muted"> (not a deploy target)</span>}
                        </td>
                        <td className="deploy-muted">{isDefault ? "default" : ""}</td>
                        <td className="row-actions-cell">
                          {envBusy === "default:" + e.name ? (
                            <span className="deploy-muted">Setting…</span>
                          ) : envBusy === "delete:" + e.name ? (
                            <span className="deploy-muted">Forgetting…</span>
                          ) : (
                            <RowActionsMenu
                              items={items}
                              disabled={envBusy !== null}
                              label={`Actions for ${e.name}`}
                            />
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </>
          )}
          {envError && <div className="deploy-error">{envError}</div>}
        </section>
    );
    const deploymentsSection = (
      <section className="prefs-section">
        <h2>Deployments</h2>
        <DeploymentsList />
      </section>
    );

    if (!status.logged_in) {
      return (
        <>
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
            <button type="button" className="btn btn-primary" onClick={() => void signin.begin()}>
              Sign in to Fused
            </button>
          )}
          {signin.error && <div className="deploy-error">{signin.error}</div>}
          {actionError && <div className="deploy-error">{actionError}</div>}
        </section>
          {envsSection}
          {deploymentsSection}
        </>
      );
    }

    const probe = status.probe;
    const hasManaged = status.store.envs.some((e) => e.backend === "fused");
    // A remote workspace already exists → the panel CONNECTS it rather than
    // creating anything; the header should say so.
    const hasWorkspace = probe?.ok === true && probe.orgs.some((o) => o.org && o.env);
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
              className="btn btn-danger"
              onClick={onLogout}
              disabled={busy !== null}
              title="Removes the fused CLI's stored sign-in on this machine"
            >
              {busy === "logout" ? "Signing out…" : "Sign out"}
            </button>
          </div>
          {actionError && <div className="deploy-error">{actionError}</div>}
        </section>
        {envsSection}
        {deploymentsSection}
        <section className="prefs-section">
          <h2>
            {hasManaged
              ? "Add managed environment"
              : hasWorkspace
                ? "Connect hosted environment"
                : "Set up hosted environment"}
          </h2>
          {hasManaged && !showSetup ? (
            <button type="button" onClick={() => setShowSetup(true)}>
              Set up another managed environment
            </button>
          ) : (
            <SetupPanel
              status={status}
              probing={probing}
              onChanged={() => {
                // Pin the panel open BEFORE the refresh: the env landing
                // flips hasManaged, which would otherwise collapse the panel
                // and destroy its success note the moment setup finishes.
                // reprobe: a self-serve setup may have just created the
                // personal workspace the orgs table shows.
                setShowSetup(true);
                void load(true, true);
              }}
            />
          )}
        </section>
      </>
    );
  };

  return <div className="prefs-page">{body()}</div>;
}
