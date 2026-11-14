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
import { navigateUrl, replaceSearch } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Modal } from "@platform/ui/modal/Modal";
import { Loader2, RefreshCw } from "lucide-react";
import {
  generateCanvasName,
  sanitizeCanvasName,
  validateCanvasName,
} from "./canvas-name";
import {
  CanvasOwnedError,
  ENV_NAMES,
  WORKBENCH_ENVS,
  canvasWorkspacePath,
  catalogPath,
  cancelLogin,
  cloneCanvas,
  createCanvas,
  deleteCanvas,
  deleteFusedSettings,
  getCanvasesStatus,
  getCanvasFavourites,
  getCanvasPreviews,
  listCanvases,
  logout,
  loginOutcome,
  startLogin,
  startSync,
  isWorkbenchEnv,
  setCanvasFavourite,
  type CanvasEntry,
  type CanvasesStatus,
  type WorkbenchEnv,
} from "./api";
import { CanvasCard, canvasDisplayName } from "./CanvasCard";
import { FusedSettingsModal } from "./FusedSettingsModal";
import { LayoutNotice, MirrorChip, useReferenceMirror } from "./MirrorStatus";

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

export default function Canvases() {
  const [status, setStatus] = useState<CanvasesStatus | null>(null);
  const [canvases, setCanvases] = useState<CanvasEntry[] | null>(null);
  // True while the list on screen is the disk baseline and the fresh one is
  // still on its way.
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // canvas being opened
  const [loggingIn, setLoggingIn] = useState(false);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false); // form visible
  const [newName, setNewName] = useState("");
  // The line under the name box. Two sources, kept apart because only one
  // gates Create: a rule the typed name breaks (`nameError`, per keystroke,
  // disables Create) and the create call's failure (`submitError`, leaves
  // Create on so the same name can be retried). Separate from `error` (the
  // page banner) so a typo never paints a banner over the listing.
  const [nameError, setNameError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const newNameRef = useRef<HTMLInputElement | null>(null);
  // The form mounts with the suggestion selected: Enter keeps it, typing
  // replaces it (as the workbench's modal). Once per open, not per keystroke.
  useEffect(() => {
    if (creating) newNameRef.current?.select();
  }, [creating]);
  // The account's favourite collection ids (the hosted home's star, same
  // control-plane list). Null until read — or when the read failed — and the
  // cards hide their star rather than show every canvas as unstarred.
  const [favourites, setFavourites] = useState<Set<string> | null>(null);
  // Bumped when a toggle starts: drops favourites reads that left before it.
  const favSeqRef = useRef(0);
  // Ids with a star write on the wire: a second press waits it out, so a PUT
  // and a DELETE for one canvas never race (and a failed first write cannot
  // roll back a second one).
  const favInFlightRef = useRef<Set<string>>(new Set());
  // The canvas whose card's trash was pressed: the confirm dialog's target.
  const [deleteTarget, setDeleteTarget] = useState<CanvasEntry | null>(null);
  const [deleteCanvasBusy, setDeleteCanvasBusy] = useState(false);
  const [deleteCanvasErr, setDeleteCanvasErr] = useState("");
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
  // Sequence of listing requests within an env generation: see `refresh`.
  const listSeqRef = useRef(0);
  // The ~/.fused/settings.toml editor. It opens by itself the first time a
  // status says custom is picked and unconfigured (a dropdown pick or a
  // `?env=custom` load alike); a dismissal is remembered until the pick
  // changes, and the inline prompt reopens it on demand.
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsDismissedRef = useRef(false);
  // "Delete config" is two presses: the first arms it in place (the second
  // button's words say what goes), the next deletes. No `confirm()`.
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const env = status ? effectiveEnv(status, picked) : null;
  // The reference canvases of the env on show: the head's chip, and the
  // notice for clones the folder-layout move left behind.
  const referenceMirror = useReferenceMirror(env, status, setError);

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

  // Read the favourites alongside the listing, never in front of it. A
  // failure just leaves the stars hidden (an external CLI has no shim; an old
  // control plane has no endpoint) — not worth a banner over the cards.
  const fillFavourites = useCallback(async (env: WorkbenchEnv) => {
    const gen = envGenRef.current;
    // A read that left before a toggle answers with the list as it was: it
    // must not land over the toggle (favSeqRef moves on every toggle).
    const seq = favSeqRef.current;
    try {
      const { collection_ids } = await getCanvasFavourites(env);
      if (gen !== envGenRef.current || seq !== favSeqRef.current) return;
      setFavourites(new Set(collection_ids));
    } catch {
      if (gen !== envGenRef.current || seq !== favSeqRef.current) return;
      setFavourites(null);
    }
  }, []);

  const refresh = useCallback(async () => {
    const gen = envGenRef.current;
    // Two list reads for the SAME env can overlap (a settings save, a delete,
    // the header button): the env generation does not move, so the older
    // one's answers — above all its slow fresh one — must not land over the
    // newer one's. Only the latest sequence writes. Taken HERE, before the
    // status read, so an older fresh answer cannot land in that window (a
    // just-deleted canvas popping back). The spinner goes with it: its owner
    // is now superseded and may no longer clear it.
    const seq = ++listSeqRef.current;
    setRefreshing(false);
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
        const { canvases, stale } = await listCanvases(env);
        if (gen !== envGenRef.current || seq !== listSeqRef.current) return;
        setCanvases(canvases);
        // Deliberately NOT awaited: the cards render on the line above, and
        // the thumbs (and stars) pop in when their batches land.
        void fillPreviews(env, canvases);
        void fillFavourites(env);
        if (stale) {
          // The baseline painted; now the real list (stale-while-revalidate,
          // like the workbench home). Same generation check: an env switch
          // while this was out must not write the old env's list.
          setRefreshing(true);
          try {
            const second = await listCanvases(env, true);
            if (gen !== envGenRef.current || seq !== listSeqRef.current) return;
            setCanvases(second.canvases);
            void fillPreviews(env, second.canvases);
          } finally {
            // Only the refresh that is still current may clear the hint: a
            // newer one may have set it for its own fresh request.
            if (gen === envGenRef.current && seq === listSeqRef.current) {
              setRefreshing(false);
            }
          }
        }
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
  }, [fillPreviews, fillFavourites]);

  // The header's refresh button: straight to the control plane (`fresh`),
  // the cards on screen stay live under the spinner. Takes a list sequence
  // like `refresh`, so a background revalidation still out cannot land over
  // this answer, and a later refresh or env switch drops this one.
  const onRevalidate = async () => {
    if (!env || refreshing) return;
    const gen = envGenRef.current;
    const seq = ++listSeqRef.current;
    setRefreshing(true);
    try {
      const { canvases: next } = await listCanvases(env, true);
      if (gen !== envGenRef.current || seq !== listSeqRef.current) return;
      setCanvases(next);
      setError(null);
      void fillPreviews(env, next);
      void fillFavourites(env);
    } catch (e) {
      if (gen !== envGenRef.current || seq !== listSeqRef.current) return;
      const err = e as Error & { status?: number };
      // Same as `refresh`: an unrefreshable sign-in shows the sign-in flow.
      if (err.status === 401) {
        setStatus((prev) => (prev ? { ...prev, logged_in: false } : prev));
        setError(`Your Fused sign-in expired — sign in again. (${err.message})`);
        return;
      }
      setError(err.message);
    } finally {
      if (gen === envGenRef.current && seq === listSeqRef.current) {
        setRefreshing(false);
      }
    }
  };

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
    setRefreshing(false);
    // Collection ids and signed URLs belong to the environment they came from.
    setPreviews({});
    previewsAskedRef.current = new Set();
    setFavourites(null);
    setError(null);
    // So does the new-canvas form: its suggestion and its duplicate verdict
    // were judged against the old env's listing.
    closeCreate();
    // Stop watching the old env's browser sign-in: its stamp is the old
    // env's store (custom's is its own, D1412), so judging the new env by it
    // would call the sign-in done or failed wrongly. The sign-in itself
    // carries on; switching back finds it in this env's status.
    setLoggingIn(false);
    setDeleteArmed(false);
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

  // Remove ~/.fused/settings.toml. The page then shows the inline prompt,
  // not the editor: a reader who just deleted a config did not ask to write
  // one, so the auto-open is suppressed for this pick.
  const onDeleteSettings = async () => {
    if (deleteBusy) return;
    setDeleteBusy(true);
    setError(null);
    try {
      await deleteFusedSettings();
      settingsDismissedRef.current = true;
      setDeleteArmed(false);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeleteBusy(false);
    }
  };

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
      // A fresh listing still in flight for the old account must not leave
      // its hint on the signed-out page.
      setRefreshing(false);
      // Signed URLs (and stars) belong to the account that just signed out.
      setPreviews({});
      previewsAskedRef.current = new Set();
      setFavourites(null);
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

  // The card's "Open canvas in new tab": a cloned canvas's workspace opens
  // as-is; an uncloned one is cloned (and its sync armed) first, since that
  // page cannot open a canvas with no folder. If the window cannot be opened
  // after the clone's await (a popup policy that wants a fresh gesture), the
  // canvas opens here instead — the clone is done either way.
  const onOpenInNewTab = async (canvas: CanvasEntry) => {
    if (!env) return;
    const path = canvasWorkspacePath(env, canvas.name);
    // A blocked window (null) opens the canvas here instead. No "noopener"
    // feature: with it window.open always returns null and a block could not
    // be told apart; the opener link is cut on the handle instead.
    const openWindow = () => {
      const w = window.open(path, "_blank");
      if (w) w.opener = null;
      else navigateUrl(path);
      return w !== null;
    };
    if (canvas.cloned) {
      openWindow();
      return;
    }
    const gen = envGenRef.current;
    setBusy(canvas.name);
    setError(null);
    try {
      await cloneCanvas(env, canvas.name);
      await startSyncForOpen(env, canvas.name);
      // The env changed while the clone was out: the listing on screen is
      // another env's, whose same-named canvas this clone says nothing about.
      if (gen !== envGenRef.current) return;
      setCanvases((prev) =>
        prev ? prev.map((c) => (c.name === canvas.name ? { ...c, cloned: true } : c)) : prev,
      );
      if (!openWindow()) return;
    } catch (e) {
      if (gen !== envGenRef.current) return;
      setError((e as Error).message);
    }
    setBusy(null);
  };

  // Optimistic, rolled back on failure — the hosted star's behaviour. One
  // write per canvas at a time (favInFlightRef); a press while one is out is
  // ignored, which is also what the user sees (the star already flipped).
  const onToggleFavourite = async (canvas: CanvasEntry) => {
    if (!env || !canvas.id || favourites === null) return;
    const id = canvas.id;
    if (favInFlightRef.current.has(id)) return;
    favInFlightRef.current.add(id);
    favSeqRef.current += 1;
    const gen = envGenRef.current;
    const on = !favourites.has(id);
    const flip = (want: boolean) =>
      setFavourites((prev) => {
        if (prev === null) return prev;
        const next = new Set(prev);
        if (want) next.add(id);
        else next.delete(id);
        return next;
      });
    flip(on);
    try {
      await setCanvasFavourite(env, id, on);
    } catch (e) {
      if (gen !== envGenRef.current) return;
      flip(!on);
      setError(`${on ? "Adding" : "Removing"} favourite failed: ${(e as Error).message}`);
    } finally {
      favInFlightRef.current.delete(id);
    }
  };

  // The confirmed delete: server AND this machine (the clone folder goes too).
  // The server drops its listing cache, so the re-list is the truth.
  const onConfirmDelete = async () => {
    if (!env || !deleteTarget || deleteCanvasBusy) return;
    setDeleteCanvasBusy(true);
    setDeleteCanvasErr("");
    try {
      await deleteCanvas(env, deleteTarget.name);
      const gone = deleteTarget.name;
      setCanvases((prev) => (prev ? prev.filter((c) => c.name !== gone) : prev));
      setDeleteTarget(null);
      void refresh();
    } catch (e) {
      // 409 `owned` (another fused-render process syncs the clone) and CLI
      // failures alike: the server's sentence, inside the dialog.
      setDeleteCanvasErr((e as Error).message);
    } finally {
      setDeleteCanvasBusy(false);
    }
  };

  // The share link's origin: the env's workbench (`base_url`), as the hosted
  // card's `getPublicOrigin()`.
  const shareOrigin = useMemo(() => {
    const base = env && status?.envs?.[env]?.base_url;
    if (!base) return null;
    try {
      return new URL(base).origin;
    } catch {
      return null;
    }
  }, [env, status]);

  // Names the duplicate rule checks against: the LIVE listing, so a canvas
  // that appears while the box is open (poll, another session) is caught by
  // onCreate's re-check even if the keystroke validation missed it.
  // null until the listing has loaded: there is no duplicate rule to apply
  // against nothing, so the form stays closed (button disabled) rather than
  // suggesting `canvas_1` and letting a taken name through.
  const existingNames = useMemo(
    () => (canvases === null ? null : canvases.map((c) => c.name)),
    [canvases],
  );

  // The listing moves under an open form (poll, env switch, a canvas created
  // elsewhere): re-judge the typed name against the names as they are now.
  useEffect(() => {
    if (!creating) return;
    setNameError(
      existingNames === null
        ? "Loading canvases…"
        : validateCanvasName(newName, existingNames),
    );
    // newName is judged on every keystroke already (onNewNameChange).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creating, existingNames]);

  const openCreate = () => {
    if (existingNames === null) return;
    // The workbench prefills `canvas_<n>` and selects it, so Enter alone
    // creates and typing replaces.
    const suggested = generateCanvasName(existingNames);
    setNewName(suggested);
    setNameError(validateCanvasName(suggested, existingNames));
    setSubmitError(null);
    setCreating(true);
  };

  const closeCreate = () => {
    setCreating(false);
    setNewName("");
    setNameError(null);
    setSubmitError(null);
  };

  const onNewNameChange = (raw: string) => {
    const name = sanitizeCanvasName(raw);
    setNewName(name);
    setNameError(
      existingNames === null
        ? "Loading canvases…"
        : validateCanvasName(name, existingNames),
    );
    setSubmitError(null);
  };

  const onCreate = async () => {
    const name = newName.trim();
    if (!env || existingNames === null) return;
    const invalid = validateCanvasName(name, existingNames);
    if (invalid) {
      setNameError(invalid);
      return;
    }
    setCreateBusy(true);
    setSubmitError(null);
    setError(null);
    try {
      await createCanvas(env, name);
      await cloneCanvas(env, name);
      await startSyncForOpen(env, name);
      navigateUrl(canvasWorkspacePath(env, name));
    } catch (e) {
      setSubmitError((e as Error).message);
      setCreateBusy(false);
    }
  };

  const createError = nameError ?? submitError;

  const filtered = useMemo(() => {
    if (canvases === null) return null;
    // Cards print names with spaces for underscores: match in that form, so a
    // query typed from the title ("map query") and one typed from the folder
    // name ("map_query") both find the canvas.
    const q = canvasDisplayName(query.trim().toLowerCase());
    const shown = q
      ? canvases.filter((c) => canvasDisplayName(c.name.toLowerCase()).includes(q))
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
        <div className={"canvases-head" + (creating ? " is-creating" : "")}>
          <h1 className="canvases-title">Workbench Canvases</h1>
          {/* The disk baseline is on screen while the fresh listing is in
              flight (stale-while-revalidate): a small non-blocking spinner,
              never a scrim — the stale cards stay usable. */}
          {refreshing ? (
            <span
              className="canvases-refreshing"
              role="status"
              aria-label="Refreshing the list"
              title="Refreshing the list…"
            >
              <Loader2 className="canvases-refreshing-icon" aria-hidden />
            </span>
          ) : (
            // Idle: the same spot is the on-demand refetch.
            status?.logged_in &&
            !customUnconfigured &&
            canvases !== null && (
              <button
                type="button"
                className="canvases-refresh"
                aria-label="Refresh the list"
                title="Refresh the list"
                onClick={() => void onRevalidate()}
              >
                <RefreshCw className="canvases-refresh-icon" aria-hidden />
              </button>
            )
          )}
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
                      {ENV_NAMES[e].label}
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
                      <div className="canvases-new-row">
                        <input
                          ref={newNameRef}
                          className="field-control canvases-new-name"
                          autoFocus
                          placeholder="new_canvas_name"
                          value={newName}
                          onChange={(e) => onNewNameChange(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Escape" && !createBusy) {
                              e.preventDefault();
                              closeCreate();
                            }
                          }}
                          disabled={createBusy}
                          aria-invalid={createError ? "true" : undefined}
                          aria-describedby={createError ? "canvases-new-error" : undefined}
                        />
                        <button
                          className="btn btn-primary"
                          type="submit"
                          disabled={createBusy || !!nameError}
                        >
                          {createBusy ? "Creating…" : "Create"}
                        </button>
                        <button
                          className="btn"
                          type="button"
                          onClick={closeCreate}
                          disabled={createBusy}
                        >
                          Cancel
                        </button>
                      </div>
                      {createError && (
                        <div
                          id="canvases-new-error"
                          className="canvases-new-error"
                          title={createError}
                        >
                          {createError}
                        </div>
                      )}
                    </form>
                  ) : (
                    <button
                      className="btn btn-primary"
                      onClick={openCreate}
                      disabled={existingNames === null}
                    >
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
        {/* Ahead of the subtitle it floats beside: the head's own row is full
            at ordinary window widths (the title already wraps there). */}
        <MirrorChip
          mirror={referenceMirror.mirror}
          starting={referenceMirror.starting}
          onRun={() => void referenceMirror.run()}
        />
        <p className="canvases-sub">
          Develop workbench canvases locally: pick a canvas, edit its files with
          Claude Code, and every save is pushed back to the hosted workbench.
        </p>
        {error && <ErrorBanner>{error}</ErrorBanner>}
        <LayoutNotice layout={referenceMirror.layout} />
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
            {env === "custom" && !loggingIn && (
              deleteArmed ? (
                <>
                  <span className="canvases-sub">
                    Delete <code>{status.settings_path}</code>?
                  </span>
                  <button
                    className="btn btn-danger"
                    onClick={() => void onDeleteSettings()}
                    disabled={deleteBusy}
                  >
                    {deleteBusy ? "Deleting…" : "Yes, delete"}
                  </button>
                  <button className="btn" onClick={() => setDeleteArmed(false)} disabled={deleteBusy}>
                    Cancel
                  </button>
                </>
              ) : (
                <button className="btn" onClick={() => setDeleteArmed(true)}>
                  Delete config
                </button>
              )
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
            {filtered.map((canvas) => (
              <CanvasCard
                key={canvas.name}
                canvas={canvas}
                href={canvasWorkspacePath(env, canvas.name)}
                // Either the free public URL from the listing, or the signed
                // one the previews batch filled in afterwards (D364).
                thumb={
                  canvas.preview_url ?? (canvas.id ? previews[canvas.id] : undefined) ?? null
                }
                loading={busy === canvas.name}
                disabled={(busy !== null && busy !== canvas.name) || createBusy}
                isFavourite={
                  canvas.id && favourites !== null ? favourites.has(canvas.id) : undefined
                }
                shareUrl={
                  canvas.share_token && shareOrigin
                    ? `${shareOrigin}/canvas/${encodeURIComponent(canvas.share_token)}`
                    : null
                }
                onOpen={() => void onOpen(canvas)}
                onOpenInNewTab={() => void onOpenInNewTab(canvas)}
                onDelete={() => {
                  setDeleteCanvasErr("");
                  setDeleteTarget(canvas);
                }}
                onToggleFavourite={() => void onToggleFavourite(canvas)}
              />
            ))}
          </div>
        )}
      </div>
      {deleteTarget && (
        <Modal
          title={`Delete ${canvasDisplayName(deleteTarget.name)}?`}
          busy={deleteCanvasBusy}
          onClose={() => {
            if (!deleteCanvasBusy) setDeleteTarget(null);
          }}
          footer={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={deleteCanvasBusy}
                onClick={() => setDeleteTarget(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                aria-disabled={deleteCanvasBusy}
                onClick={() => void onConfirmDelete()}
              >
                {deleteCanvasBusy ? "Deleting…" : "Delete canvas"}
              </button>
            </>
          }
        >
          <p>
            This deletes the canvas from your Fused account
            {deleteTarget.cloned ? " and removes its local folder on this machine" : ""}.
          </p>
          <p>
            <b>This is permanent and cannot be undone.</b>
          </p>
          {deleteCanvasErr && (
            <p className="deploy-error" role="alert">
              {deleteCanvasErr}
            </p>
          )}
        </Modal>
      )}
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
