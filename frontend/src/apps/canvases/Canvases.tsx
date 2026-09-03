// Canvases listing — the sub-app's front door (/canvases).
//
// Local development on legacy-workbench canvases: sign in with the CLI's
// `fused login` provider (distinct from the `fused cloud login` account the
// Preferences page manages), list the account's canvases as a card gallery
// (search, create, sign out), and open one — which clones it under
// ~/.fused-render/canvases/<name> and lands on the workspace page
// (/canvases/<name>: Claude-editable local files, watch-and-push sync,
// embedded live workbench). Tailwind + shadcn primitives + the flow
// composites; the gallery tile itself is ./CanvasCard.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LayoutGrid, Plus } from "lucide-react";
import { navigateUrl } from "@platform/lib/router";
import { Alert, AlertDescription } from "@platform/shadcn/ui/alert";
import { Button } from "@platform/shadcn/ui/button";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@platform/shadcn/ui/empty";
import { Input } from "@platform/shadcn/ui/input";
import { Muted, Page, PageBody, PageHeader } from "@platform/ui/flow/Typography";
import {
  cloneCanvas,
  createCanvas,
  getCanvasesStatus,
  getCanvasPreviews,
  listCanvases,
  logout,
  startLogin,
  startSync,
  type CanvasEntry,
  type CanvasesStatus,
} from "./api";
import { CanvasCard } from "./CanvasCard";
import { publishLoggedIn } from "./logged-in";

// `fused login`'s own browser callback times out server-side; polling any
// slower than this makes a completed sign-in feel stuck.
const LOGIN_POLL_MS = 1500;

// Same rule the server (and the CLI's push) enforces.
const NAME_RE = /^[A-Za-z0-9_]{1,128}$/;

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
  const fillPreviews = useCallback(async (entries: CanvasEntry[]) => {
    const ids = entries
      .filter((c) => c.preview_pending && c.id)
      .map((c) => c.id as string)
      .filter((id) => !previewsAskedRef.current.has(id));
    if (ids.length === 0) return;
    for (const id of ids) previewsAskedRef.current.add(id);
    try {
      const { previews: signed } = await getCanvasPreviews(ids);
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
    try {
      const s = await getCanvasesStatus();
      setStatus(s);
      if (s.logged_in) {
        setLoggingIn(false);
        const { canvases } = await listCanvases();
        setCanvases(canvases);
        // Deliberately NOT awaited: the cards render on the line above, and
        // the thumbs pop in when the signing batch lands.
        void fillPreviews(canvases);
      }
      setError(null);
    } catch (e) {
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

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // The sidebar's Canvases row reads the same fact this page does, so
  // hand it every status this page learns — the first read, the login poll's
  // flip, the 401 downgrade, the sign-out — rather than leaving it to notice on
  // its own minute-long poll. The whole status goes over, not just the boolean:
  // the 401 downgrade below is a verdict on a SPECIFIC credentials store, and
  // `creds_stamp` is what names it (see ./logged-in).
  useEffect(() => {
    if (status) publishLoggedIn(status);
  }, [status]);

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
      void getCanvasesStatus().then((s) => {
        if (cancelled) return;
        setStatus(s);
        const completed =
          s.logged_in && s.creds_stamp !== loginStampRef.current;
        if (completed) {
          setLoggingIn(false);
          setError(null);
          void refresh();
        } else if (!s.login_in_flight) {
          setLoggingIn(false);
          setError("Sign-in was not completed — try again.");
        }
      });
    }, LOGIN_POLL_MS);
    return () => {
      cancelled = true;
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
  }, [loggingIn, refresh]);

  const onLogin = async () => {
    setError(null);
    try {
      loginStampRef.current = status?.creds_stamp ?? null;
      await startLogin();
      setLoggingIn(true);
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
    try {
      await logout();
      setCanvases(null);
      // Signed URLs belong to the account that just signed out.
      setPreviews({});
      previewsAskedRef.current = new Set();
      setQuery("");
      setCreating(false);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const onOpen = async (canvas: CanvasEntry) => {
    setBusy(canvas.name);
    setError(null);
    try {
      // Clone only the first time: `pull --force` resets the folder to the
      // remote state, and an already-cloned canvas may hold local edits the
      // watcher hasn't pushed yet (e.g. after a server restart).
      if (!canvas.cloned) await cloneCanvas(canvas.name);
      await startSync(canvas.name);
      navigateUrl(`/canvases/${encodeURIComponent(canvas.name)}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  const onCreate = async () => {
    const name = newName.trim();
    if (!NAME_RE.test(name)) {
      setError("Canvas names may only use letters, digits, and underscores.");
      return;
    }
    setCreateBusy(true);
    setError(null);
    try {
      await createCanvas(name);
      await cloneCanvas(name);
      await startSync(name);
      navigateUrl(`/canvases/${encodeURIComponent(name)}`);
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

  const canCreate = NAME_RE.test(newName.trim());

  // Header actions exist only once signed in; the gate below the header is
  // the whole page until then.
  const actions = status?.logged_in ? (
    <>
      <Input
        className="w-56"
        type="search"
        placeholder="Search canvases"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {creating ? (
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void onCreate();
          }}
        >
          <Input
            className="w-48"
            autoFocus
            placeholder="new_canvas_name"
            value={newName}
            // Spaces aren't legal in canvas names — typing one lands an
            // underscore instead of silently disabling Create.
            onChange={(e) => setNewName(e.target.value.replace(/\s+/g, "_"))}
            disabled={createBusy}
          />
          <Button type="submit" disabled={createBusy || !canCreate}>
            {createBusy ? "Creating…" : "Create"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              setCreating(false);
              setNewName("");
            }}
            disabled={createBusy}
          >
            Cancel
          </Button>
        </form>
      ) : (
        <Button onClick={() => setCreating(true)}>
          <Plus />
          New canvas
        </Button>
      )}
      <Button variant="outline" onClick={() => void onLogout()}>
        Sign out
      </Button>
    </>
  ) : undefined;

  return (
    <Page>
      <PageHeader
        title="Workbench Canvases"
        description="Develop workbench canvases locally: pick a canvas, edit its files with Claude Code, and every save is pushed back to the hosted workbench."
        actions={actions}
      />
      <PageBody>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {status && !status.cli_found && (
          <Muted>
            The fused CLI is not available in this server&rsquo;s environment. Install it
            with{" "}
            <code className="font-mono text-xs text-foreground">
              pip install &quot;fused-render[fused]&quot;
            </code>{" "}
            or set <code className="font-mono text-xs text-foreground">FUSED_RENDER_FUSED_BIN</code>.
          </Muted>
        )}
        {status && status.cli_found && !status.logged_in && (
          <div className="flex items-center gap-3">
            <Button onClick={onLogin} disabled={loggingIn}>
              {loggingIn ? "Waiting for browser sign-in…" : "Sign in to Fused"}
            </Button>
            {loggingIn && (
              <Muted>Complete the sign-in in the browser window that just opened.</Muted>
            )}
          </div>
        )}
        {status?.logged_in && filtered === null && !error && <Muted>Loading canvases…</Muted>}
        {status?.logged_in && filtered !== null && filtered.length === 0 && (
          <Empty className="py-16">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <LayoutGrid />
              </EmptyMedia>
              <EmptyTitle>
                {query ? "No canvases match your search" : "No canvases in this account yet"}
              </EmptyTitle>
              {!query && (
                <EmptyDescription>Create one to start developing it locally.</EmptyDescription>
              )}
            </EmptyHeader>
            {!query && !creating && (
              <Button onClick={() => setCreating(true)}>
                <Plus />
                New canvas
              </Button>
            )}
          </Empty>
        )}
        {status?.logged_in && filtered !== null && filtered.length > 0 && (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4">
            {filtered.map((canvas) => (
              <CanvasCard
                key={canvas.name}
                canvas={canvas}
                // Either the free public URL from the listing, or the signed
                // one the previews batch filled in afterwards (D364).
                thumb={
                  canvas.preview_url ?? (canvas.id ? previews[canvas.id] : undefined) ?? null
                }
                broken={brokenPreviews}
                onBroken={(url) =>
                  setBrokenPreviews((prev) => {
                    const next = new Set(prev);
                    next.add(url);
                    return next;
                  })
                }
                cloning={busy === canvas.name}
                disabled={busy !== null || createBusy}
                onOpen={() => void onOpen(canvas)}
              />
            ))}
          </div>
        )}
      </PageBody>
    </Page>
  );
}
