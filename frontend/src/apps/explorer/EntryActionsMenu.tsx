// THE APP-LEVEL ROWS — every app-level action that used to stand in the crumb
// bar as its own bordered button — App Doctor, Export App, Open in project —
// plus the fullscreen glyph and the MCP companion. Four labelled buttons and
// two glyphs in a 28px strip was a toolbar competing with the mode control for
// the bar; the mode control is what the bar is FOR, and these are things you
// do to the app once in a while.
//
// Two exports. `useAppActionRows` is the hook: it owns the entry probe, the
// App Doctor's checks and modal, the Share sheet's version logic, and returns
// the rows as ContextMenu MenuEntry[] in two groups (`app`, `open`) plus the
// trigger badge and the modal node. `EntryActionsMenu` is the file preview's
// kebab built on it (BarMenu's OverflowMenu). The FOLDER LISTING does not use
// the component: it takes the hook's groups and composes them with its own
// folder ops into the one folder menu (bar-menus' folderMenu) that its kebab,
// its background right-click and the crumb bar all show — so the same row is
// never spelled twice.
//
// ONE ENTRY PROBE. The three buttons each asked /api/apps/entry whether the
// previewed page is its folder's app entry (the server's own entry rule, never
// the filename) and each hid itself when it was not. That is one question asked
// three times per file; it is asked once here, and the answer gates the three
// entry-only rows together. The two rows that are not about "the app" — Open in
// embed, MCP config — are gated on their own facts (always, and whether the parent
// folder offers an MCP companion), so a plain html file still gets a kebab.
//
// The App Doctor's STATUS DOT rides the trigger's corner while the menu is shut
// (OverflowMenu's `badge`): its whole job is to be seen without a click, and a
// dot on a row inside a closed menu is a dot nobody sees. It repeats on the row
// so the two agree.
//
// The bodies are the deleted buttons' bodies, verbatim where it matters:
//   * Share (which absorbed the Download row — the sheet behind it holds both
//     the public link and the .fused file) keeps Export's
//     `snapshotPending || snapshotError` guard and its `.preview-frame.is-shown`
//     capture source (see `doShare`);
//   * Open as project puts the folder on the sidebar's desk first, then
//     navigates in THIS tab, spelled by hand since an app may not import
//     shell/current-apps-lib;
//   * Open in embed is a NEW TAB now (`window.open`) — the old fullscreen button
//     replaced this document (`location.assign`), and a way out only through
//     EmbedStrip's "Open in explorer" is a poor trade for a menu row that says
//     "open"; the explorer stays where it was. Its URL is still the caller's
//     (Preview.tsx owns the `_mode` stamp rule).
//   * MCP config opens the parent folder's `mcp` template in a dialog
//     (McpDialog) rather than in the sidebar, whose two remaining companions are
//     tabs now (SideChrome's SideTabs). The dialog's open state lives in
//     Preview.tsx because Open With → MCP has to reach it too.
import { useEffect, useState, type ReactNode } from "react";
import { Plug, Stethoscope } from "lucide-react";
import { addCurrentApp, getAppEntry } from "@platform/lib/api";
import { openShareApp } from "@platform/lib/share-app";
import { AppDoctorModal } from "@platform/ui/AppDoctorModal";
import { AppDoctorStatusDot } from "@platform/ui/AppDoctorStatusDot";
import { useAppDoctorChecks } from "@platform/ui/useAppDoctorChecks";
import { announceCurrentAppsChanged } from "@platform/lib/tasksChanged";
import { navigateUrl, encodeFsPathSegments } from "@platform/lib/router";
import { basename } from "@platform/lib/format";
import { useAppVersionLabel } from "@platform/lib/appVersionLabel";
import type { ResolvedSnapshot } from "@platform/lib/snapshot-param";
import { MenuIcons } from "@platform/ui/MenuIcons";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { OverflowMenu } from "@apps/explorer/BarMenu";

// lucide at MenuIcons' own weight (1.5 on a 24 grid, 16px) so the two rows that
// have no MenuIcons glyph sit in the list at the same stroke as the rest.
const LUCIDE = { size: 16, strokeWidth: 1.5, "aria-hidden": true } as const;

// The server answers os.path.abspath (backslashes on Windows) while fsPath is
// the shell's canonical forward-slash form — same drive-letter-only
// normalization rule as the URL codec (a backslash in a POSIX filename must not
// be rewritten).
// Exported for the folder listing, which hands this component the server's own
// entry answer as `fsPath` and has to canonicalise it the same way first.
export const canonEntryPath = (p: string) =>
  /^[A-Za-z]:[\\/]/.test(p) ? p.replace(/\\/g, "/") : p;
const canon = canonEntryPath;

export interface EntryActionsMenuProps {
  // The app's ENTRY PAGE, or the page that might be one. Over a file preview
  // this is the previewed file and the component asks the server whether it is
  // the entry; over a folder listing the caller has already asked (Listing's
  // `appEntryPath`) and passes the answer as `isEntry`, with `fsPath` the entry
  // page it found — or `<folder>/index.html` as a stand-in when there is none,
  // so the folder is still what `dir` resolves to.
  fsPath: string;
  // Skip the probe: the caller knows. Undefined means "ask /api/apps/entry".
  isEntry?: boolean;
  // The pane's own `_snapshot` resolution (`usePreviewSnapshot`, hoisted in the
  // parent so the picker and Export agree on what "the previewed version" means)
  // — mirrors AppPage.tsx's `snapshot` and its Export control's use of it: export
  // the snapshot's OWN extracted tree, never the live folder, while one is
  // previewed. Absent on a surface with no snapshot machinery (the folder
  // listing, whose snapshot view never renders this menu — `paneEnabled`), where
  // the export is the live folder.
  snapshotSha?: string | null;
  snapshotResolved?: ResolvedSnapshot | null;
  snapshotPending?: boolean;
  snapshotError?: boolean;
  // Opens this page under the chrome-free embed prefix in a new tab. The URL
  // (and its `_mode` stamp) is Preview.tsx's rule, so it builds it. Omitted by
  // a surface with no embed of its own (the folder listing), and the row with it.
  onOpenEmbed?: () => void;
  // The MCP row. `available: false` when the parent folder offers no MCP
  // companion (not an app, a mount) — the row is then listed disabled with the
  // reason, since a menu that changes shape per file reads as broken; `pending`
  // while the parent's probe is out. OMITTED (undefined) by a surface that never
  // probes the parent at all — a panel/tab pane (Preview's `splitCapable` is
  // false there) — so the menu says nothing about MCP rather than "not an app"
  // over a folder that may well be one.
  mcp?: { available: boolean; pending: boolean; reason?: string };
  onOpenMcp: () => void;
}

// What the hook hands back. `app` is what this folder IS when it is an app
// (App Doctor, Share…, Open as project, MCP config); `open` is "this page
// elsewhere" (Open in embed) — the folder listing files each into the matching
// group of its folder menu. `isEntry` is the probe's (or the caller's) answer,
// `badge` rides the kebab trigger while the menu is shut, `modal` is the App
// Doctor dialog to render wherever the rows are shown.
export interface AppActionRows {
  app: MenuEntry[];
  open: MenuEntry[];
  isEntry: boolean;
  badge: ReactNode;
  modal: ReactNode;
}

export function useAppActionRows({
  fsPath,
  isEntry: isEntryKnown,
  snapshotSha = null,
  snapshotResolved = null,
  snapshotPending = false,
  snapshotError = false,
  onOpenEmbed,
  mcp,
  onOpenMcp,
}: EntryActionsMenuProps): AppActionRows {
  const dir = fsPath.slice(0, fsPath.lastIndexOf("/")) || "/";
  const name = basename(dir);
  const [isEntryProbed, setIsEntry] = useState(false);
  const isEntry = isEntryKnown ?? isEntryProbed;
  const [doctorOpen, setDoctorOpen] = useState(false);
  useEffect(() => {
    let alive = true;
    setIsEntry(false);
    setDoctorOpen(false);
    if (isEntryKnown !== undefined) return; // the caller answered; nothing to ask
    getAppEntry(dir)
      .then((r) => {
        if (alive) setIsEntry(r.entry != null && canon(r.entry) === fsPath);
      })
      .catch(() => {
        /* indeterminate reads as "not an entry" — no rows for nothing */
      });
    return () => {
      alive = false;
    };
  }, [fsPath, dir, isEntryKnown]);
  // Fetched after first paint, never blocking it — see useAppDoctorChecks.
  // Opening the modal re-fetches its own copy; this one is only for the dot and
  // is never reused to seed the dialog.
  const doctorChecks = useAppDoctorChecks(isEntry ? dir : null);
  const versionLabel = useAppVersionLabel(dir, snapshotSha);

  // Mirrors AppPage.tsx's `shareDisabled`: a click landing mid-resolve, before
  // `snapshotResolved.dir` exists, must not fall through to exporting the LIVE
  // folder while the pane still shows the version being resolved.
  const shareDisabled = snapshotPending || snapshotError;
  // One Share entry opens the unified sheet (ShareAppModal): the public link
  // and the `.fused` download as two cards. This is the one place that turns
  // "which version is previewed" into "which folder the FILE card exports".
  const doShare = async () => {
    if (shareDisabled) return;
    const isLive = snapshotSha === null;
    // The snapshot's OWN extracted tree (`snap.dir`), never `snap.app_dir`
    // (the LIVE folder the sha resolved from) — exporting that would silently
    // ship the live app labelled as the picked commit.
    const exportPath = snapshotResolved ? snapshotResolved.dir : dir;
    // The filename carries the version so a v7 export sitting beside a live
    // export in Downloads is never ambiguous about which is which.
    const exportName = isLive ? name : `${name}-${versionLabel}`;
    const live = { path: dir, name };
    openShareApp(live, {
      file: isLive ? live : { path: exportPath, name: exportName },
      // Live only — the shared canvas is named after the app's id and
      // always carries "the app", so a snapshot published under it would
      // downgrade every link out there. The sheet says so instead.
      link: isLive,
      versionLabel,
    });
  };

  // Put the folder on the sidebar's desk (POST /api/current-apps/add, a no-op
  // when the row is there already, which then reads as the active row) and open
  // the folder's app page, `/apps/<folder>`, in THIS tab.
  const openProject = async () => {
    try {
      await addCurrentApp(dir);
      announceCurrentAppsChanged();
    } catch {
      /* the page still opens; the row shows up on the next task under it */
    }
    navigateUrl("/apps/" + encodeFsPathSegments(dir));
  };

  const app: MenuEntry[] = isEntry
    ? [
        {
          label: "App Doctor",
          icon: <Stethoscope {...LUCIDE} />,
          title:
            "Check " + name +
            " before you share it: leaked credentials, paths tied to this machine, " +
            "stray generated files, uncommitted work, a stale fused API version",
          trailing: <AppDoctorStatusDot checks={doctorChecks} />,
          onClick: () => setDoctorOpen(true),
        },
        // The one Share entry: the sheet behind it offers the public link
        // (share_app.py) and the `.fused` download together (see `doShare`).
        {
          label: "Share…",
          icon: MenuIcons.share,
          title:
            snapshotSha === null
              ? "Share " + name + " — public link or .fused file"
              : "Share " + name + " as of " + versionLabel + " as a .fused file",
          disabled: shareDisabled,
          onClick: () => void doShare(),
        },
        {
          label: "Open as project",
          icon: MenuIcons.open,
          title: "Open " + name + " as a project",
          onClick: () => void openProject(),
        },
      ]
    : [];

  // The MCP row is worth listing DISABLED only where its absence is news: on
  // an app (an entry page exists, so "this app publishes no tools" says
  // something) or while the probe is still out. A plain folder that is not an
  // app would otherwise get a kebab that opens on one dead row.
  if (mcp && (mcp.available || mcp.pending || isEntry)) {
    app.push({
      label: "MCP config",
      icon: mcp.pending ? <span className="mode-icon-spinner" /> : <Plug {...LUCIDE} />,
      title: mcp.pending
        ? "Checking if this folder publishes MCP tools…"
        : mcp.available
          ? "The MCP tools " + name + " publishes"
          : mcp.reason ?? "This folder publishes no MCP tools",
      disabled: mcp.pending || !mcp.available,
      onClick: onOpenMcp,
    });
  }

  const open: MenuEntry[] = onOpenEmbed
    ? [
        {
          label: "Open in embed",
          icon: MenuIcons.newTab,
          title: "Open this page in a new tab, without the sidebar and toolbar",
          onClick: onOpenEmbed,
        },
      ]
    : [];

  return {
    app,
    open,
    isEntry,
    badge: isEntry ? <AppDoctorStatusDot checks={doctorChecks} /> : undefined,
    modal: doctorOpen ? <AppDoctorModal dir={dir} onClose={() => setDoctorOpen(false)} /> : null,
  };
}

// The FILE PREVIEW's kebab: the hook's rows in one `⋮`, the app group first and
// the embed row under a separator. Renders nothing at all when nothing
// qualifies (OverflowMenu on an empty list), so a plain html file that is not
// an entry and whose folder publishes no MCP gets no `⋮` rather than a menu
// that opens on nothing.
export function EntryActionsMenu(props: EntryActionsMenuProps) {
  const rows = useAppActionRows(props);
  const items: MenuEntry[] = [
    ...rows.app,
    ...(rows.app.length && rows.open.length ? (["separator"] as MenuEntry[]) : []),
    ...rows.open,
  ];
  return (
    <>
      <OverflowMenu items={items} title="App actions" badge={rows.badge} />
      {rows.modal}
    </>
  );
}

export default EntryActionsMenu;
