// Accounts: every service this app signs in to, in one place. Agent sign-in
// (Claude Code, Copilot) stays with each agent's health under Coding agents,
// where the fix for a broken install sits beside it; a pointer row here finds it.
//
// NO TOKEN PASSES THROUGH THIS FILE in either direction. Each sign-in starts
// the provider's own login (the fused CLI's browser login; huggingface_hub's
// device-code flow), the provider stores the result, and these rows only read
// "signed in / as whom". Each POLLS only while a login it started is in flight
// — the wait is a person finishing a browser round trip — and is otherwise idle.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ENV_LABELS,
  WORKBENCH_ENVS,
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
import { publishLoggedIn } from "@apps/canvases/logged-in";
import {
  cancelHfLogin,
  getHfAuth,
  hfLogout,
  putWorkbenchEnv,
  startHfLogin,
  type HfAuth,
} from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { SettingRow, SettingSelect } from "./SettingRow";

const FUSED_LOGIN_POLL_MS = 1500;

// The Fused account: the `fused login` provider Share and Canvases run on
// (fused_render/canvases.py owns the routes). It used to be a tab of its own;
// before THAT, the only sign-in UI sat on the Canvases page behind that
// feature's flag (D427), so with the flag off there was no way to see which
// account this machine was on, or leave it. Log out also stops canvas sync —
// said in the button's title, the one consequence a reader would not expect.
export function FusedAccountControl() {
  const [status, setStatus] = useState<CanvasesStatus | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loggingIn, setLoggingIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loginStampRef = useRef<number | null>(null);
  const loginStartedRef = useRef<number | null>(null);
  const aliveRef = useRef(true);

  const refresh = useCallback(async () => {
    let s = await getCanvasesStatus();
    if (!aliveRef.current) return s;
    if (s.logged_in) {
      // `logged_in` is the credentials FILE existing; whoami tells a live
      // store from a dead one. A 401 is DOWNGRADED to signed-out before
      // anything is shown or published — the Canvases page's verdict — so the
      // row offers Sign in rather than a Log out that would change nothing.
      // No env: the server answers in the auth env, which is all a
      // signed-in-as line needs.
      try {
        const who = await getWhoami();
        if (!aliveRef.current) return s;
        setHandle(who.handle);
      } catch (e) {
        if (!aliveRef.current) return s;
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
    publishLoggedIn(s);
    return s;
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    refresh().catch((e) => aliveRef.current && setError((e as Error).message));
    return () => {
      aliveRef.current = false;
    };
  }, [refresh]);

  // Ends when the credentials file's stamp changes (a completed login), or
  // when the browser child exits without one (closed tab, denied).
  useEffect(() => {
    if (!loggingIn) return;
    let cancelled = false;
    // Set once the completed login is seen, so ticks landing while `refresh`
    // asks whoami neither re-run it nor fall into the "child gone" branch.
    let completing = false;
    const id = window.setInterval(() => {
      if (completing) return;
      void getCanvasesStatus()
        .then((s) => {
          if (cancelled || completing) return;
          // The raw tick is NOT stored or published: `logged_in` here is only
          // the file existing, and a cancelled re-login after a whoami 401 must
          // not bring back a Log out for a store already refused.
          if (s.logged_in && s.creds_stamp !== loginStampRef.current) {
            // `loggingIn` drops only AFTER `refresh` wrote the vouched status:
            // dropping it first showed an enabled Sign in for the round trip.
            completing = true;
            setError(null);
            refresh()
              .catch((e) => !cancelled && setError((e as Error).message))
              .finally(() => {
                if (!cancelled) setLoggingIn(false);
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
      window.clearInterval(id);
    };
  }, [loggingIn, refresh]);

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
      await startLogin();
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
      await logout();
      await refresh();
    });

  const noCli = status && !status.cli_found;
  return (
    <SettingRow
      id="fused-account"
      status={
        !status ? (
          error ? undefined : "Checking…"
        ) : noCli ? (
          <>
            The fused CLI is not available in this server&rsquo;s environment. Install it with{" "}
            <code>pip install &quot;fused-render[fused]&quot;</code> or set <code>FUSED_RENDER_FUSED_BIN</code>.
          </>
        ) : loggingIn ? (
          "Complete the sign-in in the browser window that just opened."
        ) : status.logged_in ? (
          <span className="settings-dot settings-dot-ok">Signed in{handle ? <> as <b>{handle}</b></> : null}</span>
        ) : (
          "Not signed in."
        )
      }
      control={
        !status || noCli ? undefined : loggingIn ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void onCancel()}>
            Cancel
          </Button>
        ) : status.logged_in ? (
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
          <Button variant="accent" size="sm" disabled={busy} onClick={() => void onLogin()}>
            Sign in to Fused
          </Button>
        )
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

// The DEFAULT Fused environment (fusedcli.workbench_env): where the canvas
// catalog opens, what `fused` in a chat outside a canvas clone targets, and
// where share uploads go. A plain PUT /api/prefs: nothing has to stop first,
// because canvases no longer follow it — each keeps the env it was cloned
// from (its clone lives under ~/Fused/canvas/<env>), so a prod and an
// unstable canvas sync side by side whatever this says. Locked, with the
// reason, while FUSED_RENDER_WORKBENCH_ENV pins it (the server 423s a write).
export function WorkbenchEnvControl() {
  const [status, setStatus] = useState<CanvasesStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void getCanvasesStatus()
      .then((s) => alive && setStatus(s))
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  const onPick = async (env: WorkbenchEnv) => {
    if (!status || env === status.workbench_env) return;
    setBusy(true);
    setError(null);
    try {
      await putWorkbenchEnv(env);
      // Re-read rather than patch: the status is what the server resolved.
      setStatus(await getCanvasesStatus());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const current = status?.workbench_env ?? "";
  // A pin outside the choices (a hand-set env var) still shows as itself.
  const known = isWorkbenchEnv(current);
  return (
    <SettingRow
      id="workbench-env"
      status={
        !status ? (
          error ? undefined : "Checking…"
        ) : (
          <>
            {status.locked && (
              <div>
                Pinned to <b>{current}</b> by <code>FUSED_RENDER_WORKBENCH_ENV</code>.
              </div>
            )}
            {WORKBENCH_ENVS.map((e) => {
              const info = status.envs?.[e];
              if (!info) return null;
              const n = info.cloned_count;
              return (
                <div key={e}>
                  {`${ENV_LABELS[e]}: ${n} ${n === 1 ? "canvas" : "canvases"} cloned under ${info.canvases_dir}`}
                  {/* The one env whose workbench is not fixed: say where
                      settings.toml points it right now. */}
                  {e === "custom" && ` (workbench: ${info.base_url})`}
                </div>
              );
            })}
          </>
        )
      }
      control={
        !status ? undefined : (
          <SettingSelect
            id="workbench-env"
            value={known ? current : ""}
            options={[
              ...(known ? [] : [{ key: "", label: current }]),
              ...WORKBENCH_ENVS.map((e) => ({
                key: e,
                label: ENV_LABELS[e],
                disabled: status.envs?.[e]?.available === false,
              })),
            ]}
            disabled={busy || status.locked}
            onChange={(v) => {
              if (isWorkbenchEnv(v)) void onPick(v);
            }}
          />
        )
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

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
