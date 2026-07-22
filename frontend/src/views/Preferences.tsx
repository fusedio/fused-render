// Preferences page (SPEC §20) — the `/view/_prefs` sentinel route, entered
// from the sidebar's bottom-left gear. Two tabs (D125):
//   Render preferences — Logs, Execution engine, Deploy to Fused account
//     (the opt-in Deploy-button toggle), Tour. Always present; the default
//     (clean URL).
//   Fused account       — the account/sign-in/environments panel (formerly
//     its own `/view/_account` page, folded in once it stopped being a
//     separate sidebar entry). Shown only once Deploy is enabled — that's
//     the only reason this app cares about a Fused account.
// The active tab lives in the URL (`?tab=account`), same pattern as
// Templates' bindings/library tabs.
// Template bindings live in the dedicated /view/_templates view.
import { useEffect, useState } from "react";
import { getPrefs, putDeployEnabled, putEnginePref, revealPath } from "../lib/api";
import type { Prefs } from "../lib/api";
import { navigateUrl } from "../lib/router";
import { notifyPrefsChanged } from "../lib/prefs";
import { startTour } from "../lib/tour";
import { ErrorBanner } from "../components/ErrorBanner";
import { AccountPanel } from "./Account";

type PrefsTab = "render" | "account";

function TourSection() {
  return (
    <section className="prefs-section">
      <h2>Tour</h2>
      <p className="deploy-muted">
        A short guided walkthrough of the interface. It also runs automatically on your first visit.
      </p>
      <button type="button" onClick={() => startTour()}>
        Start tour
      </button>
    </section>
  );
}

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
      {error && <ErrorBanner>{error}</ErrorBanner>}
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
        How <code>fused.runPython</code> runs a page's Python.{" "}
        <b>Both engines run on this machine</b> — neither uses your configured Fused environments
        (those are only deploy targets, chosen in a page's Deploy dialog). Changes apply to the next
        run — no restart needed.
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
          <b>Local (built-in)</b> — a fresh subprocess per call, in the environment that launched
          this server.
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
          resolved into cached venvs (<code>~/.openfused/venvs</code>), plus <code>@fused.udf</code>{" "}
          / <code>result</code> entrypoints.
          {!engine.fused_available && (
            <span className="deploy-muted"> (unavailable — the fused package isn't installed)</span>
          )}
        </span>
      </label>
      <div className="deploy-muted">
        Currently running:{" "}
        <b>{engine.effective === "fused" ? "Fused engine" : "Local (built-in)"}</b>
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
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

function DeployToggle({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const enabled = prefs.deploy.enabled;

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      onChange(await putDeployEnabled(!enabled));
      // The sidebar's signed-in dot (useDeployEnabled) is mounted alongside
      // this page, not remounted by navigation — without this it would only
      // pick up the flip on the next focus/visibility return.
      notifyPrefsChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <label className="prefs-radio">
        <input type="checkbox" checked={enabled} disabled={busy} onChange={toggle} />
        <span>
          <b>Show the Deploy button</b> on renderable pages. Deploy publishes a page to a public
          hosted URL through the <code>fused</code> CLI.
        </span>
      </label>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </>
  );
}

function DeploymentsSection({
  prefs,
  onChange,
  onOpenAccount,
}: {
  prefs: Prefs;
  onChange: (p: Prefs) => void;
  onOpenAccount: () => void;
}) {
  return (
    <section className="prefs-section">
      <h2>Deploy to Fused account</h2>
      <DeployToggle prefs={prefs} onChange={onChange} />
      {prefs.deploy.enabled && (
        <p className="deploy-muted">
          The per-environment share list (every deployed mount, with Revoke) lives on the{" "}
          <button type="button" className="link-button" onClick={onOpenAccount}>
            Fused account tab
          </button>{" "}
          beside your environments.
        </p>
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

  // Requested tab lives in the URL (`?tab=account`) — bookmarkable, and how
  // the Deploy modal and the old `/view/_account` redirect (App.tsx) land
  // here directly on the account tab. Falls back to "render" whenever the
  // account tab wouldn't be offered (Deploy not enabled) rather than showing
  // a tab with no button pointing at it.
  const requestedTab: PrefsTab =
    new URLSearchParams(location.search).get("tab") === "account" ? "account" : "render";
  const tab: PrefsTab = requestedTab === "account" && prefs?.deploy.enabled ? "account" : "render";
  const setTab = (next: PrefsTab) => {
    const params = new URLSearchParams(location.search);
    if (next === "render") params.delete("tab");
    else params.set("tab", next);
    const search = params.toString();
    navigateUrl(location.pathname + (search ? "?" + search : ""));
  };

  return (
    <div className="prefs-page">
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!prefs && !error && <div className="deploy-muted">Loading…</div>}
      {prefs && (
        <>
          <div className="prefs-tabs">
            <button
              type="button"
              className={"prefs-tab" + (tab === "render" ? " active" : "")}
              onClick={() => setTab("render")}
            >
              Render preferences
            </button>
            {prefs.deploy.enabled && (
              <button
                type="button"
                className={"prefs-tab" + (tab === "account" ? " active" : "")}
                onClick={() => setTab("account")}
              >
                Fused account
              </button>
            )}
          </div>
          <div className="prefs-tabpanel">
            {tab === "render" && (
              <>
                <LogsSection prefs={prefs} />
                <EngineSection prefs={prefs} onChange={setPrefs} />
                <DeploymentsSection
                  prefs={prefs}
                  onChange={setPrefs}
                  onOpenAccount={() => setTab("account")}
                />
                <TourSection />
              </>
            )}
            {tab === "account" && prefs.deploy.enabled && <AccountPanel />}
          </div>
        </>
      )}
    </div>
  );
}
