// Canvases listing — the sub-app's front door (/canvases).
//
// Local development on legacy-workbench canvases: sign in with the CLI's
// `fused login` provider (distinct from the `fused cloud login` account the
// Preferences page manages), list the account's canvases as a card gallery
// (search, create, sign out), and open one — which clones it under
// ~/Fused/canvas/<env>/<name> and lands on the workspace page
// (/canvases/<env>/<name>: Claude-editable local files, watch-and-push sync,
// embedded live workbench). Styling lives in styles/canvases.css.
//
// The Environment dropdown picks WHICH environment's canvases are listed; a
// prod canvas and an unstable one can be open and syncing at once, so this is
// a view filter, not a switch. Its state is the `?env=` query (browser storage
// holds no facts, D1386), defaulting to the `workbench_env` preference — the
// preference is only that default now. FUSED_RENDER_WORKBENCH_ENV pins it
// (`status.locked`): the dropdown is disabled and the query ignored, since the
// server 423s any other env.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import previewTile from "@assets/canvas-preview-tile.png";
import { navigateUrl, replaceSearch } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  CanvasOwnedError,
  ENV_LABELS,
  WORKBENCH_ENVS,
  canvasWorkspacePath,
  catalogPath,
  cancelLogin,
  cloneCanvas,
  createCanvas,
  getCanvasesStatus,
  getCanvasPreviews,
  listCanvases,
  logout,
  loginOutcome,
  startLogin,
  startSync,
  isWorkbenchEnv,
  type CanvasEntry,
  type CanvasesStatus,
  type WorkbenchEnv,
} from "./api";
import { FusedSettingsModal } from "./FusedSettingsModal";

/** sync/start before opening a canvas. Another process owning the clone
 *  (409 `owned`) is not a reason to stay on the list: the workspace shows
 *  who owns it and picks the canvas up when that process quits. */
async function startSyncForOpen(env: WorkbenchEnv, name: string): Promise<void> {
  try {
    await startSync(env, name);
  } catch (e) {
    if (e instanceof CanvasOwnedError) return;
    throw e;
  }
}

/** The `?env=` the URL carries, if it names an environment; anything else
 *  falls back to the default rather than erroring. */
function envFromQuery(): WorkbenchEnv | null {
  const v = new URLSearchParams(location.search).get("env");
  return isWorkbenchEnv(v) ? v : null;
}

/** The environment the page lists: the pin when one is set (the server
 *  refuses every other env), else the URL's pick, else the default. Null when
 *  that is not one of the choices — FUSED_RENDER_WORKBENCH_ENV can pin a
 *  stg/dev/PR env the canvases have no folder or endpoint set for, and the
 *  page says so instead of listing under a name it cannot serve. */
function effectiveEnv(s: CanvasesStatus, picked: WorkbenchEnv | null): WorkbenchEnv | null {
  const fallback = isWorkbenchEnv(s.workbench_env) ? s.workbench_env : null;
  return s.locked ? fallback : (picked ?? fallback);
}

// `fused login`'s own browser callback times out server-side; polling any
// slower than this makes a completed sign-in feel stuck.
const LOGIN_POLL_MS = 1500;

// Same rule the server (and the CLI's push) enforces.
const NAME_RE = /^[A-Za-z0-9_]{1,128}$/;

// A canvas with no uploaded preview gets the hosted gallery's stand-in: one
// dark map tile per UDF, laid out in a grid, so the card still reads as a
// canvas of N things instead of an empty box. The tile is the workbench's own
// `preview_thumbnail_1.png` (fused-magic S3, main_marketing_website/), vendored
// into the bundle rather than hot-linked — this app runs locally and a card
// that needs the network to look right is a card that breaks offline.
const TILE_CAP = 16;

// The gallery's grid shapes, keyed by the layout each count rounds up into: 5
// tiles use the 6 layout with an empty cell, 7 uses the 8, and so on. Copied
// from the client's `getGridTemplateByCount` so the two gardens match.
type TileLayout = { gridTemplateColumns: string; gridTemplateRows: string };

const TILE_LAYOUTS: Record<number, TileLayout> = {
  1: { gridTemplateColumns: "1fr", gridTemplateRows: "1fr" },
  2: { gridTemplateColumns: "1fr 1fr", gridTemplateRows: "1fr" },
  3: { gridTemplateColumns: "1fr 1fr", gridTemplateRows: "1fr 1fr" },
  4: { gridTemplateColumns: "1fr 1fr", gridTemplateRows: "1fr 1fr" },
  6: { gridTemplateColumns: "1fr 1fr 1fr", gridTemplateRows: "1fr 1fr" },
  8: { gridTemplateColumns: "1fr 1fr 1fr 1fr", gridTemplateRows: "1fr 1fr" },
  9: { gridTemplateColumns: "1fr 1fr 1fr", gridTemplateRows: "1fr 1fr 1fr" },
  12: { gridTemplateColumns: "1fr 1fr 1fr 1fr", gridTemplateRows: "1fr 1fr 1fr" },
  15: {
    gridTemplateColumns: "1fr 1fr 1fr 1fr 1fr",
    gridTemplateRows: "1fr 1fr 1fr",
  },
  16: {
    gridTemplateColumns: "1fr 1fr 1fr 1fr",
    gridTemplateRows: "1fr 1fr 1fr 1fr",
  },
};

function tileLayout(count: number): TileLayout {
  const size = [1, 2, 3, 4, 6, 8, 9, 12, 15, 16].find((n) => n >= count) ?? 16;
  return TILE_LAYOUTS[size];
}

// Full locale date+time, seconds and four-digit year included — the same string
// the hosted workbench's gallery prints under a canvas name.
function formatModified(mtime: number): string {
  return new Date(mtime * 1000).toLocaleString();
}

export default function Canvases() {
  const [status, setStatus] = useState<CanvasesStatus | null>(null);
  const [canvases, setCanvases] = useState<CanvasEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // canvas being opened
  const [loggingIn, setLoggingIn] = useState(false);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false); // form visible
  const [newName, setNewName] = useState("");
  const [createBusy, setCreateBusy] = useState(false);
  // Preview URLs that failed to load (expired presigned URL, deleted asset) —
  // fall back to the monogram instead of a broken-image icon.
  const [brokenPreviews, setBrokenPreviews] = useState<Set<string>>(new Set());
  const pollRef = useRef<number | null>(null);
  // creds_stamp at the moment login started: a re-login over a stale-but-
  // present store never flips logged_in, so completion = the stamp changing.
  const loginStampRef = useRef<number | null | undefined>(undefined);
  // When this page started the browser sign-in: tells a child that died at
  // the server's deadline from one that ended early (refused, tab closed).
  const loginStartedRef = useRef<number | null>(null);
  // The dropdown's pick (null = none, use the default). Mirrored in a ref so
  // `refresh` reads the current pick without re-creating itself on a change.
  const [picked, setPicked] = useState<WorkbenchEnv | null>(envFromQuery);
  const pickedRef = useRef(picked);
  // Bumped on every environment change: a listing or preview batch that
  // departed under the old env must not land in the new env's page.
  const envGenRef = useRef(0);
  // The ~/.fused/settings.toml editor. It opens by itself the first time a
  // status says custom is picked and unconfigured (a dropdown pick or a
  // `?env=custom` load alike); a dismissal is remembered until the pick
  // changes, and the inline prompt reopens it on demand.
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsDismissedRef = useRef(false);
  const env = status ? effectiveEnv(status, picked) : null;

  // Presigned preview URLs, keyed by collection id — the second, slower half
  // of the listing (D364). Kept beside `canvases` rather than merged into it so
  // a refresh that re-lists doesn't drop thumbs we already resolved.
  const [previews, setPreviews] = useState<Record<string, string>>({});
  // Ids already asked about (resolved OR came back empty), so a re-list doesn't
  // re-sign what we have. A ref, not the `previews` state: reading the state
  // here would make `fillPreviews` — and through it `refresh` — a new function
  // on every thumb that lands, and refresh's own effect would re-run forever.
  const previewsAskedRef = useRef<Set<string>>(new Set());

  // Sign the pending previews for a listing that has already been painted.
  // Failures are silent by design: a card without a thumb shows its monogram,
  // which is exactly what a canvas with no preview shows anyway — not worth
  // an error banner over the listing that did load.
  const fillPreviews = useCallback(async (env: WorkbenchEnv, entries: CanvasEntry[]) => {
    const gen = envGenRef.current;
    const ids = entries
      .filter((c) => c.preview_pending && c.id)
      .map((c) => c.id as string)
      .filter((id) => !previewsAskedRef.current.has(id));
    if (ids.length === 0) return;
    for (const id of ids) previewsAskedRef.current.add(id);
    try {
      const { previews: signed } = await getCanvasPreviews(env, ids);
      if (gen !== envGenRef.current) return;
      const resolved: Record<string, string> = {};
      for (const [id, url] of Object.entries(signed)) {
        if (typeof url === "string" && url) resolved[id] = url;
      }
      if (Object.keys(resolved).length > 0) {
        setPreviews((prev) => ({ ...prev, ...resolved }));
      }
    } catch {
      // No thumbs this time; monograms stand in. Un-mark them so the next
      // refresh retries — a transient 502 shouldn't cost thumbs until reload.
      for (const id of ids) previewsAskedRef.current.delete(id);
    }
  }, []);

  const refresh = useCallback(async () => {
    const gen = envGenRef.current;
    try {
      // The PICKED env's login (custom has its own `[auth]`, D1412); with no
      // pick yet, the default env's — the same env `effectiveEnv` lands on.
      const s = await getCanvasesStatus(pickedRef.current);
      // Another env picked while this read was out: its answer describes
      // the old env's login, and the new pick's own refresh is on its way.
      if (gen !== envGenRef.current) return;
      setStatus(s);
      if (!s.logged_in && s.login_in_flight) {
        // This env's browser sign-in is still open — one `onPickEnv`
        // stopped watching on the way out, or one started elsewhere. Watch
        // it again, from the store as it is now (`login_in_flight` is
        // already per store on the server).
        loginStampRef.current = s.creds_stamp;
        // Started elsewhere, or before this page stopped watching: when it
        // began is unknown, and a stale stamp from an earlier sign-in would
        // call any early end a timeout.
        loginStartedRef.current = null;
        setLoggingIn(true);
      }
      if (s.logged_in) {
        setLoggingIn(false);
        const env = effectiveEnv(s, pickedRef.current);
        if (env === null) {
          // Nothing to list: the render below explains the unsupported pin.
          setError(null);
          return;
        }
        if (env === "custom" && s.envs?.custom?.available === false) {
          // An unconfigured custom run is a hosted-prod run under another
          // name (FUSED_ENV unset, no base_url) — and its "login" may well be
          // the shared hosted store. Listing would show prod canvases as
          // custom ones; the settings prompt stands in instead.
          setCanvases(null);
          setError(null);
          return;
        }
        const { canvases } = await listCanvases(env);
        if (gen !== envGenRef.current) return;
        setCanvases(canvases);
        // Deliberately NOT awaited: the cards render on the line above, and
        // the thumbs pop in when the signing batch lands.
        void fillPreviews(env, canvases);
      }
      setError(null);
    } catch (e) {
      if (gen !== envGenRef.current) return;
      const err = e as Error & { status?: number };
      // 401: the credentials file exists but is unrefreshable (the CLI says
      // re-authenticate) — show the sign-in flow, not a dead error page.
      if (err.status === 401) {
        setStatus((prev) => (prev ? { ...prev, logged_in: false } : prev));
        setError(`Your Fused sign-in expired — sign in again. (${err.message})`);
        return;
      }
      setError(err.message);
    }
  }, [fillPreviews]);

  // The dropdown: rewrite the query in place (no history entry — Back leaves
  // the catalog, it doesn't step through environments), drop everything the
  // old environment's listing held, and list the new one.
  const onPickEnv = (next: WorkbenchEnv) => {
    if (next === env) return;
    envGenRef.current += 1;
    pickedRef.current = next;
    setPicked(next);
    replaceSearch(catalogPath(next));
    setCanvases(null);
    // Collection ids and signed URLs belong to the environment they came from.
    setPreviews({});
    previewsAskedRef.current = new Set();
    setError(null);
    // Stop watching the old env's browser sign-in: its stamp is the old
    // env's store (custom's is its own, D1412), so judging the new env by it
    // would call the sign-in done or failed wrongly. The sign-in itself
    // carries on; switching back finds it in this env's status.
    setLoggingIn(false);
    void refresh();
  };

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Custom picked with nothing to point it at: open the editor rather than
  // offer a "deployment" sign-in that, with FUSED_ENV unset and no settings
  // file, would be a hosted-prod sign-in under another name. Gated on the
  // CLI too: with none installed the page says so, and an editor over that
  // message would be the wrong first thing to fix.
  const customUnconfigured =
    status?.cli_found === true &&
    env === "custom" &&
    status.envs?.custom?.available === false;
  useEffect(() => {
    if (!customUnconfigured) {
      settingsDismissedRef.current = false;
      return;
    }
    if (!settingsDismissedRef.current) setSettingsOpen(true);
  }, [customUnconfigured]);

  // While a login is in flight, poll status until logged_in flips — or the
  // browser child exits without ever flipping it (closed tab, denied, or the
  // flow otherwise failed), which must also drop `loggingIn` or the button
  // stays stuck on "Waiting for browser sign-in…" forever.
  useEffect(() => {
    if (!loggingIn) return;
    // A poll tick already in flight when this effect is torn down (e.g. a
    // deliberate sign-out) must not act on its result — clearInterval only
    // stops future ticks, not a request that's already on the wire.
    let cancelled = false;
    pollRef.current = window.setInterval(() => {
      // A tick already on the wire when the env changes answers for the old
      // env's login; drop it like `refresh` does.
      const gen = envGenRef.current;
      void getCanvasesStatus(pickedRef.current).then((s) => {
        if (cancelled || gen !== envGenRef.current) return;
        setStatus(s);
        const completed =
          s.logged_in && s.creds_stamp !== loginStampRef.current;
        if (completed) {
          setLoggingIn(false);
          setError(null);
          void refresh();
        } else if (!s.login_in_flight) {
          setLoggingIn(false);
          setError(loginOutcome(loginStartedRef.current));
        }
      });
    }, LOGIN_POLL_MS);
    return () => {
      cancelled = true;
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
  }, [loggingIn, refresh]);

  // Both continuations are dropped if the env changed while the request was
  // out: the sign-in started (a server-side child, not cancelled here) is
  // the OLD env's, and `onPickEnv` has already stopped watching it.
  const onLogin = async () => {
    setError(null);
    const gen = envGenRef.current;
    try {
      loginStampRef.current = status?.creds_stamp ?? null;
      await startLogin(env);
      if (gen !== envGenRef.current) return;
      loginStartedRef.current = Date.now();
      setLoggingIn(true);
    } catch (e) {
      if (gen !== envGenRef.current) return;
      setError((e as Error).message);
    }
  };

  // Drop `loggingIn` FIRST (the Settings row's order, for its reason): once the
  // child is terminated a tick already on the wire would see
  // `login_in_flight: false` and report "not completed" for a cancel the
  // reader asked for.
  const onCancelLogin = async () => {
    setLoggingIn(false);
    setError(null);
    try {
      await cancelLogin();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const onLogout = async () => {
    setError(null);
    // A deliberate sign-out during a stale-creds re-login must not let the
    // login poll's now-defunct login_in_flight read surface a spurious
    // "sign-in was not completed" error over this.
    setLoggingIn(false);
    const gen = envGenRef.current;
    try {
      await logout(env);
      // Signed out of the env picked then; a newer pick's listing stays, but
      // it is still re-read: prod and unstable share one store, so the new
      // pick's own refresh may have run before this cleared it.
      if (gen !== envGenRef.current) {
        await refresh();
        return;
      }
      setCanvases(null);
      // Signed URLs belong to the account that just signed out.
      setPreviews({});
      previewsAskedRef.current = new Set();
      setQuery("");
      setCreating(false);
      await refresh();
    } catch (e) {
      if (gen !== envGenRef.current) return;
      setError((e as Error).message);
    }
  };

  const onOpen = async (canvas: CanvasEntry) => {
    if (!env) return;
    setBusy(canvas.name);
    setError(null);
    try {
      // Clone only the first time: `pull --force` resets the folder to the
      // remote state, and an already-cloned canvas may hold local edits the
      // watcher hasn't pushed yet (e.g. after a server restart).
      if (!canvas.cloned) await cloneCanvas(env, canvas.name);
      await startSyncForOpen(env, canvas.name);
      navigateUrl(canvasWorkspacePath(env, canvas.name));
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  const onCreate = async () => {
    const name = newName.trim();
    if (!env) return;
    if (!NAME_RE.test(name)) {
      setError("Canvas names may only use letters, digits, and underscores.");
      return;
    }
    setCreateBusy(true);
    setError(null);
    try {
      await createCanvas(env, name);
      await cloneCanvas(env, name);
      await startSyncForOpen(env, name);
      navigateUrl(canvasWorkspacePath(env, name));
    } catch (e) {
      setError((e as Error).message);
      setCreateBusy(false);
    }
  };

  const filtered = useMemo(() => {
    if (canvases === null) return null;
    const q = query.trim().toLowerCase();
    const shown = q
      ? canvases.filter((c) => c.name.toLowerCase().includes(q))
      : canvases.slice();
    // Last-modified first (local clone mtime, else the server's last_updated);
    // canvases we know nothing about last, alphabetically.
    const modified = (c: CanvasEntry) => c.mtime ?? c.updated_at;
    shown.sort((a, b) => {
      const am = modified(a);
      const bm = modified(b);
      if (am !== null && bm !== null) return bm - am;
      if (am !== null) return -1;
      if (bm !== null) return 1;
      return a.name.localeCompare(b.name);
    });
    return shown;
  }, [canvases, query]);

  return (
    <div className="canvases-page">
      <div className="canvases-inner">
        <div className="canvases-head">
          <h1 className="canvases-title">Workbench Canvases</h1>
          {/* The picker shows signed out too: each env can be its own login
              (custom's `[auth]`, D1412), so a signed-out pick must still be
              able to switch back. The rest needs that env's login. */}
          {status?.cli_found && env && (
            <div className="canvases-head-actions">
              <label
                className="canvases-env"
                htmlFor="canvases-env"
                title={status.locked ? "Pinned by FUSED_RENDER_WORKBENCH_ENV" : undefined}
              >
                Environment
                <select
                  id="canvases-env"
                  className="field-control"
                  value={env}
                  disabled={status.locked || busy !== null || createBusy}
                  onChange={(e) => {
                    const v = e.target.value;
                    if (isWorkbenchEnv(v)) onPickEnv(v);
                  }}
                >
                  {/* Custom is never disabled here: an unconfigured one opens
                      the settings editor instead (customUnconfigured). */}
                  {WORKBENCH_ENVS.map((e) => (
                    <option key={e} value={e}>
                      {ENV_LABELS[e]}
                    </option>
                  ))}
                </select>
              </label>
              {status.logged_in && !customUnconfigured && (
                <>
                  <input
                    className="field-control canvases-search"
                    type="search"
                    placeholder="Search canvases"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                  {creating ? (
                    <form
                      className="canvases-new-form"
                      onSubmit={(e) => {
                        e.preventDefault();
                        void onCreate();
                      }}
                    >
                      <input
                        className="field-control"
                        autoFocus
                        placeholder="new_canvas_name"
                        value={newName}
                        // Spaces aren't legal in canvas names — typing one lands
                        // an underscore instead of silently disabling Create.
                        onChange={(e) => setNewName(e.target.value.replace(/\s+/g, "_"))}
                        disabled={createBusy}
                      />
                      <button
                        className="btn btn-primary"
                        type="submit"
                        disabled={createBusy || !NAME_RE.test(newName.trim())}
                      >
                        {createBusy ? "Creating…" : "Create"}
                      </button>
                      <button
                        className="btn"
                        type="button"
                        onClick={() => {
                          setCreating(false);
                          setNewName("");
                        }}
                        disabled={createBusy}
                      >
                        Cancel
                      </button>
                    </form>
                  ) : (
                    <button className="btn btn-primary" onClick={() => setCreating(true)}>
                      + New canvas
                    </button>
                  )}
                  <span className="canvases-account">
                    <button className="btn" onClick={() => void onLogout()}>
                      Sign out
                    </button>
                  </span>
                </>
              )}
            </div>
          )}
        </div>
        <p className="canvases-sub">
          Develop workbench canvases locally: pick a canvas, edit its files with
          Claude Code, and every save is pushed back to the hosted workbench.
        </p>
        {error && <ErrorBanner>{error}</ErrorBanner>}
        {status && !status.cli_found && (
          <p>
            The fused CLI is not available in this server&rsquo;s environment.
            Install it with <code>pip install &quot;fused-render[fused]&quot;</code>{" "}
            or set <code>FUSED_RENDER_FUSED_BIN</code>.
          </p>
        )}
        {status && status.cli_found && customUnconfigured && (
          <div className="canvases-settings-prompt">
            <p className="canvases-sub">
              The custom environment follows <code>{status.settings_path}</code>, which
              does not name a Fused deployment yet. Paste your deployment&rsquo;s config to
              continue.
            </p>
            <button
              className="btn btn-primary"
              onClick={() => {
                settingsDismissedRef.current = false;
                setSettingsOpen(true);
              }}
            >
              Add settings.toml
            </button>
          </div>
        )}
        {status && status.cli_found && !status.logged_in && !customUnconfigured && (
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <button className="btn btn-primary" onClick={onLogin} disabled={loggingIn}>
              {loggingIn
                ? "Waiting for browser sign-in…"
                : env === "custom"
                  ? "Sign in to your Fused deployment"
                  : "Sign in to Fused"}
            </button>
            {loggingIn && (
              <>
                <button className="btn" onClick={() => void onCancelLogin()}>
                  Cancel
                </button>
                <span className="canvases-sub">
                  Complete the sign-in in the browser window that just opened.
                </span>
              </>
            )}
          </div>
        )}
        {status?.logged_in && env === null && !customUnconfigured && (
          <p className="canvases-empty">
            {status.locked
              ? `Canvases are pinned to ${status.workbench_env} by FUSED_RENDER_WORKBENCH_ENV, which is not one of ${WORKBENCH_ENVS.join("/")}.`
              : `The default environment ${status.workbench_env} is not one of ${WORKBENCH_ENVS.join("/")}.`}
          </p>
        )}
        {status?.logged_in && env && !customUnconfigured && filtered === null && !error && <p>Loading canvases…</p>}
        {status?.logged_in && env && !customUnconfigured && filtered !== null && filtered.length === 0 && (
          <p className="canvases-empty">
            {query
              ? "No canvases match your search."
              : "No canvases in this account yet — create one to get started."}
          </p>
        )}
        {status?.logged_in && env && !customUnconfigured && filtered !== null && filtered.length > 0 && (
          <div className="canvases-grid">
            {filtered.map((canvas) => {
              // Either the free public URL from the listing, or the signed one
              // the previews batch filled in afterwards (D364).
              const thumb =
                canvas.preview_url ?? (canvas.id ? previews[canvas.id] : undefined) ?? null;
              // Local clone mtime when we have one, else the control plane's
              // last_updated — the same expression the sort above orders by.
              const modified = canvas.mtime ?? canvas.updated_at;
              // The clone's own *.py count wins when we have one (it sees local
              // edits the workbench hasn't been pushed yet); otherwise the
              // listing's count, which exists for every canvas in the account.
              const nUdfs = canvas.n_udfs ?? canvas.n_code_udfs ?? null;
              // An account whose listing predates the count field (or came from
              // the bare-name CLI fallback) still gets a map rather than an
              // empty box — one tile, standing for "a canvas", not for a count.
              const tiles = nUdfs === null ? 1 : Math.min(nUdfs, TILE_CAP);
              return (
              <button
                key={canvas.name}
                className="canvas-card"
                onClick={() => void onOpen(canvas)}
                disabled={busy !== null || createBusy}
              >
                <span className="canvas-card-thumb">
                  {thumb && !brokenPreviews.has(thumb) ? (
                    <img
                      className="canvas-card-img"
                      src={thumb}
                      alt=""
                      loading="lazy"
                      onError={() =>
                        setBrokenPreviews((prev) => {
                          const next = new Set(prev);
                          next.add(thumb);
                          return next;
                        })
                      }
                    />
                  ) : tiles > 0 ? (
                    <span className="canvas-card-tiles" style={tileLayout(tiles)}>
                      {Array.from({ length: tiles }, (_, i) => (
                        <img
                          key={i}
                          className="canvas-card-tile"
                          src={previewTile}
                          alt=""
                        />
                      ))}
                    </span>
                  ) : (
                    <span className="canvas-card-noshot">
                      No UDFs present in the canvas
                    </span>
                  )}
                </span>
                <span className="canvas-card-body">
                  <span className="canvas-card-name" title={canvas.name}>
                    {canvas.name}
                  </span>
                  <span className="canvas-card-stat">
                    {busy === canvas.name
                      ? "Cloning…"
                      : nUdfs === null
                        ? "Not cloned yet — click to clone & open"
                        : `${nUdfs} UDF${nUdfs === 1 ? "" : "s"}${
                            // The count now exists before the clone does, but
                            // an uncloned card still needs to say what a click
                            // will do — it is the only affordance it has.
                            canvas.cloned ? "" : " · click to clone & open"
                          }`}
                  </span>
                  {modified !== null && (
                    <span className="canvas-card-meta">
                      Last modified: {formatModified(modified)}
                    </span>
                  )}
                </span>
              </button>
              );
            })}
          </div>
        )}
      </div>
      {settingsOpen && (
        <FusedSettingsModal
          onClose={() => {
            settingsDismissedRef.current = true;
            setSettingsOpen(false);
          }}
          onSaved={() => {
            setSettingsOpen(false);
            setError(null);
            void refresh();
          }}
        />
      )}
    </div>
  );
}
