// Accounts: every service this app signs in to, in one place. Agent sign-in
// (Claude Code, Copilot) stays with each agent's health under Coding agents,
// where the fix for a broken install sits beside it; a pointer row here finds it.
//
// NO TOKEN PASSES THROUGH THIS FILE in either direction. Each sign-in starts
// the provider's own login (the fused CLI's browser login; huggingface_hub's
// device-code flow), the provider stores the result, and these rows only read
// "signed in / as whom". Each POLLS only while a login it started is in flight
// — the wait is a person finishing a browser round trip — and is otherwise idle.
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  USER_WORKBENCH_ENVS,
  cancelLogin,
  loginOutcome,
  getCanvasesStatus,
  getWhoami,
  isWorkbenchEnv,
  logout,
  startLogin,
  type CanvasesStatus,
  type WorkbenchEnv,
} from "@apps/canvases/api";
import { FusedSettingsModal } from "@apps/canvases/FusedSettingsModal";
import { publishCanvasesEnabled } from "@apps/canvases/feature-flag";
import {
  cancelHfLogin,
  getHfAuth,
  hfLogout,
  putCanvasesEnabled,
  putWorkbenchEnvsEnabled,
  startHfLogin,
  type HfAuth,
} from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { RowSwitch, SettingRow, type PrefControlProps } from "./SettingRow";

const FUSED_LOGIN_POLL_MS = 1500;

// FUSED WORKBENCH (Preferences > Fused Workbench): one row per environment a
// person can turn on — Fused hosted (prod) and On-prem (custom) — each a
// switch. Unstable has no row (owner, 2026-11-26): the server adds it to the
// enabled list by itself for a Fused employee signed in to Fused hosted
// (fusedcli._staff_unstable), and only the Canvases picker ever shows it. An environment that is on shows its own sign-in line under
// the switch; Custom shows its config line first, and its sign-in unlocks
// once ~/.fused/settings.toml names a deployment. The switches write
// `workbench_envs_enabled` (PUT /api/prefs): the canvas catalog's environment
// picker offers only the environments turned on, and is not drawn when there
// is one. Turning an environment off signs nothing out and stops no sync.
//
// The three rows are three components (the registry draws one per id), so
// what they share lives in a small module store: the env-less status (which
// are on, the pin, the CLI, custom's config) and a counter bumped when any
// row's sign-in state changes — prod and unstable share one `fused login`
// store, so signing in on one row is news for the other.
interface WorkbenchShared {
  status: CanvasesStatus | null;
  error: string | null;
  authGen: number;
  /** A switch's write is on the wire. Shared, because each write sends the
   *  whole list: two rows toggled at once would each send a list without the
   *  other's change. */
  toggling: boolean;
}
let workbenchShared: WorkbenchShared = { status: null, error: null, authGen: 0, toggling: false };
const workbenchListeners = new Set<() => void>();
let workbenchLoading: Promise<void> | null = null;

function setWorkbenchShared(patch: Partial<WorkbenchShared>) {
  workbenchShared = { ...workbenchShared, ...patch };
  workbenchListeners.forEach((fn) => fn());
}

// Bumped by every read that must be newer than a write: an answer that left
// before it is dropped rather than stored over the write's result.
let workbenchGen = 0;

/** Read the shared status. `afterWrite` is the read that follows a change (a
 *  switch, a config save): it never rides a read already on the wire, which
 *  may have left before the change and would put the old list back. */
function reloadWorkbenchShared(afterWrite = false): Promise<void> {
  if (workbenchLoading && !afterWrite) return workbenchLoading;
  const gen = afterWrite ? ++workbenchGen : workbenchGen;
  const p = getCanvasesStatus()
    .then((status) => {
      if (gen === workbenchGen) setWorkbenchShared({ status, error: null });
    })
    .catch((e: Error) => {
      if (gen === workbenchGen) setWorkbenchShared({ error: e.message });
    })
    .finally(() => {
      if (workbenchLoading === p) workbenchLoading = null;
    });
  workbenchLoading = p;
  return p;
}

const subscribeWorkbench = (fn: () => void) => {
  workbenchListeners.add(fn);
  return () => {
    workbenchListeners.delete(fn);
  };
};

function useWorkbenchShared(): WorkbenchShared {
  const shared = useSyncExternalStore(subscribeWorkbench, () => workbenchShared);
  useEffect(() => {
    // Every mount re-reads: the page may have been away while a pin, the
    // config file or another window's toggle changed the answer.
    void reloadWorkbenchShared();
  }, []);
  return shared;
}

/** The environments a person turned on, as the server resolved them —
 *  unstable left out, since it is never theirs to switch. An older server
 *  answers without the list: both count as on then. */
const enabledEnvsOf = (s: CanvasesStatus): WorkbenchEnv[] =>
  s.enabled_envs
    ? s.enabled_envs.filter(isWorkbenchEnv).filter((e) => USER_WORKBENCH_ENVS.includes(e))
    : [...USER_WORKBENCH_ENVS];

const hostOf = (url: string): string => {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
};

function WorkbenchEnvRow({ env, prefs, onChange }: { env: WorkbenchEnv } & PrefControlProps) {
  const { status, error: loadError, toggling: busy } = useWorkbenchShared();
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const id = `workbench-${env}`;

  const enabled = status ? enabledEnvsOf(status) : [];
  const on = enabled.includes(env);
  const pinned = status?.locked === true;
  const lastOn = on && enabled.length === 1;
  const configured = env !== "custom" || status?.envs?.custom?.available !== false;

  const onToggle = async (next: boolean) => {
    if (!status || workbenchShared.toggling) return;
    setWorkbenchShared({ toggling: true });
    setError(null);
    try {
      if (!next && lastOn) {
        // BOTH OFF IS WORKBENCH OFF (owner, 2026-11-26): the last environment
        // is not switched off — Fused Workbench is, and this environment
        // stays the one on for when it comes back. The list is never empty.
        const saved = await putCanvasesEnabled(false);
        publishCanvasesEnabled(saved.canvases.enabled);
        onChange(saved);
        return;
      }
      const want = USER_WORKBENCH_ENVS.filter((e) => (e === env ? next : enabled.includes(e)));
      const saved = await putWorkbenchEnvsEnabled(want);
      // The page's own copy too: the proxy row under On-prem shows with it.
      onChange(saved);
      // The write's own answer first: if the read below fails, the next
      // switch must still build its list from what is now stored.
      const confirmed = saved.workbench?.enabled;
      if (confirmed && workbenchShared.status) {
        workbenchGen += 1;
        setWorkbenchShared({ status: { ...workbenchShared.status, enabled_envs: confirmed } });
      }
      await reloadWorkbenchShared(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setWorkbenchShared({ toggling: false });
    }
  };

  // FUSED_RENDER_WORKBENCH_ENV pins an environment that is none of the three
  // (stg, dev, a PR): every switch is off and locked, and the Production row
  // carries the one sign-in there is, the pinned environment's.
  const pinnedElsewhere = pinned && enabled.length === 0 && env === "prod";

  return (
    <SettingRow
      id={id}
      off={!!status && !on}
      status={
        !status ? (
          loadError ? undefined : "Checking…"
        ) : (pinned && on) || pinnedElsewhere ? (
          <>
            Pinned to <b>{status.workbench_env}</b> by <code>FUSED_RENDER_WORKBENCH_ENV</code>.
          </>
        ) : env === "custom" ? (
          "Your own Fused deployment"
        ) : (
          hostOf(status.envs?.[env]?.base_url ?? "")
        )
      }
      control={
        status ? (
          <RowSwitch
            id={id}
            checked={on}
            disabled={busy || pinned || !prefs.canvases?.enabled}
            title={
              pinned
                ? "Pinned by FUSED_RENDER_WORKBENCH_ENV"
                : lastOn
                  ? "Switching this off turns Fused Workbench off"
                  : undefined
            }
            onChange={(v) => void onToggle(v)}
          />
        ) : undefined
      }
    >
      {(error || loadError) && <ErrorBanner>{error || loadError}</ErrorBanner>}
      {status && on && !status.cli_found && (
        <div className="settings-subrow">
          <span className="settings-subrow-main">
            The fused CLI is not available in this server&rsquo;s environment. Install it with{" "}
            <code>pip install &quot;fused-render[fused]&quot;</code> or set <code>FUSED_RENDER_FUSED_BIN</code>.
          </span>
        </div>
      )}
      {status && on && status.cli_found && env === "custom" && (
        <div className="settings-subrow">
          <span className="settings-subrow-main">
            {configured ? (
              <span className="settings-dot settings-dot-ok">
                Config points at <code>{hostOf(status.envs.custom.base_url)}</code>
              </span>
            ) : (
              <span className="settings-dot settings-dot-idle">
                No config yet. Needs a <code>base_url</code> in <code>{status.settings_path}</code>
              </span>
            )}
          </span>
          <Button variant={configured ? "outline" : "accent"} size="sm" onClick={() => setEditing(true)}>
            {configured ? "Edit config" : "Add config"}
          </Button>
        </div>
      )}
      {status && on && status.cli_found && <WorkbenchEnvSignIn env={env} locked={!configured} />}
      {status && pinnedElsewhere && status.cli_found && <WorkbenchEnvSignIn env={null} locked={false} />}
      {editing && (
        <FusedSettingsModal
          onClose={() => {
            setEditing(false);
            // A save that left no `base_url` keeps the editor open and never
            // reaches `onSaved`; closing after it must still re-read.
            void reloadWorkbenchShared(true);
          }}
          onSaved={() => {
            setEditing(false);
            // The deployment may have changed under a signed-in line: custom's
            // `[auth]` can name another credentials file.
            void reloadWorkbenchShared(true).then(() =>
              setWorkbenchShared({ authGen: workbenchShared.authGen + 1 }),
            );
          }}
        />
      )}
    </SettingRow>
  );
}

// One environment's sign-in line: the `fused login` store that environment
// reads (prod and unstable share one; custom has its own `[auth]`). Log out
// also stops canvas sync — said in the button's title, the one consequence a
// reader would not expect. `locked` is custom with no config: a sign-in then
// would be a hosted-prod sign-in under another name, so the button waits.
// `env` null is the env-less login: the server answers in its auth env.
function WorkbenchEnvSignIn({ env, locked }: { env: WorkbenchEnv | null; locked: boolean }) {
  const { authGen } = useSyncExternalStore(subscribeWorkbench, () => workbenchShared);
  const [status, setStatus] = useState<CanvasesStatus | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loggingIn, setLoggingIn] = useState(false);
  // The raw poll tick's sign-in address (the "browser did not open?" link).
  const [loginUrl, setLoginUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loginStampRef = useRef<number | null>(null);
  const loginStartedRef = useRef<number | null>(null);
  const aliveRef = useRef(true);
  // Reads overlap (mount, focus, after a logout): only the newest may write,
  // or a read that left while signed in lands over the signed-out one.
  const refreshGenRef = useRef(0);

  const refresh = useCallback(async () => {
    const gen = ++refreshGenRef.current;
    const current = () => aliveRef.current && gen === refreshGenRef.current;
    let s = await getCanvasesStatus(env);
    if (!current()) return s;
    if (s.logged_in && !locked) {
      // `logged_in` is the credentials FILE existing; whoami tells a live
      // store from a dead one. A 401 is DOWNGRADED to signed-out before
      // anything is shown — the Canvases page's verdict — so the line offers
      // Sign in rather than a Log out that would change nothing.
      try {
        const who = await getWhoami(env ?? undefined);
        if (!current()) return s;
        // The email when the account has one: it is what decides whether
        // unstable joins the Canvases picker, so it is what the line shows.
        setHandle(who.email || who.handle);
      } catch (e) {
        if (!current()) return s;
        setHandle(null);
        if ((e as Error & { status?: number }).status === 401) {
          s = { ...s, logged_in: false };
        } else {
          // A blip, not a verdict: still signed in, just nameless for now.
          setError((e as Error).message);
        }
      }
    } else {
      setHandle(null);
    }
    setStatus(s);
    return s;
  }, [env, locked]);

  // Also re-read when another row's sign-in changed (`authGen`).
  useEffect(() => {
    aliveRef.current = true;
    refresh().catch((e) => aliveRef.current && setError((e as Error).message));
    return () => {
      aliveRef.current = false;
    };
  }, [refresh, authGen]);

  const announce = () => setWorkbenchShared({ authGen: workbenchShared.authGen + 1 });

  // Coming back to the window re-reads: a sign-in finishes in the browser,
  // and one started on a row since switched off (or in another window, or a
  // terminal) has nobody polling for it here.
  useEffect(() => {
    const onFocus = () => {
      refresh().catch(() => undefined);
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  // Ends when the credentials file's stamp changes (a completed login), or
  // when the browser child exits without one (closed tab, denied).
  useEffect(() => {
    if (!loggingIn) return;
    let cancelled = false;
    // Set once the completed login is seen, so later ticks neither re-run
    // the completion nor fall into the "child gone" branch.
    let completing = false;
    const timer = window.setInterval(() => {
      if (completing) return;
      void getCanvasesStatus(env)
        .then((s) => {
          if (cancelled || completing) return;
          setLoginUrl(s.login_url ?? null);
          if (s.logged_in && s.creds_stamp !== loginStampRef.current) {
            // `loggingIn` drops only AFTER `refresh` wrote the vouched status:
            // dropping it first showed an enabled Sign in for the round trip.
            completing = true;
            setError(null);
            refresh()
              .catch((e) => !cancelled && setError((e as Error).message))
              .finally(() => {
                if (cancelled) return;
                setLoggingIn(false);
                // The other sign-in lines re-read too.
                announce();
              });
          } else if (!s.login_in_flight) {
            setLoggingIn(false);
            setError(loginOutcome(loginStartedRef.current));
          }
        })
        .catch(() => undefined); // a blip mid-login is not worth a banner
    }, FUSED_LOGIN_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [loggingIn, env, refresh]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const onLogin = () =>
    act(async () => {
      loginStampRef.current = status?.creds_stamp ?? null;
      setLoginUrl(null);
      await startLogin(env);
      loginStartedRef.current = Date.now();
      setLoggingIn(true);
    });

  // Drop `loggingIn` FIRST: once the child is cancelled a still-armed tick
  // would report "Sign-in was not completed" for a cancel the reader asked for.
  const onCancel = () =>
    act(async () => {
      setLoggingIn(false);
      await cancelLogin();
    });

  const onLogout = () =>
    act(async () => {
      await logout(env);
      // This line first, while the button is still held; then the others.
      await refresh();
      announce();
    });

  const signedIn = !!status?.logged_in && !locked;
  return (
    <>
      <div className="settings-subrow">
        <span className="settings-subrow-main">
          {!status ? (
            error ? null : "Checking…"
          ) : loggingIn ? (
            <>
              Complete the sign-in in the browser window that just opened.
              {loginUrl && (
                <>
                  {" "}No window?{" "}
                  <a href={loginUrl} target="_blank" rel="noreferrer">
                    Open the sign-in page
                  </a>
                  .
                </>
              )}
            </>
          ) : signedIn ? (
            <span className="settings-dot settings-dot-ok">Signed in{handle ? <> as <b>{handle}</b></> : null}</span>
          ) : (
            <span className="settings-dot settings-dot-idle">Not signed in</span>
          )}
        </span>
        {!status ? null : loggingIn ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void onCancel()}>
            Cancel
          </Button>
        ) : signedIn ? (
          <Button
            variant="destructive"
            size="sm"
            disabled={busy}
            title="Sign out of Fused on this machine. Canvas sync stops until you sign in again."
            onClick={() => void onLogout()}
          >
            Log out
          </Button>
        ) : (
          <Button
            variant={locked ? "outline" : "accent"}
            size="sm"
            disabled={busy || locked}
            title={locked ? "Add the config first" : undefined}
            onClick={() => void onLogin()}
          >
            Sign in
          </Button>
        )}
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </>
  );
}

export const WorkbenchProdControl = (props: PrefControlProps) => <WorkbenchEnvRow env="prod" {...props} />;
export const WorkbenchCustomControl = (props: PrefControlProps) => <WorkbenchEnvRow env="custom" {...props} />;

// Hugging Face (server/routers/hf_auth.py, D402): huggingface_hub's own
// device-code login. Someone who needs a specific fine-grained token exports
// HF_TOKEN instead, which hf reads ahead of its own store and which this row
// reports as being in force. The row POLLS while a login is pending: hf's
// device code lives ~15 minutes, and the wait is a person in another tab.
// This is the only place to log OUT — AI Models offers sign-in alone, inside
// a gated model's drawer.
export function HuggingFaceControl() {
  const [auth, setAuth] = useState<HfAuth | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getHfAuth()
      .then((a) => alive && setAuth(a))
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  const pending = auth?.pending ?? null;
  useEffect(() => {
    if (!pending) return;
    let alive = true;
    const id = setInterval(() => {
      getHfAuth()
        .then((a) => alive && setAuth(a))
        .catch(() => undefined); // a blip mid-login is not worth a banner
    }, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [pending !== null]);

  const act = async (fn: () => Promise<HfAuth>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setAuth(await fn());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const locked = auth?.forcedByVar != null;
  return (
    <SettingRow
      id="huggingface"
      status={
        !auth ? (
          error ? undefined : "Checking…"
        ) : auth.pending ? (
          // The code is shown as well as carried in the link: the Hub asks for
          // confirmation, and a reader in a different browser has to type it.
          <>
            Waiting for you to authorize. If asked for a code, enter <code>{auth.pending.userCode}</code>. It expires in{" "}
            {Math.max(1, Math.round(auth.pending.secondsLeft / 60))} min.
          </>
        ) : auth.signedIn ? (
          <span className="settings-dot settings-dot-ok">
            Signed in{auth.account ? <> as <b>{auth.account}</b></> : null}
          </span>
        ) : (
          "Not signed in. Downloads are anonymous: a lower rate limit, and no gated or private repos."
        )
      }
      control={
        !auth ? undefined : auth.pending ? (
          <>
            {/* An <a>, not a button: it opens the Hub in a new tab and must
                survive a middle-click and a copy-link. */}
            <Button
              variant="accent"
              size="sm"
              nativeButton={false}
              render={<a href={auth.pending.url} target="_blank" rel="noopener noreferrer" />}
            >
              Authorize on huggingface.co
            </Button>
            <Button variant="outline" size="sm" disabled={busy} onClick={() => void act(cancelHfLogin)}>
              Cancel
            </Button>
          </>
        ) : auth.signedIn ? (
          <Button variant="destructive" size="sm" disabled={busy || locked} onClick={() => void act(hfLogout)}>
            Log out
          </Button>
        ) : (
          <Button variant="accent" size="sm" disabled={busy || locked} onClick={() => void act(() => startHfLogin())}>
            Log in to Hugging Face
          </Button>
        )
      }
    >
      {/* The one state no control can show: a variable overriding hf's store,
          where the button is present and would change nothing. */}
      {locked && (
        <p className="settings-row-desc">
          Using the token in <code>{auth!.forcedByVar}</code> from this app&apos;s environment — hf reads that ahead of
          its own store, so signing in here would change nothing until the variable is removed.
        </p>
      )}
      {/* The last attempt's failure (denied, expired, network), kept until the
          next attempt replaces it. */}
      {auth?.error && <ErrorBanner>{auth.error}</ErrorBanner>}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}
