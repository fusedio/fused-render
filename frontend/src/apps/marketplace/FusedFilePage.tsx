// The `.fused` app file's INSTALL PAGE (SPEC §43 AF-14), App-Store style —
// the same detail page as /marketplace/app/<slug> (AppDetailPage.tsx), built
// from the same @platform/ui/appstore blocks: hero with the primary action
// and a "⋯" menu, the Information strip, the screenshot stage, Overview
// (readme) / Files beside an Information list and the "Before you install"
// card. The one install page, whatever `marketplace_enabled` says: the
// fusedapp template (fused_render/templates/fusedapp) only draws card peeks.
//
// Reached two ways, one component:
//   /marketplace/file?path=<abs .fused>          (MarketplacePage's route)
//   /explorer/view|embed/<path>.fused            (rendered IN PLACE by the
//       shell — App.tsx — so a double-click, a deeplink, the CLI and a click
//       in a listing all land here with no redirect and no flash)
//
// Nothing in the file runs here. The page reads the read-only probe (GET
// /api/appfile/install: what installing would do), the icon and the
// screenshot, and its one action POSTs the X-Fused /api/appfile/install,
// which unpacks the app into <workspace>/app/<slug> (or updates the copy
// there). Each probe STATE maps to a pill and a status callout:
//
//   new        Install (accent)          ok    "Not installed yet"
//   update     Update (accent)           ok    "Update available"
//   diverged   Overwrite → Confirm (caution, two clicks)  warn + changed files
//   downgrade  Overwrite → Confirm                         warn
//   unknown    Overwrite → Confirm                         warn
//   same       Open; ⋯ Reinstall → Confirm                 ok
//   clash      Install, disabled                           warn
//   switch-link  Follow (caution) · Keep reviewed releases  warn
//              (stage B, CONTRIB_LOOP.md §18.4: a GitHub link opened for
//              an app installed from the Marketplace. Following the author
//              directly is the user's explicit switch — Follow posts the
//              install with `switch: true`; Keep leaves the copy alone.)
//
// A WARN state (`needs_force`) takes two clicks: the first arms the pill
// ("Confirm overwrite", filled caution), the second sends `force`. A copy
// that changed between the probe and the click answers 409 with the fresh
// probe, which is simply re-rendered — the reader decides again.
//
// After an install the page opens the app where the template's `go()` did:
// the entry page's explorer view, or — when this document is a chrome-free
// embed (a double-click / deeplink / CLI window) — its embed, so the window
// becomes the app's.
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  ArrowUpRight,
  FileArchive,
  FolderOpen,
  LayoutPanelTop,
  MessageSquare,
  Play,
  RotateCcw,
  Trash2,
} from "lucide-react";
import {
  ActionPill,
  AppIcon,
  DetailBody,
  DetailHero,
  DetailLayout,
  DetailSkeleton,
  Delayed,
  InfoList,
  InfoStrip,
  OverviewTabs,
  PreviewStage,
  StatusCallout,
  TagsSection,
  TrustCard,
  plainClick,
  readmeLead,
  type ActionPillProps,
  type BackLink,
  type InfoListRow,
  type InfoStripItem,
  type StatusTone,
  type UrlResolver,
} from "@platform/ui/appstore";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import {
  appfileIconUrl,
  appfilePreviewUrl,
  getAppFileProbe,
  installAppFile,
  type AppFileProbe,
  type HttpError,
} from "@platform/lib/api";
import { basename, dirname, formatSize } from "@platform/lib/format";
import { useDocumentTitle, useRefreshOnReturn } from "@platform/lib/hooks";
import { navReach } from "@platform/lib/nav-history";
import { notify } from "@platform/lib/notifications";
import {
  IS_EMBED,
  IS_TOP_EMBED,
  embedUrlForFsPath,
  navigate,
  navigateUrl,
  urlForFsPath,
  viewUrlForFsPath,
} from "@platform/lib/router";
import { CURRENT_APPS_CHANGED_EVENT, announceCurrentAppsChanged } from "@platform/lib/tasksChanged";
import { openUninstallApp } from "@platform/lib/uninstall-app";
import { appPageHref } from "@platform/lib/marketplace";
import { EmptyState } from "./shared";
import { trustItemsForFile } from "./trust";

// -- Data ------------------------------------------------------------------------

/** Last probe per file: a remount (every navigation remounts the route) paints
 *  the page at once and revalidates behind it. */
const probeCache = new Map<string, AppFileProbe>();

function useProbe(path: string) {
  const [probe, setProbe] = useState<AppFileProbe | null>(() => probeCache.get(path) ?? null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const ctl = new AbortController();
    getAppFileProbe(path, { signal: ctl.signal })
      .then((p) => {
        probeCache.set(path, p);
        setProbe(p);
        setError(null);
      })
      .catch((e: HttpError) => {
        if (ctl.signal.aborted) return;
        setError(e.message || "Couldn't read this app file");
      });
    return () => ctl.abort();
  }, [path, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const replace = useCallback(
    (p: AppFileProbe) => {
      probeCache.set(path, p);
      setProbe(p);
    },
    [path],
  );
  // An uninstall (the shared dialog announces it), or edits made to the
  // installed copy while the reader was elsewhere, change the state.
  useEffect(() => {
    window.addEventListener(CURRENT_APPS_CHANGED_EVENT, reload);
    return () => window.removeEventListener(CURRENT_APPS_CHANGED_EVENT, reload);
  }, [reload]);
  useRefreshOnReturn(reload);
  return { probe, error, reload, replace };
}

// -- Formatting -------------------------------------------------------------------

/** The export stamp (UTC ISO, seconds) as the reader's local date + time —
 *  two exports of one app on one day differ by the minute. */
function fmtStamp(iso: string | null | undefined, withTime = true): string | null {
  if (!iso || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  });
}

function shortDate(iso: string | null | undefined): string | null {
  if (!iso || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

function clock(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** Two export stamps as labels that tell them apart: their dates, or — when
 *  both fall on one day (two exports an hour apart) — their clock times. */
function stampPair(a: string | null | undefined, b: string | null | undefined): [string | null, string | null] {
  const da = shortDate(a);
  const db = shortDate(b);
  if (da && da === db) return [clock(a), clock(b)];
  return [da, db];
}

/** "…/app/sine-lab" → "app/sine-lab": the workspace-relative tail. */
function tail(path: string, n = 2): string {
  return path.split("/").filter(Boolean).slice(-n).join("/");
}

function githubUrl(handle: string): string {
  return `https://github.com/${encodeURIComponent(handle.replace(/^@/, ""))}`;
}

// -- Navigation --------------------------------------------------------------------
// The embed prefix is fixed per document (router.ts), so from a TOP-LEVEL
// embed (the window a double-click opened) the explorer is a new document with
// its chrome — EmbedStrip's "Open in explorer" rule. A framed embed (a panel
// pane) stays in its frame; a normal window navigates in place.

function openInExplorer(path: string, opts: { isDir: boolean; sel?: string }) {
  const search = opts.sel ? "?sel=" + encodeURIComponent(opts.sel) : "";
  if (IS_TOP_EMBED) location.assign(viewUrlForFsPath(path, search));
  else if (IS_EMBED) navigateUrl(urlForFsPath(path, search), { isDir: opts.isDir });
  else navigate(path, { isDir: opts.isDir, sel: opts.sel });
}

function explorerHref(path: string, sel?: string): string {
  return viewUrlForFsPath(path, sel ? "?sel=" + encodeURIComponent(sel) : "");
}

/** Open the installed app's entry page — the template's `go()`. */
function openEntry(entryPath: string) {
  if (IS_TOP_EMBED) location.assign(embedUrlForFsPath(entryPath));
  else if (IS_EMBED) navigateUrl(embedUrlForFsPath(entryPath), { isDir: false });
  else navigateUrl(viewUrlForFsPath(entryPath), { isDir: false });
}

// -- State → pill + callout ---------------------------------------------------------

type Phase =
  | { kind: "idle" }
  | { kind: "armed" }
  | { kind: "installing"; force: boolean }
  | { kind: "done"; entry: string; updated: boolean }
  | { kind: "error"; message: string };

interface StateView {
  /** Whether the page shows the callout at all: a plain `new` file needs no
   *  sentence the pill and the strip do not already say. */
  callout: boolean;
  tone: StatusTone;
  title: string;
  body: ReactNode;
  list?: string[];
  /** The strip's Status cell. */
  status: string;
  statusCaption: string;
}

function describe(t: AppFileProbe): StateView {
  if (t.git) return describeGit(t, t.git);
  const when = (s: string | null) => fmtStamp(s) || "an unknown date";
  const at = <code title={t.path}>{tail(t.path)}</code>;
  const keeps = <>.fused, .venv and files the app file does not carry stay.</>;
  switch (t.state) {
    case "new":
      return {
        callout: t.husk,
        tone: "info",
        title: t.husk ? "Not installed — its data is still here" : "Not installed yet",
        body: t.husk ? (
          <>Installing puts the app back at {at} and picks up the data an earlier install left in .fused.</>
        ) : (
          <>Install puts the app at {at} as an editable app of your own.</>
        ),
        status: "Not installed",
        statusCaption: "Ready to install",
      };
    case "update":
      return {
        tone: "ok",
        callout: true,
        title: "Update available",
        body: (
          <>
            Installed version from {when(t.installed_exported_at)}; this file is from {when(t.exported_at)}. Your copy is
            unchanged since it was installed. Updating replaces its files and keeps everything in .fused.
          </>
        ),
        status: "Update",
        statusCaption: "Newer file",
      };
    case "diverged": {
      const n = t.changed.length;
      return {
        tone: "warn",
        callout: true,
        title: "Your copy has changed since it was installed",
        body: (
          <>
            This file ({when(t.exported_at)}) is newer than what you installed ({when(t.installed_exported_at)}), but {n}{" "}
            installed file{n === 1 ? "" : "s"} differ from the install. Overwriting replaces them; {keeps} The previous
            state stays in the app's history.
          </>
        ),
        list: t.changed,
        status: "Edited",
        statusCaption: `${n} file${n === 1 ? "" : "s"} changed`,
      };
    }
    case "same":
      return {
        tone: "ok",
        callout: true,
        title: "This version is installed",
        body: (
          <>
            Installed at {at} from a file exported {when(t.exported_at)}. Open it, or reinstall from the ⋯ menu to reset
            its files.
          </>
        ),
        status: "Installed",
        statusCaption: "This version",
      };
    case "downgrade":
      return {
        tone: "warn",
        callout: true,
        title: "Older than the installed version",
        body: (
          <>
            This file is from {when(t.exported_at)}; the installed copy is from {when(t.installed_exported_at)}. Overwriting
            goes back to this older version. {keeps}
          </>
        ),
        status: "Older",
        statusCaption: "Than installed",
      };
    case "unknown":
      return {
        tone: "warn",
        callout: true,
        title: "Installed copy of unknown version",
        body: (
          <>
            A copy is at {at} but it carries no install record (a copy by hand, or an export without a date), so
            this file cannot be compared with it. Overwriting replaces its files; {keeps}
          </>
        ),
        status: "Installed",
        statusCaption: "Unknown version",
      };
    case "clash":
      return {
        tone: "warn",
        callout: true,
        title: "A different app is installed under this name",
        body: <>{at} holds another app. Rename or uninstall it, then install this file.</>,
        status: "Name taken",
        statusCaption: "Another app",
      };
  }
  return { callout: true, tone: "warn", title: "Unknown state", body: String(t.state), status: "—", statusCaption: "" };
}

type GitLink = NonNullable<AppFileProbe["git"]>;

/** A GitHub-link build (SPEC §26 DL-9) installs as a copy that keeps getting
 *  its author's latest — the vanilla template's describeGit, same words. No
 *  git vocabulary anywhere (contrib §0.1, §16.8). For an INSTALLED GitHub-link
 *  copy `git` also carries the contribution engine's view: `who` (whose
 *  latest), `changes` (the user has their own — its presence marks such a
 *  copy), `collision` (their own files the update would land on). */
function describeGit(t: AppFileProbe, git: GitLink): StateView {
  const at = <code title={t.path}>{tail(t.path)}</code>;
  const label = repoLabel(git.url);
  const repo = <code>{label}</code>;
  const owner = label.split("/")[1] || "the author";
  const followed = "changes" in git;
  const who = git.who || owner;
  const blocked = followed ? (git.collision ?? []) : [];
  // The app page is pointed at only when it has the panel to act on
  // (`git.app_page`, the A1 switch); otherwise plain words.
  const appPage = followed && git.app_page === true;
  switch (t.state) {
    case "new":
      return {
        callout: true,
        tone: "info",
        title: "Not installed yet",
        body: (
          <>
            Install puts a copy of {repo} at {at}. It keeps getting {possessive(owner)} latest version, and the changes
            you make stay on this computer until you choose to share them.
          </>
        ),
        status: "Not installed",
        statusCaption: "Ready to install",
      };
    case "update":
      return followed
        ? {
            callout: true,
            tone: "ok",
            title: "A newer version is available",
            body: <>{who} has a newer version of this app.</>,
            status: "Update",
            statusCaption: "Newer version",
          }
        : {
            callout: true,
            tone: "ok",
            title: "Update available",
            body: <>A newer version of {repo} is available.</>,
            status: "Update",
            statusCaption: "Newer version",
          };
    case "same":
      return {
        callout: true,
        tone: "ok",
        title: "This version is installed",
        body: followed ? (
          <>You have {possessive(who)} latest version.</>
        ) : (
          <>
            {at} has the newest version of {repo}.
          </>
        ),
        status: "Installed",
        statusCaption: "Up to date",
      };
    case "local":
      if (blocked.length > 0) {
        return {
          callout: true,
          tone: "warn",
          title: "A file of yours is in the way",
          body: (
            <>
              {possessive(who)} latest adds {blocked.length === 1 ? blocked[0] : `${blocked.length} files`}, which you
              already have here
              {appPage
                ? ". Open the app to move yours aside and get the update."
                : ", so this app isn't updated from here."}
            </>
          ),
          list: blocked.length > 1 ? blocked : undefined,
          status: "Edited",
          statusCaption: blocked.length === 1 ? "A file in the way" : `${blocked.length} files in the way`,
        };
      }
      if (followed) {
        return {
          callout: true,
          tone: "warn",
          title: "You have your own changes",
          body: appPage ? (
            <>You have your own changes in this app. Open it to bring in {possessive(who)} latest.</>
          ) : (
            <>This app has changes of its own, so it isn&rsquo;t updated from here.</>
          ),
          status: "Edited",
          statusCaption: "Your changes",
        };
      }
      return t.found_by === "git"
        ? {
            callout: true,
            tone: "warn",
            title: "Your copy has changes of its own",
            body: (
              <>
                {at} has changes that are not in {repo}, so it is not updated from here. Open it to see them, or ask
                Claude.
              </>
            ),
            status: "Edited",
            statusCaption: "Local changes",
          }
        : {
            callout: true,
            tone: "warn",
            title: "Your copy was edited since it was installed",
            body: (
              <>
                {at} holds this app installed from a .fused file, and these files changed since. Rename or uninstall it,
                then open the link again.
              </>
            ),
            list: t.changed,
            status: "Edited",
            statusCaption: "Local changes",
          };
    case "replace":
      return {
        callout: true,
        tone: "warn",
        title: "Installed from a file",
        body: (
          <>
            {at} holds this app, installed from a file. Replacing it puts a copy of {repo} that keeps getting{" "}
            {possessive(owner)} latest in its place, and keeps its data (.fused) and environment (.venv); the old folder
            goes to the Trash.
          </>
        ),
        status: "Installed",
        statusCaption: "From a file",
      };
    case "clash":
      return {
        callout: true,
        tone: "warn",
        title: "A different app is installed under this name",
        body: <>{at} holds another app. Rename or uninstall it, then open the link again.</>,
        status: "Name taken",
        statusCaption: "Another app",
      };
    case "switch-link":
      return {
        callout: true,
        tone: "warn",
        title: `Follow ${who} directly? You'll get every change, unreviewed.`,
        body: (
          <>
            You installed this app from the Marketplace, so {at} gets only releases Fused reviewed. Following {who}{" "}
            directly brings every change {who} makes, as soon as it is made. You can switch back from the app&rsquo;s
            Activity.
          </>
        ),
        status: "Installed",
        statusCaption: "Reviewed releases",
      };
  }
  return { callout: true, tone: "warn", title: "Unknown state", body: String(t.state), status: "—", statusCaption: "" };
}

function possessive(name: string): string {
  return name.endsWith("s") ? `${name}'` : `${name}'s`;
}

/** "https://github.com/owner/repo" → "github.com/owner/repo". */
function repoLabel(url: string): string {
  return url.replace(/^https?:\/\//i, "").replace(/\.git$/i, "");
}

/** The first-click label of each state's primary action. */
function primaryLabel(t: AppFileProbe): string {
  switch (t.state) {
    case "replace":
      return "Replace";
    case "switch-link":
      return "Follow";
    case "update":
      // An installed GitHub-link copy brings the author's latest in.
      return t.git && "changes" in t.git ? "Get the latest" : "Update";
    case "diverged":
    case "downgrade":
    case "unknown":
      return "Overwrite";
    case "same":
      return "Reinstall";
    default:
      return "Install";
  }
}

// -- The page ----------------------------------------------------------------------------

export function FusedFilePage({ path }: { path: string }) {
  const { probe, error, reload, replace } = useProbe(path);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const fileName = basename(path);
  const folder = dirname(path);
  const meta = probe?.metadata ?? null;
  const name = meta?.name || probe?.name || fileName.replace(/\.fused$/i, "");
  useDocumentTitle(name);

  // Esc backs out of the armed (confirm) step.
  useEffect(() => {
    if (phase.kind !== "armed") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPhase({ kind: "idle" });
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [phase.kind]);

  // A fresh probe (focus return, uninstall) disarms a stale confirm.
  useEffect(() => {
    setPhase((p) => (p.kind === "armed" || p.kind === "error" ? { kind: "idle" } : p));
  }, [probe?.state, probe?.installed]);

  // A deep-link download lives in an internal cache folder: no way "back"
  // into it, and no "Show in Explorer" pointing there. Only the probe knows,
  // so the folder link waits for it rather than flashing the cache's name.
  const downloaded = probe?.downloaded === true;
  const canBack = navReach().back && !IS_TOP_EMBED;
  const back: BackLink | undefined = canBack
    ? { label: "Back", href: explorerHref(folder, fileName), onClick: plainClick(() => history.back()) }
    : !probe || downloaded
      ? undefined
      : {
          label: basename(folder) || "Files",
          href: explorerHref(folder, fileName),
          onClick: plainClick(() => openInExplorer(folder, { isDir: true, sel: fileName })),
        };

  const resolveUrl = useMemo<UrlResolver>(
    () => (url, kind) => {
      if (/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(url)) {
        // An app file is untrusted until installed: a remote image would
        // tell its author who opened it, and when. Links stay (a click is
        // consent); images load only from the file itself or inline data.
        if (kind === "img" && !/^data:image\//i.test(url)) return null;
        return url;
      }
      // Members are not served one by one; the two the file has routes for are.
      const rel = url.replace(/^\.\//, "").toLowerCase();
      if (kind === "img" && rel === "preview.png") return appfilePreviewUrl(path);
      if (kind === "img" && (rel === "icon.svg" || rel === "icon.png")) return appfileIconUrl(path);
      return null;
    },
    [path],
  );

  if (!probe) {
    return (
      <DetailLayout back={back}>
        {error ? (
          <EmptyState
            icon={<FileArchive size={22} strokeWidth={1.8} />}
            title={/no such file/i.test(error) ? "App file not found" : "Couldn't read this app file"}
            body={error}
            action={
              <button type="button" className="mp-btn" onClick={reload}>
                Try again
              </button>
            }
          />
        ) : (
          <Delayed>
            <DetailSkeleton />
          </Delayed>
        )}
      </DetailLayout>
    );
  }

  const t = probe;
  const view = describe(t);
  const installedEntry = t.installed ? t.entry_path : null;

  const install = async (force: boolean, switchTo = false) => {
    setPhase({ kind: "installing", force });
    try {
      const r = await installAppFile(path, force, { switch: switchTo });
      if ("needsConfirm" in r) {
        // The copy changed between the probe and the click: show the state it
        // is really in and let the reader decide again.
        replace(r.needsConfirm);
        setPhase({ kind: "idle" });
        notify({ title: "The installed copy changed — check the new state before installing", tone: "info" });
        return;
      }
      const res = r.ok;
      const updated = res.action === "updated";
      setPhase({ kind: "done", entry: res.entry, updated });
      announceCurrentAppsChanged();
      notify({ title: `${name} ${updated ? "updated" : "installed"}`, tone: "info" });
      // A beat for the pill's check, then the app — the template's go().
      setTimeout(() => openEntry(res.entry), 700);
    } catch (e) {
      setPhase({ kind: "error", message: (e as Error).message || "Install failed" });
    }
  };

  // ---- primary action ----
  let pill: ActionPillProps;
  let note: ReactNode = null;
  const label = primaryLabel(t);
  const sizeNote = `${formatSize(t.bytes)} · ${t.files.length} file${t.files.length === 1 ? "" : "s"}`;
  if (phase.kind === "installing") {
    pill = { state: "progress", progress: null, label: phase.force || t.installed ? "Updating" : "Installing" };
    note = !t.installed
      ? "Unpacking into your workspace…"
      : t.git
        ? "Getting the latest version…"
        : "Replacing the app's files…";
  } else if (phase.kind === "done") {
    pill = { state: "open", onClick: () => openEntry(phase.entry) };
    note = <b>{phase.updated ? "Updated" : "Installed"} · opening…</b>;
  } else if (phase.kind === "error") {
    pill = { state: "retry", title: phase.message, onClick: () => install(t.needs_force) };
    note = <span className="is-error">{phase.message}</span>;
  } else if (phase.kind === "armed") {
    pill = {
      state: "caution",
      emphasis: "filled",
      wide: true,
      label: t.state === "same" ? "Confirm reinstall" : t.state === "replace" ? "Confirm replace" : "Confirm overwrite",
      onClick: () => install(true),
    };
    note = (
      <>
        <span className="is-warn">
          {t.state === "diverged"
            ? `Replaces ${t.changed.length} edited file${t.changed.length === 1 ? "" : "s"}`
            : t.state === "replace"
              ? "Old folder goes to the Trash"
              : t.state === "same"
              ? "Resets the app's files"
              : "Replaces the installed files"}
        </span>
        {" · "}
        <button type="button" className="as-link-btn" onClick={() => setPhase({ kind: "idle" })}>
          Cancel
        </button>
      </>
    );
  } else if (t.state === "clash") {
    pill = { state: "disabled", label: "Install", title: "Another app is installed under this name" };
    note = <span className="is-warn">Name taken by another app</span>;
  } else if (t.state === "switch-link") {
    pill = { state: "caution", label, wide: true, onClick: () => install(true, true) };
    note = (
      <>
        <span className="is-warn">Installed from the Marketplace</span>
        {" · "}
        <button
          type="button"
          className="as-link-btn"
          onClick={() => (installedEntry ? openEntry(installedEntry) : history.back())}
        >
          Keep reviewed releases
        </button>
      </>
    );
  } else if (t.state === "local") {
    pill = installedEntry
      ? { state: "open", onClick: () => openEntry(installedEntry) }
      : { state: "disabled", label: "Update", title: "Your copy has changes of its own" };
    note = <span className="is-warn">Installed · changes of its own</span>;
  } else if (t.state === "same") {
    pill = installedEntry
      ? { state: "open", onClick: () => openEntry(installedEntry) }
      : t.git
        ? { state: "disabled", label: "Installed", title: "This version is already installed" }
        : { state: "caution", label, onClick: () => setPhase({ kind: "armed" }) };
    note = <>Installed{shortDate(t.exported_at) ? <> · exported {shortDate(t.exported_at)}</> : null}</>;
  } else if (t.needs_force) {
    pill = { state: "caution", label, onClick: () => setPhase({ kind: "armed" }) };
    note =
      t.state === "diverged" ? (
        <span className="is-warn">Installed · edited locally</span>
      ) : t.state === "replace" ? (
        <span className="is-warn">Installed · from a file</span>
      ) : t.state === "downgrade" ? (
        <span className="is-warn">Older than installed</span>
      ) : (
        <span className="is-warn">Installed · no install record</span>
      );
  } else if (t.state === "update") {
    const [mine, theirs] = stampPair(t.exported_at, t.installed_exported_at);
    pill = { state: "update", label, onClick: () => install(false) };
    note = t.git ? (
      "A newer version is available"
    ) : (
      <>
        Update to <b>{mine ?? "this file"}</b>
        {theirs ? <> · you have {theirs}</> : null}
      </>
    );
  } else {
    pill = { state: "get", emphasis: "filled", label, onClick: () => install(false) };
    note = sizeNote;
  }

  // ---- ⋯ menu ----
  const menu: MenuEntry[] = [];
  if (installedEntry && t.state !== "same" && t.state !== "clash" && t.state !== "local") {
    menu.push({
      label: "Open installed app",
      icon: <Play size={14} strokeWidth={2} />,
      onClick: () => openEntry(installedEntry),
    });
  }
  if (t.state === "same" && !t.git && phase.kind === "idle") {
    menu.push({
      label: "Reinstall from this file…",
      icon: <RotateCcw size={14} strokeWidth={2} />,
      onClick: () => setPhase({ kind: "armed" }),
    });
  }
  if (t.installed && t.state !== "clash" && !IS_EMBED) {
    menu.push({
      label: "Open app page",
      icon: <LayoutPanelTop size={14} strokeWidth={2} />,
      onClick: () => navigateUrl(appPageHref(t.path)),
    });
  }
  if (t.installed || t.state === "clash") {
    menu.push({
      label: "Show app folder",
      icon: <FolderOpen size={14} strokeWidth={2} />,
      onClick: () => openInExplorer(t.path, { isDir: true }),
    });
  }
  menu.push({
    label: "Show file in Explorer",
    icon: <FileArchive size={14} strokeWidth={2} />,
    onClick: () => openInExplorer(folder, { isDir: true, sel: fileName }),
  });
  if (!IS_EMBED) {
    // The file's other explorer mode (the template's mode switcher had it):
    // `_mode=claude` is the one URL the shell does not turn into this page.
    menu.push({
      label: "Ask Claude about this file",
      icon: <MessageSquare size={14} strokeWidth={2} />,
      onClick: () => navigate(path, { isDir: false, mode: "claude" }),
    });
  }
  if (t.installed && t.state !== "clash") {
    menu.push("separator");
    menu.push({
      label: "Uninstall…",
      icon: <Trash2 size={14} strokeWidth={2} />,
      danger: true,
      onClick: () => openUninstallApp({ path: t.path, name }),
    });
  }

  // ---- hero ----
  const subtitle = meta?.description || (t.readme ? readmeLead(t.readme) : null) || "A Fused app, packed into one file.";
  const authorNode = meta?.author ? (
    meta.author.github ? (
      <a href={githubUrl(meta.author.github)} target="_blank" rel="noopener noreferrer" className="mp-author-link">
        {meta.author.name}
      </a>
    ) : (
      <span>{meta.author.name}</span>
    )
  ) : null;
  const author = (
    <span className="mp-file-byline">
      {authorNode && <>by {authorNode}</>}
      {authorNode && <span aria-hidden="true"> · </span>}
      <span className="mp-file-name" title={path}>
        <FileArchive size={13} strokeWidth={2} aria-hidden="true" />
        {fileName}
      </span>
    </span>
  );

  // ---- information strip ----
  const exportedShort = shortDate(t.exported_at);
  const versionCaption =
    t.installed && t.installed_exported_at && t.installed_exported_at !== t.exported_at && t.state !== "clash"
      ? `You have ${stampPair(t.exported_at, t.installed_exported_at)[1] ?? "another"}`
      : meta?.version
        ? exportedShort
          ? `Exported ${exportedShort}`
          : "Exported"
        : clock(t.exported_at)
          ? `Exported ${clock(t.exported_at)}`
          : "Export date";
  const strip: InfoStripItem[] = [
    { label: "Type", value: <FileArchive size={18} strokeWidth={2} aria-hidden="true" />, caption: ".fused file" },
    {
      label: "Version",
      value: meta?.version ?? exportedShort ?? "—",
      caption: versionCaption,
      title: fmtStamp(t.exported_at) ? `Exported ${fmtStamp(t.exported_at)}` : undefined,
    },
    { label: "Size", value: formatSize(t.bytes), caption: "Unpacked" },
    { label: "Files", value: String(t.files.length), caption: "In this file" },
    {
      label: t.state === "clash" ? "Location" : t.installed ? "Installed at" : "Installs to",
      value: <span className="mp-strip-dev">{tail(t.path)}</span>,
      caption: t.state === "clash" ? "Taken" : "Workspace",
      title: t.path,
    },
    {
      label: "Status",
      value: <span className={view.tone === "warn" ? "mp-strip-warn" : undefined}>{view.status}</span>,
      caption: view.statusCaption,
    },
  ];

  // ---- aside ----
  const rows: InfoListRow[] = [
    {
      label: "File",
      value: (
        <button
          type="button"
          className="as-link-btn mp-path"
          title={path}
          onClick={() => openInExplorer(folder, { isDir: true, sel: fileName })}
        >
          {fileName}
        </button>
      ),
    },
  ];
  if (meta?.version) rows.push({ label: "Version", value: meta.version });
  rows.push({ label: "Exported", value: fmtStamp(t.exported_at, false) ?? "Unknown" });
  if (authorNode) rows.push({ label: "Developer", value: authorNode });
  rows.push({ label: "Size", value: formatSize(t.bytes) });
  if (t.installed && t.installed_at) rows.push({ label: "Installed", value: fmtStamp(t.installed_at, false) ?? "—" });
  rows.push({
    label: "Location",
    value: (
      <button
        type="button"
        className="as-link-btn mp-path"
        title={t.path}
        onClick={() => (t.installed || t.state === "clash" ? openInExplorer(t.path, { isDir: true }) : openInExplorer(dirname(t.path), { isDir: true }))}
      >
        {tail(t.path)}
      </button>
    ),
  });

  const topRight = downloaded ? undefined : (
    <a className="as-toplink" href={explorerHref(folder, fileName)} onClick={plainClick(() => openInExplorer(folder, { isDir: true, sel: fileName }))}>
      Show in Explorer <ArrowUpRight size={13} strokeWidth={2.2} aria-hidden="true" />
    </a>
  );

  return (
    <DetailLayout back={back} topRight={topRight} className="mp-file">
      <DetailHero
        icon={<AppIcon src={t.has_icon ? appfileIconUrl(path) : null} name={name} seed={t.slug} size={112} raised />}
        name={name}
        subtitle={subtitle}
        author={author}
        tags={meta?.tags ?? []}
        actions={<ActionPill size="lg" {...pill} />}
        actionsNote={note}
        menu={menu}
        menuLabel={`More actions for ${name}`}
      />
      {t.git && (
        <StatusCallout tone="warn" title="Unverified code from GitHub">
          This app comes from <code>{repoLabel(t.git.url)}</code>. Fused has not reviewed or verified it, and it can
          contain malicious code that runs with your access once installed. Install it only if you trust its author.
        </StatusCallout>
      )}
      {view.callout && (
        <StatusCallout tone={view.tone} title={view.title} list={view.list} listMax={12}>
          {view.body}
        </StatusCallout>
      )}
      <InfoStrip items={strip} />
      {t.has_preview && <PreviewStage src={appfilePreviewUrl(path)} alt={`${name} screenshot`} />}
      <DetailBody
        main={
          <OverviewTabs
            readme={t.readme ?? null}
            resolveUrl={resolveUrl}
            files={t.files}
            emptyText="This app file has no readme."
          />
        }
        aside={
          <>
            <InfoList rows={rows} />
            <TagsSection tags={meta?.tags ?? []} />
            <TrustCard items={trustItemsForFile(t)} />
          </>
        }
      />
    </DetailLayout>
  );
}

export default FusedFilePage;
