// Preferences page (SPEC §20) — the `/view/_prefs` sentinel route, entered
// from the sidebar's bottom-left gear. Three tabs (D125; AI accounts added
// alongside the bundled AI proxy work — docs/AI_PROXY_BUNDLING.md):
//   Render preferences — Appearance, Call log (capture/redaction/retention for
//     fused_render/calls.py), Deploy to Fused account (the opt-in Deploy-button
//     toggle), Accessibility, and last Execution engine. Always present; the
//     default (clean URL). No Tour button — the tour still runs itself on a
//     first visit (App.tsx's maybeAutoStartTour); it is onboarding, not a
//     preference. The app's OWN log is not here either: it is disposable
//     temp-dir output (D68) reached from the desktop tray's "Open app logs", and a
//     second "Logs" heading next to the Call log section only ever read as the
//     call log's own settings.
//   AI accounts         — connect/disconnect Claude and ChatGPT logins against
//     the bundled AI proxy that backs fused.ai() (fused_render/ai_accounts.py).
//     Always offered, unlike Fused account below — there is no enabling pref
//     to gate it on, and a user with no accounts yet is exactly who the tab is
//     for (docs/AI_PROXY_BUNDLING.md's "Preferences UI" section).
//   Fused account       — the account/sign-in/environments panel (formerly
//     its own `/view/_account` page, folded in once it stopped being a
//     separate sidebar entry). Shown only once Deploy is enabled — that's
//     the only reason this app cares about a Fused account.
// The active tab lives in the URL (`?tab=account` / `?tab=ai`), same pattern
// as Templates' bindings/library tabs.
// Template bindings live in the dedicated /view/_templates view.
import { useEffect, useRef, useState } from "react";
import {
  addAiApiKey,
  deleteAiAccount,
  deleteAiApiKey,
  cancelAiConnect,
  getAiAccounts,
  getPrefs,
  putAiRoutingStrategy,
  putCallsEnabled,
  putCallsParamsMode,
  putCallsRetentionDays,
  putDeployEnabled,
  putEnginePref,
  putReaderEnabled,
} from "../lib/api";
import type {
  AiAccount,
  AiAccountsResult,
  AiApiKey,
  AiProvider,
  AiRoutingStrategy,
  CallsParamsMode,
  Prefs,
} from "../lib/api";
import { useAiLogin } from "../lib/aiAccounts";
import { navigate, navigateUrl } from "../lib/router";
import { notifyPrefsChanged } from "../lib/prefs";
import { ErrorBanner } from "../components/ErrorBanner";
import { Field, Select, TextInput } from "../components/field/fields";
import { useThemePref } from "../lib/theme";
import { AccountPanel } from "./Account";

type PrefsTab = "render" | "ai" | "account";

// The one section on this page that is deliberately NOT server-backed. Every
// other control here round-trips /api/prefs (shell/prefs.py); Appearance is
// per-browser-profile localStorage["fused-render:theme"] by decision — SPEC §30
// AP-1 / D134 — so a browser tab and the desktop window can legitimately hold
// different choices, and there is no server store to keep in sync. Writes are
// synchronous, hence no busy/locked/error plumbing.
function AppearanceSection() {
  const [pref, setPref] = useThemePref();
  return (
    <section className="prefs-section">
      <h2>Appearance</h2>
      <p className="deploy-muted">
        Light or dark for this app. Stored in this browser profile, so each browser and the
        desktop window remember their own choice. Applies immediately.
      </p>
      <label className="prefs-radio">
        <input
          type="radio"
          name="appearance"
          checked={pref === "system"}
          onChange={() => setPref("system")}
        />
        <span>
          <b>System</b> — follows your desktop appearance, including a scheduled day/night
          switch.
        </span>
      </label>
      <label className="prefs-radio">
        <input
          type="radio"
          name="appearance"
          checked={pref === "light"}
          onChange={() => setPref("light")}
        />
        <span>
          <b>Light</b> — always light, whatever your desktop is set to.
        </span>
      </label>
      <label className="prefs-radio">
        <input
          type="radio"
          name="appearance"
          checked={pref === "dark"}
          onChange={() => setPref("dark")}
        />
        <span>
          <b>Dark</b> — always dark, whatever your desktop is set to.
        </span>
      </label>
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
        targets, chosen in a page's Deploy dialog). Changes apply to the next run — no restart
        needed.
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
          <code>@fused.udf</code> / <code>result</code> entrypoints.
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
          <b>Show the Deploy button</b> on renderable pages. Deploy publishes a page to a
          public hosted URL through the <code>fused</code> CLI.
        </span>
      </label>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </>
  );
}

function ReaderToggle({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const enabled = prefs.reader.enabled;

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      onChange(await putReaderEnabled(!enabled));
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
          <b>Reader (listen to files)</b>. Adds a Reader mode to text files and PDFs that reads
          them aloud.
        </span>
      </label>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </>
  );
}

function AccessibilitySection({
  prefs,
  onChange,
}: {
  prefs: Prefs;
  onChange: (p: Prefs) => void;
}) {
  return (
    <section className="prefs-section">
      <h2>Accessibility</h2>
      <ReaderToggle prefs={prefs} onChange={onChange} />
    </section>
  );
}

// The retention window as the "Currently keeping ..." line says it, matching
// the select's own option labels so the forced value reads like a choice.
function describeRetention(days: number): string {
  if (days === 0) return "until the size cap";
  return days === 1 ? "1 day" : `${days} days`;
}

function CallLogSection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const calls = prefs.calls;
  // Same shape as the Engine section's `locked`: a non-null raw env value means
  // the process overrides the pref, so the control is shown but not actionable.
  // Non-null is the server's assertion that the variable is actually IN FORCE,
  // not merely set — it withholds the value when the writer ignores it (an empty
  // or non-numeric retention window, say). So never re-derive this from the
  // value's shape here: a client-side "is it a number?" check is the second copy
  // of a rule the writer already owns, and lockout is what it costs to get wrong.
  const enabledLocked = calls.enabled_forced_by !== null;
  const retentionLocked = calls.retention_forced_by !== null;

  const apply = async (fn: () => Promise<Prefs>) => {
    setBusy(true);
    setError(null);
    try {
      onChange(await fn());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Call log</h2>
      <p className="deploy-muted">
        Records every API call your pages make — each <code>runPython</code>, <code>readFile</code>,{" "}
        <code>stat</code> and <code>writeFile</code>, with its duration, result size, output and
        any traceback. A page with recorded calls gains a <b>Calls</b> view mode showing charts and
        a per-target breakdown; <code>fused-render calls</code> reads the same log from a terminal.
      </p>
      {/* The checkbox shows the STORED pref and the muted line below shows what
          is actually in force, exactly as the Engine section does: the control
          reflects the choice you made (and what a PUT round-trips), the line
          reports reality. They diverge whenever FUSED_RENDER_CALLS wins, and
          the control is disabled then so the discrepancy can't be acted on. */}
      <label className="prefs-radio">
        <input
          type="checkbox"
          checked={calls.enabled}
          disabled={busy || enabledLocked}
          onChange={() => apply(() => putCallsEnabled(!calls.enabled))}
        />
        <span>
          <b>Record API calls</b> made by pages rendered in this app.
        </span>
      </label>
      <div className="deploy-muted">
        Currently <b>{calls.effective_enabled ? "recording" : "not recording"}</b>
        {enabledLocked && (
          <>
            {" "}
            — locked by <code>FUSED_RENDER_CALLS={calls.enabled_forced_by}</code> for this process;
            the checkbox applies once the variable is removed.
          </>
        )}
      </div>
      <div className="prefs-field">
        <label>
          Parameters{" "}
          {/* Gated on what is actually recording, not on the stored pref —
              otherwise an env-forced off state leaves these live, and an
              env-forced on state greys them out while calls are landing. */}
          <select
            value={calls.params}
            disabled={busy || !calls.effective_enabled}
            onChange={(e) => apply(() => putCallsParamsMode(e.target.value as CallsParamsMode))}
          >
            <option value="full">Record values</option>
            <option value="keys">Record names only</option>
            <option value="off">Record nothing</option>
          </select>
        </label>
        <p className="deploy-muted">
          A run's parameters are usually the whole repro, so they are recorded by default — they
          are already visible in the URL. Switch to names-only if a page passes a secret as a
          parameter.
        </p>
      </div>
      <div className="prefs-field">
        <label>
          Keep for{" "}
          <select
            value={String(calls.retention_days)}
            disabled={busy || !calls.effective_enabled || retentionLocked}
            onChange={(e) => apply(() => putCallsRetentionDays(Number(e.target.value)))}
          >
            <option value="1">1 day</option>
            <option value="7">7 days</option>
            <option value="14">14 days</option>
            <option value="90">90 days</option>
            <option value="0">Until the size cap</option>
          </select>
        </label>
        {retentionLocked && (
          <p className="deploy-muted">
            Currently keeping <b>{describeRetention(calls.effective_retention_days)}</b> — locked by{" "}
            <code>FUSED_RENDER_CALLS_RETENTION_DAYS={calls.retention_forced_by}</code> for this
            process; the choice above applies once the variable is removed.
          </p>
        )}
      </div>
      <p className="deploy-muted">
        Stored at <code>{calls.dir}</code>
        {calls.dir_exists ? "." : " — no calls recorded yet, so the folder does not exist."}
      </p>
      {/* Navigates IN-APP, not to the OS file manager: the explorer is how you
          reach the Calls view — open the folder, click a .calls.jsonl, and it
          renders in the same viewer the mode switcher offers.

          Disabled until the store exists: the writer creates it on its first
          append, so browsing beforehand navigates to a path that fails to stat
          — an error card where the answer is simply "nothing has run yet",
          which is also the answer to "why has no page got a Calls mode?". */}
      <button
        type="button"
        disabled={!calls.dir_exists}
        title={calls.dir_exists ? undefined : "No calls have been recorded yet"}
        onClick={() => navigate(calls.dir, { isDir: true })}
      >
        Browse call logs
      </button>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
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

function aiProviderLabel(p: AiProvider): string {
  return p === "claude" ? "Claude" : "ChatGPT";
}

// A pasted API key (ai_accounts.py's "API keys" section) — a second,
// config-level way to authenticate one of the same two providers OAuth
// connects above. Its own section rather than folded into the accounts
// table: a key has no session to expire and no browser to reconnect
// through, and removing one is a config write, not a file deletion — enough
// of a different affordance that sharing a table would force the reader to
// sniff which kind each row is. Rendered as plain rows (deploy-form-row), not
// a table, so it reads as visually distinct from the OAuth table above it.
//
// One shared form with a provider selector, not two per-provider forms: the
// add flow (paste a key, submit) is identical for both providers, so a
// second copy of the same three fields would only be duplicated markup, not
// a meaningfully different experience.
function ApiKeysSection({ apiKeys, onChanged }: { apiKeys: AiApiKey[]; onChanged: () => void }) {
  const [provider, setProvider] = useState<AiProvider>("claude");
  const [apiKey, setApiKey] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  // The auth_index currently being removed — row-scoped busy, same pattern as
  // AiAccountsPanel's rowBusy for Disconnect above.
  const [rowBusy, setRowBusy] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  const submit = async () => {
    if (addBusy || !apiKey) return;
    setAddBusy(true);
    setAddError(null);
    try {
      await addAiApiKey(provider, apiKey);
      // Cleared the instant the request settles, success or not: the module
      // docstring's "never reaches a client" rule cuts both ways — this app
      // must not hold a pasted key in React state any longer than the POST
      // needs it, so a stale tab can't leak it back out via devtools.
      setApiKey("");
      onChanged();
    } catch (e) {
      setAddError((e as Error).message);
    } finally {
      setAddBusy(false);
    }
  };

  const remove = (key: AiApiKey) => {
    if (
      !window.confirm(`Remove this ${aiProviderLabel(key.provider)} API key (${key.hint})?`)
    ) {
      return;
    }
    void (async () => {
      setRowBusy(key.auth_index);
      setRowError(null);
      try {
        await deleteAiApiKey(key.auth_index);
        onChanged();
      } catch (e) {
        setRowError((e as Error).message);
      } finally {
        setRowBusy(null);
      }
    })();
  };

  return (
    <section className="prefs-section">
      <h2>API keys</h2>
      <p className="deploy-muted">
        A second way to authenticate Claude or ChatGPT for <code>fused.ai()</code>, alongside the
        accounts above — paste a key from the provider's own dashboard instead of signing in with
        a subscription. Useful for a pay-per-token key, or when there's no interactive login to
        use. The full key is never stored by this app past the request that adds it, and there is
        no way to read it back — only the masked hint below ever comes back from the server.
      </p>
      {apiKeys.length === 0 ? (
        <p className="deploy-muted">No API keys added yet.</p>
      ) : (
        apiKeys.map((k) => (
          <div className="deploy-form-row" key={k.auth_index}>
            <span>
              <b>{aiProviderLabel(k.provider)}</b> <code>{k.hint}</code>
            </span>
            {rowBusy === k.auth_index ? (
              <span className="deploy-muted">Removing…</span>
            ) : (
              <button
                type="button"
                className="btn btn-danger"
                disabled={rowBusy !== null}
                onClick={() => remove(k)}
              >
                Remove
              </button>
            )}
          </div>
        ))
      )}
      {rowError && <div className="deploy-error">{rowError}</div>}
      <form
        className="deploy-form-row"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Field label="Provider">
          <Select
            value={provider}
            disabled={addBusy}
            onChange={(e) => setProvider(e.target.value as AiProvider)}
          >
            <option value="claude">Claude</option>
            <option value="codex">ChatGPT</option>
          </Select>
        </Field>
        <Field label="API key">
          {/* type="password": a pasted key must never render as plain text on
              screen, matching the backend's own "never reveal a full key"
              rule. autoComplete off so the browser doesn't offer to save/fill
              a provider secret into its own password store. */}
          <TextInput
            type="password"
            autoComplete="off"
            placeholder="paste key"
            style={{ minWidth: 220 }}
            value={apiKey}
            disabled={addBusy}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </Field>
        {/* Blank caption reserves the label row's height so the button aligns
            with the inputs, not the captions above them (AddRemote's pattern). */}
        <Field label={" "}>
          <button type="submit" className="btn btn-primary" disabled={addBusy || !apiKey}>
            {addBusy ? "Adding…" : "Add key"}
          </button>
        </Field>
      </form>
      {addError && <div className="deploy-error">{addError}</div>}
    </section>
  );
}

// PUT /api/ai/accounts/routing-strategy. Mirrors EngineSection's register
// (a locked-looking radio pair with a plain-language tradeoff line each), but
// nothing here is ever locked — there is no env override for this pref.
function RoutingStrategySection({
  strategy,
  onChanged,
}: {
  strategy: AiRoutingStrategy;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const select = async (value: AiRoutingStrategy) => {
    if (busy || value === strategy) return;
    setBusy(true);
    setError(null);
    try {
      await putAiRoutingStrategy(value);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Routing strategy</h2>
      <p className="deploy-muted">
        How <code>fused.ai()</code> picks a credential when a provider has more than one —
        signed-in accounts and API keys are pooled together here, not treated separately.
        Changing this restarts the AI proxy; it respawns lazily with the new setting on its next
        use, so it's normal to see it briefly report not running right after.
      </p>
      <label className="prefs-radio">
        <input
          type="radio"
          name="ai-routing-strategy"
          checked={strategy === "round-robin"}
          disabled={busy}
          onChange={() => select("round-robin")}
        />
        <span>
          <b>Round-robin</b> — every credential for a provider is rotated between calls, with
          failover to the next one when one hits a rate limit. The upstream default.
        </span>
      </label>
      <label className="prefs-radio">
        <input
          type="radio"
          name="ai-routing-strategy"
          checked={strategy === "fill-first"}
          disabled={busy}
          onChange={() => select("fill-first")}
        />
        <span>
          <b>Fill-first</b> — one credential is used until it fails (rate limit, expiry), only
          then does the next one take over. Pick this to keep a preferred account or key in use
          for as long as possible before falling back.
        </span>
      </label>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

// The "AI accounts" tab (fused_render/ai_accounts.py). No enabling pref gates
// it — unlike AccountPanel this is offered to everyone, since a user with
// nothing connected yet is exactly who it's for. Connect flow follows
// useAiLogin (lib/aiAccounts.ts, mirroring useFusedLogin); the account list
// and its per-row Disconnect follow Account.tsx's environments table.
function AiAccountsPanel() {
  const [data, setData] = useState<AiAccountsResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // The account (by listing `name`) currently being disconnected — disables
  // just that row's button rather than the whole panel.
  const [rowBusy, setRowBusy] = useState<string | null>(null);

  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    []
  );

  const load = async () => {
    try {
      const fresh = await getAiAccounts();
      if (alive.current) {
        setData(fresh);
        setLoadError(null);
      }
    } catch (e) {
      if (alive.current) setLoadError((e as Error).message);
    }
  };
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadRef = useRef(load);
  loadRef.current = load;
  const login = useAiLogin(() => void loadRef.current());

  // A login started from another page/tab shows up as the listing's own
  // `login` field (Account.tsx's inFlightElsewhere pattern) — this tab never
  // called connect itself, so connect/status was never subscribed to here;
  // polling the LISTING is the only way to notice it resolve.
  const inFlightElsewhere = data?.login != null && !login.connecting;
  useEffect(() => {
    if (!inFlightElsewhere) return;
    const id = window.setInterval(() => void loadRef.current(), 2000);
    return () => window.clearInterval(id);
  }, [inFlightElsewhere]);

  const onCancelElsewhere = async () => {
    setActionError(null);
    try {
      await cancelAiConnect();
    } catch (e) {
      if (alive.current) setActionError((e as Error).message);
    }
    void load();
  };

  const onDisconnect = (account: AiAccount) => {
    const label = account.email ?? account.label ?? account.name;
    // Reversible by reconnecting — the tone Account.tsx's "Forget…" confirm
    // uses for its local-only removals, adapted: this one really does delete
    // the credential file server-side, but signing in again recreates it.
    if (!window.confirm(`Disconnect ${label}? You can reconnect it any time.`)) return;
    void (async () => {
      setRowBusy(account.name);
      setActionError(null);
      try {
        await deleteAiAccount(account.name);
        await load();
      } catch (e) {
        if (alive.current) setActionError((e as Error).message);
      } finally {
        if (alive.current) setRowBusy(null);
      }
    })();
  };

  if (loadError) {
    return (
      <section className="prefs-section">
        <h2>AI accounts</h2>
        <div className="deploy-error">{loadError}</div>
        <button type="button" onClick={() => void load()}>
          Retry
        </button>
      </section>
    );
  }
  if (!data) {
    return (
      <section className="prefs-section">
        <h2>AI accounts</h2>
        <div className="deploy-muted">Loading…</div>
      </section>
    );
  }

  // Both Connect buttons disable together, not just the in-flight provider's:
  // the callback ports are fixed per provider, so only one login can run at
  // all across the whole app, regardless of which provider it's for.
  const connectDisabled = login.connecting || inFlightElsewhere;

  return (
    <>
    <section className="prefs-section">
      <h2>AI accounts</h2>
      <p className="deploy-muted">
        Connects a Claude or ChatGPT subscription so pages can call <code>fused.ai()</code> —
        a one-time browser sign-in, no API key to manage.
        {!data.running && " The AI proxy isn't running yet; it starts on the first fused.ai() call."}
      </p>

      {data.accounts.length === 0 ? (
        <p className="deploy-muted">
          {data.api_keys.length === 0 ? (
            <>
              Nothing connected yet. Connect Claude or ChatGPT below for one-click access to a
              subscription you already have, or add an API key in the API keys section further
              down instead — better for a pay-per-token key, or a provider account with no
              interactive login. Either one is enough for pages to start calling{" "}
              <code>fused.ai()</code>.
            </>
          ) : (
            <>
              No signed-in accounts yet — connect one below, or rely on the API key(s) already
              configured further down.
            </>
          )}
        </p>
      ) : (
        <table className="deploy-shares-table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Account</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.accounts.map((a) => (
              <tr key={a.name}>
                <td>{aiProviderLabel(a.provider)}</td>
                <td>
                  {a.email ?? a.label ?? a.name}
                  {a.disabled && <span className="deploy-muted"> (disabled)</span>}
                </td>
                <td className="row-actions-cell">
                  {rowBusy === a.name ? (
                    <span className="deploy-muted">Disconnecting…</span>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-danger"
                      disabled={rowBusy !== null}
                      onClick={() => onDisconnect(a)}
                    >
                      Disconnect
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {login.connecting ? (
        <div className="deploy-form-row">
          <span className="deploy-spinner" />
          <span className="deploy-muted">
            Waiting for the browser sign-in
            {login.provider ? ` (${aiProviderLabel(login.provider)})` : ""}… finish signing in in
            the tab that just opened.
          </span>
          <button type="button" onClick={() => void login.cancel()}>
            Cancel
          </button>
        </div>
      ) : inFlightElsewhere ? (
        <div className="deploy-form-row">
          <span className="deploy-muted">
            A sign-in is already in progress
            {data.login ? ` (${aiProviderLabel(data.login.provider)})` : ""} — started from
            another page or tab.
          </span>
          <button type="button" onClick={() => void onCancelElsewhere()}>
            Cancel it
          </button>
        </div>
      ) : (
        <div className="deploy-form-row">
          <button
            type="button"
            className="btn btn-primary"
            disabled={connectDisabled}
            onClick={() => void login.begin("claude")}
          >
            Connect Claude
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={connectDisabled}
            onClick={() => void login.begin("codex")}
          >
            Connect ChatGPT
          </button>
        </div>
      )}
      {login.error && <div className="deploy-error">{login.error}</div>}
      {actionError && <div className="deploy-error">{actionError}</div>}
    </section>
    <ApiKeysSection apiKeys={data.api_keys} onChanged={() => void load()} />
    <RoutingStrategySection strategy={data.routing_strategy} onChanged={() => void load()} />
    </>
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

  // Requested tab lives in the URL (`?tab=account` / `?tab=ai`) — bookmarkable,
  // and how the Deploy modal and the old `/view/_account` redirect (App.tsx)
  // land here directly on the account tab. Falls back to "render" whenever the
  // account tab wouldn't be offered (Deploy not enabled) rather than showing
  // a tab with no button pointing at it; "ai" needs no such fallback — it has
  // no gating pref.
  const rawTab = new URLSearchParams(location.search).get("tab");
  const requestedTab: PrefsTab = rawTab === "account" ? "account" : rawTab === "ai" ? "ai" : "render";
  const tab: PrefsTab =
    requestedTab === "account" && !prefs?.deploy.enabled ? "render" : requestedTab;
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
            <button
              type="button"
              className={"prefs-tab" + (tab === "ai" ? " active" : "")}
              onClick={() => setTab("ai")}
            >
              AI accounts
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
                <AppearanceSection />
                <CallLogSection prefs={prefs} onChange={setPrefs} />
                <DeploymentsSection
                  prefs={prefs}
                  onChange={setPrefs}
                  onOpenAccount={() => setTab("account")}
                />
                <AccessibilitySection prefs={prefs} onChange={setPrefs} />
                {/* Last: the engine is the setting a user is least likely to
                    come here to change (builtin is right for almost everyone,
                    and an env var pins it in the cases that matter), so it does
                    not deserve the position above the ones they do. */}
                <EngineSection prefs={prefs} onChange={setPrefs} />
              </>
            )}
            {tab === "ai" && <AiAccountsPanel />}
            {tab === "account" && prefs.deploy.enabled && <AccountPanel />}
          </div>
        </>
      )}
    </div>
  );
}
