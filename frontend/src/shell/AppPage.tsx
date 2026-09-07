// The app page — `/apps/<folder path>` (D488, widened 2026-08-26): one app
// folder — a workspace app under any shelf, or a linked app anywhere on disk —
// as a place rather than as a folder. Six tabs, named by the `_tab` query
// param (absent = overview; `?_tab=tasks`, `?_tab=files`, `?_tab=api` —
// current-apps-lib):
//
//   Overview  the app itself, live in a frame — USE it here, the way the
//             explorer's file view runs an entry page (`/render?path=`, with no
//             `_preview` flag: this is a real open, and GET /render records it
//             as one, D301).
//   Tasks     the Tasks page (shell/Scheduled.tsx) scoped to this folder —
//             the same List / Board / Calendar, the same modal, a new task
//             prefilled with this app.
//   Files     the folder's files as a tree, each rendered in one of its own
//             templates (shell/AppFiles.tsx) — "what is in this app and what
//             does each piece look like", without leaving the page.
//   API       every .py in the folder as an endpoint, Swagger-style
//             (shell/AppApi.tsx): entrypoint, parameters as a form, Execute,
//             response — the api template's view, for the whole app at once.
//   Git       the folder's `git` template (templates/git) in a frame — the
//             working-tree view. Offered ONLY when the folder is inside a
//             work tree (the template's condition.py verdict, CT-12), so a
//             plain folder never shows a Git tab that could only say "no".
//
// The Git tab renders the EXISTING template rather than a second panel of
// its own: the template is the git contract's one UI, and a rebuild here
// would be a second one to keep in step. (An MCP tab framed templates/mcp the
// same way until 2026-08-27; the owner dropped it.)
//
// Opened from the sidebar's "Current apps" rows and NOWHERE ELSE (owner's
// brief): the hub's cards and the explorer keep opening the entry page as they
// always have. That is why this file adds no link to itself anywhere.
//
// Not the explorer. The explorer answers "what is in this folder"; this page
// answers "how is this app going" — the app, its work and its pieces side by
// side. The folder is one caption-click away for the operations (rename, move,
// new file) this page deliberately does not offer.
//
// Mounted per FOLDER, not per nav epoch (App.tsx): the Overview frame holds live
// app state, and a tab switch — a navigation, since the tab is in the path —
// must not reload it. The frame stays mounted behind the Tasks tab for the
// same reason (display:none, not unmount).
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import {
  appIconUrl,
  getAppEntry,
  getAppIcon,
  resolveConditions,
  statPath,
  type Config,
  type TemplateEntry,
} from "@platform/lib/api";
import { useFavicon, useUrlVersion } from "@platform/lib/hooks";
import { useThemedIconSrc } from "@platform/lib/app-icon-src";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { embedUrlForFsPath, navigateUrl, urlForFsPath } from "@platform/lib/router";
import {
  AppWindow,
  Files,
  GitBranch,
  ExternalLink,
  ListTodo,
  Webhook,
  type LucideIcon,
} from "lucide-react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { AppStar } from "@platform/ui/AppStar";
import IconPicker, { type IconPick } from "@platform/ui/IconPicker";
import { applyIconPick } from "@platform/lib/app-icon";
import { pushToast } from "@platform/lib/toast";
import { CURRENT_APPS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { AppDoctorModal } from "@platform/ui/AppDoctorModal";
import { Button } from "@platform/shadcn/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { basename } from "@platform/lib/format";
import { opensElsewhere, tildePath } from "./tasks-lib";
import {
  APP_PAGE_TABS,
  appPageTabFromSearch,
  appPageUrl,
  type AppPageTab,
} from "./current-apps-lib";
import Scheduled from "./Scheduled";
import AppFiles from "./AppFiles";
import AppApi from "./AppApi";

// ---- the tabs, as ONE registry -----------------------------------------------
//
// Adding a tab: one string in APP_PAGE_TABS (current-apps-lib.ts, which is
// also the route) and one entry below. `Record<AppPageTab, …>` is what makes
// the second half compulsory. The strip and the panels are both mapped from
// this, so there is no JSX to touch.
//
// `keepMounted`: the panel stays in the tree behind the other tabs (hidden, not
// unmounted). The Overview needs it — the frame holds live app state and a tab
// switch must not reload it. Nothing else should want it: a hidden panel still
// polls and paints.
type TabCtx = {
  slug: string;
  dir: string;
  entry: string | null;
  folderHref: string;
  /** The folder's `git` template entry, when offered (null = not). */
  gitTpl: TemplateEntry | null;
};

/** A folder template in a frame — the explorer's folder-peek shape
 *  (`/render?path=<template>&_file=<folder>`), no `_preview`: a real open. */
function templateFrame(dir: string, tpl: TemplateEntry, title: string) {
  return (
    <iframe
      className="app-page-frame"
      src={
        `/render?path=${encodeURIComponent(tpl.path as string)}` +
        `&_file=${encodeURIComponent(dir)}`
      }
      title={title}
    />
  );
}

type TabDef = {
  label: string;
  Icon: LucideIcon;
  keepMounted?: boolean;
  render: (ctx: TabCtx) => ReactNode;
};

const TAB_DEFS: Record<AppPageTab, TabDef> = {
  overview: {
    label: "Overview",
    Icon: AppWindow,
    keepMounted: true,
    render: ({ slug, entry, folderHref }) =>
      entry ? (
        <div className="app-page-frame-wrap">
          <iframe
            className="app-page-frame"
            src={`/render?path=${encodeURIComponent(entry)}`}
            title={`App: ${slug}`}
          />
        </div>
      ) : (
        <p className="app-page-empty">
          This folder has no entry page yet.{" "}
          <a href={folderHref}>Open the folder</a> to see what is there.
        </p>
      ),
  },
  tasks: {
    label: "Tasks",
    Icon: ListTodo, // the sidebar's Tasks icon too (GlobalSidebar SCHEDULED_ICON)
    render: ({ dir }) => <Scheduled scope={{ project: dir }} />,
  },
  files: {
    label: "Files",
    Icon: Files,
    // Not keepMounted: the selection is in the URL, so a return costs one walk
    // and one stat — cheaper than a hidden frame that keeps running.
    render: ({ dir, entry, folderHref }) => (
      <AppFiles dir={dir} entry={entry} folderHref={folderHref} />
    ),
  },
  api: {
    label: "API",
    Icon: Webhook,
    // Not keepMounted: the open row is in the URL (`?ep=`), and a return costs
    // one folder inspection — form values and responses are session scratch.
    render: ({ dir, folderHref }) => <AppApi dir={dir} folderHref={folderHref} />,
  },
  git: {
    label: "Git",
    Icon: GitBranch,
    // Not keepMounted: a fresh `git status` on return is the point.
    render: ({ dir, slug, gitTpl }) =>
      gitTpl ? (
        templateFrame(dir, gitTpl, `Git: ${slug}`)
      ) : (
        <p className="app-page-empty">
          This folder is not inside a git repository.
        </p>
      ),
  },
};

// What the folder turned out to be. `undefined` = still asking.
type Resolved =
  | { kind: "missing" }
  | { kind: "error"; message: string }
  | {
      kind: "app";
      entry: string | null;
    };

export default function AppPage({
  dir,
  config,
}: {
  /** The app folder, canonical forward-slash (current-apps-lib appPathFromPath). */
  dir: string;
  config: Config;
}) {
  const slug = useMemo(() => basename(dir) || dir, [dir]);
  const [resolved, setResolved] = useState<Resolved | undefined>(undefined);
  // The folder's templates (from the same stat that checks it is a folder)
  // and the gate verdicts for the conditional ones (CT-12: stat only marks
  // them; the gates run on demand). `null` verdicts = still asking.
  const [tpls, setTpls] = useState<TemplateEntry[]>([]);
  const [verdicts, setVerdicts] = useState<Record<string, boolean> | null>(
    null,
  );
  // The tab is the `_tab` query param, re-read on every URL event so
  // back/forward between the two tabs lands on the right one.
  useUrlVersion();
  const tab = appPageTabFromSearch(location.search);

  // The app's optional icon.svg (`/api/apps/icon`; the same file the Projects
  // row draws) — the header's mark AND, through useFavicon, the tab icon while
  // this page is open. One state for both, and one refetch after a pick: the
  // POST answers `{path, replaced}` with no mtime, and the mtime is exactly
  // what busts the browser's image and (far more stubborn) favicon caches, so
  // an optimistic set would show the old glyph under a new URL-less src.
  const [iconHref, setIconHref] = useState<string | null>(null);
  // The generation token stands in for the effect's usual `live` flag: a pick
  // reloads outside any effect, and a bare boolean captured per-effect cannot
  // cancel THAT read when the folder changes under it. A stale response is one
  // whose token is no longer current, whoever asked for it — which is also
  // what keeps a fast switch between two apps from painting the first one's
  // icon over the second.
  const iconGenRef = useRef(0);
  const loadIcon = useCallback(() => {
    const gen = ++iconGenRef.current;
    getAppIcon(dir)
      .then((r) => {
        if (iconGenRef.current === gen) {
          setIconHref(r.icon ? appIconUrl(r.icon, r.mtime) : null);
        }
      })
      .catch(() => {
        if (iconGenRef.current === gen) setIconHref(null);
      });
  }, [dir]);
  useEffect(() => {
    setIconHref(null);
    loadIcon();
  }, [loadIcon]);
  // The icon can also be changed from the OTHER end of the same app — the
  // sidebar's Projects glyph, whose picker writes the same file while this
  // page is the one on screen. It pokes the desk on a successful write
  // (applyIconPick), so the poke is the signal to re-read: without this the
  // header's mark and the tab favicon both kept the old glyph until the next
  // navigation, and only a pick made HERE ever looked refreshed.
  useEffect(() => {
    window.addEventListener(CURRENT_APPS_CHANGED_EVENT, loadIcon);
    return () => window.removeEventListener(CURRENT_APPS_CHANGED_EVENT, loadIcon);
  }, [loadIcon]);
  // Both the header mark and the favicon draw the theme-resolved src: a
  // picker-written glyph names its colour, and neither an <img> nor a
  // <link rel="icon"> can read a token on its own (app-icon-src.ts).
  const iconSrc = useThemedIconSrc(iconHref);
  useFavicon(iconSrc);

  // ---- the header mark's icon picker -----------------------------------------
  // The same picker the sidebar's Projects row opens from its glyph (the emoji
  // + branded-lucide IconPicker), anchored to the mark that opened it, writing
  // the same `icon.svg` through the same shared rule (applyIconPick, which also
  // pokes the sidebar so that row's glyph changes with this one). Its own
  // toggle selector: both glyphs are on screen together, and a loose selector
  // means each leaves the other's picker open (IconPicker's own note).
  const [iconAnchor, setIconAnchor] = useState<{ top: number; left: number } | null>(
    null,
  );
  // The picker decides when it closes (a shuffle leaves it open), so this only
  // writes.
  const onPickIcon = async (pick: IconPick | null) => {
    try {
      await applyIconPick(dir, pick);
    } catch (e) {
      // Louder than the sidebar's silent swallow: this mark is a deliberate
      // click on a page-level action, and a header that simply doesn't change
      // reads as the page being broken rather than the write having failed.
      pushToast({
        msg: "Could not change the icon: " + (e as Error).message,
        tone: "error",
      });
    }
    loadIcon();
  };

  useEffect(() => {
    let live = true;
    setResolved(undefined);
    setTpls([]);
    setVerdicts(null);
    (async () => {
      try {
        const st = await statPath(dir);
        if (!st.is_dir) {
          if (live) setResolved({ kind: "missing" });
          return;
        }
        if (live) {
          const templates = st.templates ?? [];
          setTpls(templates);
          if (templates.some((t) => t.conditional)) {
            // Shared in flight per path with any other asker (api.ts), so
            // this costs nothing extra when the explorer asked first.
            resolveConditions(dir)
              .then((r) => live && setVerdicts(r.conditions))
              .catch(() => live && setVerdicts({}));
          } else {
            setVerdicts({});
          }
        }
      } catch {
        // A stat that fails is a folder that is not there (404) or a server
        // that cannot say; either way there is no app to frame.
        if (live) setResolved({ kind: "missing" });
        return;
      }
      try {
        // The server's entry rule (D269/D301), asked at open time — the same
        // question every other surface asks, so this page can never disagree
        // with the card that pictures the app.
        const info = await getAppEntry(dir);
        if (live) setResolved({ kind: "app", entry: info.entry });
      } catch (e) {
        if (live) setResolved({ kind: "error", message: (e as Error).message });
      }
    })();
    return () => {
      live = false;
    };
  }, [dir]);

  const gitTplRaw = tpls.find((t) => t.mode === "git" && t.path) ?? null;
  // Git is offered only where its gate says yes: a `conditional` entry waits
  // for the verdict (pending reads as "not yet"), an unconditional one is in.
  const gitAllowed =
    !!gitTplRaw && (!gitTplRaw.conditional || verdicts?.git === true);
  const gitTpl = gitAllowed ? gitTplRaw : null;
  // The strip draws THESE; the route knows APP_PAGE_TABS. A tab that is not
  // offered is still a valid address (a `?_tab=git` deep link opened before
  // the verdict lands must not be rewritten away), so the panel logic below
  // tolerates `tab` being outside this list and renders that tab's own
  // empty state.
  const visibleTabs = useMemo(
    () =>
      APP_PAGE_TABS.filter((id) => {
        if (id === "git") return gitAllowed;
        return true;
      }),
    [gitAllowed],
  );
  const visibleRef = useRef(visibleTabs);
  visibleRef.current = visibleTabs;

  // Left/Right step the tabs (owner, 2026-08-26), the sibling of the sidebar's
  // Up/Down over its rows (sidebarArrowNav.ts): together the two axes make the
  // app page steerable from the keyboard alone. Same ownership rule as there —
  // only when nothing in particular is focused (<body>) or focus is in the
  // sidebar, so a focused control, a text field, or the base-ui tab list's own
  // arrow handling keeps its keys. Ends stop, no wrap.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (e.isComposing || e.defaultPrevented) return;
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
      if (isOverlayOpen()) return;
      const el = document.activeElement as HTMLElement | null;
      // A text field keeps its caret keys — the sidebar holds one mid-rename
      // (BookmarksSection's RenameInput), so "in the sidebar" is not enough.
      if (
        el &&
        (el.tagName === "INPUT" ||
          el.tagName === "TEXTAREA" ||
          el.isContentEditable)
      )
        return;
      const onBody =
        !el || el === document.body || el === document.documentElement;
      const inSidebar = !!el && !!document.getElementById("sidebar")?.contains(el);
      if (!onBody && !inSidebar) return;
      // Over the VISIBLE tabs, through a ref so this [dir]-scoped listener
      // never steps onto a hidden Git tab from a stale closure.
      const tabs = visibleRef.current;
      const cur = appPageTabFromSearch(location.search);
      const i = tabs.indexOf(cur) + (e.key === "ArrowRight" ? 1 : -1);
      e.preventDefault();
      if (i < 0 || i >= tabs.length) return;
      navigateUrl(appPageUrl(dir, tabs[i], location.search));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dir]);

  const pickTab = (e: MouseEvent<HTMLAnchorElement>, next: AppPageTab) => {
    if (opensElsewhere(e)) return;
    e.preventDefault();
    // The query rides along: it is the tab's own (`?view=` on Tasks), and a
    // switch away and back should find it as it was.
    if (next !== tab) navigateUrl(appPageUrl(dir, next, location.search));
  };

  const folderHref = urlForFsPath(dir);
  // Folded ONCE for every tilde below: `home` is raw expanduser (backslashed on
  // Windows) while `dir` and the root are forward-slash, and a prefix test
  // between the two spellings prints the full path instead of "~/…".
  const home = config.home.replace(/\\/g, "/");
  const entry = resolved?.kind === "app" ? resolved.entry : null;

  // App Doctor: the share-readiness checklist for this folder, opened from the
  // header beside "Open app". It stands where the fused-API "Migrate" button
  // stood, and subsumes it — a stale `fused-api-version` tag is one row of the
  // checklist now, beside the things migrate never covered (a leaked key, a
  // path tied to one machine, stray generated files, an uncommitted tree). The
  // dialog owns the whole flow: it runs the checks, and its "Explain and fix"
  // creates the one task that hands the report to a session
  // (platform/ui/AppDoctorModal).
  const [doctorOpen, setDoctorOpen] = useState(false);
  useEffect(() => {
    setDoctorOpen(false);
  }, [dir]);

  return (
    <div className="app-page">
      <header className="app-page-head">
        <div className="app-page-title">
          {/* The app's mark, and the way to change it: a click opens the same
              icon picker the sidebar's Projects row does (below). The app's
              own icon.svg drawn as is — the author's colours, no tint — or
              the generic star when it has none, which is also the affordance
              for an app that has never had an icon. */}
          <button
            type="button"
            className="app-page-icon app-page-icon-toggle"
            title="Change icon"
            aria-label="Change icon"
            onClick={(e) => {
              const rect = e.currentTarget.getBoundingClientRect();
              setIconAnchor((cur) =>
                cur ? null : { top: rect.top, left: rect.left },
              );
            }}
          >
            {iconSrc ? (
              <img src={iconSrc} alt="" draggable={false} />
            ) : (
              <AppStar />
            )}
          </button>
          {/* The name and the folder keep their own baseline row: the mark is
              a column beside the PAIR (it centers against both), and a
              baseline-aligned box in the same row would drop the text off
              center against it. */}
          <div className="app-page-name">
            <h1>{slug}</h1>
            {/* Reads as the folder and IS the folder: opens its listing in the
                explorer. The app itself is the "Open app" button opposite. */}
            <a className="app-page-folder" href={folderHref} title={dir}>
              {tildePath(dir, home)}
            </a>
          </div>
        </div>
        {entry && (
          <div className="app-page-actions">
            {/* Every app gets this, current or not: what an app about to be
                shared needs checked is never only its API version. */}
            <Button
              size="sm"
              className="app-page-doctor"
              variant="outline"
              title="Check this app before you share it: leaked credentials, paths tied to this machine, stray generated files, uncommitted work, a stale fused API version"
              onClick={() => setDoctorOpen(true)}
            >
              App Doctor
            </Button>
            {/* The app full-size AS AN APP — its entry page in embed mode,
                chrome-free — in a new tab so this page and its live frame stay
                put. The top-level embed's strip (EmbedStrip) is the way back
                into the explorer from there; the folder link opposite is the
                explorer route from here. */}
            <Button
              size="sm"
              variant="outline"
              className="app-page-open"
              onClick={() => window.open(embedUrlForFsPath(entry), "_blank", "noopener")}
            >
              Open app
              <ExternalLink data-icon="inline-end" />
            </Button>
          </div>
        )}
      </header>
      {iconAnchor && (
        <IconPicker
          anchor={iconAnchor}
          toggleSelector=".app-page-icon-toggle"
          onPick={(pick) => onPickIcon(pick)}
          onRemove={() => onPickIcon(null)}
          onClose={() => setIconAnchor(null)}
        />
      )}
      {/* The checklist, and the task that fixes it. It reports its own errors
          inside the dialog — a failure to create the fix task is about the
          dialog you are standing in, not about this page. */}
      {doctorOpen && (
        <AppDoctorModal dir={dir} onClose={() => setDoctorOpen(false)} />
      )}

      <div className="app-page-body">
        {/* Controlled by the URL and ONLY the URL: no onValueChange, so a
            ctrl/middle-click on a trigger opens the address elsewhere without
            also switching this page. Real anchors under the triggers (base-ui's
            `render`), same reason as before — a tab is an address (D420). */}
        <Tabs value={tab} className="app-page-tabs flex-none">
          <TabsList
            variant="line"
            aria-label="App page"
            className="h-auto w-full justify-start rounded-none border-b border-border p-0 pb-1"
          >
            {visibleTabs.map((id) => {
              const { label, Icon } = TAB_DEFS[id];
              return (
                <TabsTrigger
                  key={id}
                  value={id}
                  className="flex-none px-4 py-2.5"
                  // Base UI assumes a native <button> unless told otherwise:
                  // without this the anchor gets type="button" and Space
                  // does not activate it (Bugbot on #851).
                  nativeButton={false}
                  render={
                    <a
                      href={appPageUrl(dir, id, location.search)}
                      onClick={(e) => pickTab(e, id)}
                    />
                  }
                >
                  <Icon data-icon="inline-start" />
                  {label}
                </TabsTrigger>
              );
            })}
          </TabsList>
        </Tabs>

        {resolved === undefined && (
          <SkeletonLines rows={2} label="Loading app" />
        )}
        {resolved?.kind === "missing" && (
          <ErrorBanner>
            No folder at <strong>{tildePath(dir, home)}</strong>.
          </ErrorBanner>
        )}
        {resolved?.kind === "error" && (
          <ErrorBanner>
            Could not open {slug}: {resolved.message}
          </ErrorBanner>
        )}

        {resolved?.kind === "app" &&
          APP_PAGE_TABS.map((id) => {
            const def = TAB_DEFS[id];
            const active = tab === id;
            if (!active && !def.keepMounted) return null;
            return (
              <section
                key={id}
                className={
                  "app-page-panel app-page-" + id + (active ? "" : " is-hidden")
                }
                role="tabpanel"
                aria-hidden={!active}
              >
                {def.render({ slug, dir, entry, folderHref, gitTpl })}
              </section>
            );
          })}
      </div>
    </div>
  );
}
