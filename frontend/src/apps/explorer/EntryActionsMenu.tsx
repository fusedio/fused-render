// THE BAR'S KEBAB — the file preview's crumb bar, and the folder listing's
// search row (which IS the bar over a folder): every app-level action that used to
// stand in that bar as its own bordered button — App Doctor, Export App, Open in
// project — plus the fullscreen glyph and the MCP companion, in one `⋮`
// (BarMenu's OverflowMenu). Four labelled buttons and two glyphs in a 28px strip
// was a toolbar competing with the mode control for the bar; the mode control is
// what the bar is FOR, and these are things you do to the app once in a while.
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
//   * Export keeps its `busy || snapshotPending || snapshotError` guard and its
//     `.preview-frame.is-shown` capture source (see the row's comments);
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
import { useEffect, useState } from "react";
import { Plug, Stethoscope } from "lucide-react";
import { addCurrentApp, getAppEntry, statPath } from "@platform/lib/api";
import { exportAppFile } from "@platform/lib/appShot";
import { AppDoctorModal } from "@platform/ui/AppDoctorModal";
import { AppDoctorStatusDot } from "@platform/ui/AppDoctorStatusDot";
import { useAppDoctorChecks } from "@platform/ui/useAppDoctorChecks";
import { announceCurrentAppsChanged } from "@platform/lib/tasksChanged";
import { navigateUrl, encodeFsPathSegments } from "@platform/lib/router";
import { basename } from "@platform/lib/format";
import { pushToast } from "@platform/lib/toast";
import { useAppVersionLabel } from "@platform/lib/appVersionLabel";
import type { ResolvedSnapshot } from "@platform/lib/snapshot-param";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { OverflowMenu, type OverflowEntry } from "@apps/explorer/BarMenu";

// lucide at MenuIcons' own weight (1.5 on a 24 grid, 16px) so the two rows that
// have no MenuIcons glyph sit in the list at the same stroke as the rest.
const LUCIDE = { size: 16, strokeWidth: 1.5, "aria-hidden": true } as const;

// The server answers os.path.abspath (backslashes on Windows) while fsPath is
// the shell's canonical forward-slash form — same drive-letter-only
// normalization rule as the URL codec (a backslash in a POSIX filename must not
// be rewritten).
const canon = (p: string) => (/^[A-Za-z]:[\\/]/.test(p) ? p.replace(/\\/g, "/") : p);

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

export function EntryActionsMenu({
  fsPath,
  isEntry: isEntryKnown,
  snapshotSha = null,
  snapshotResolved = null,
  snapshotPending = false,
  snapshotError = false,
  onOpenEmbed,
  mcp,
  onOpenMcp,
}: EntryActionsMenuProps) {
  const dir = fsPath.slice(0, fsPath.lastIndexOf("/")) || "/";
  const name = basename(dir);
  const [isEntryProbed, setIsEntry] = useState(false);
  const isEntry = isEntryKnown ?? isEntryProbed;
  const [doctorOpen, setDoctorOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
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

  // Mirrors AppPage.tsx's `exportDisabled`: a click landing mid-resolve, before
  // `snapshotResolved.dir` exists, must not fall through to exporting the LIVE
  // folder while the pane still shows the version being resolved.
  const exportDisabled = exporting || snapshotPending || snapshotError;
  const doExport = async () => {
    if (exportDisabled) return;
    setExporting(true);
    try {
      const isLive = snapshotSha === null;
      // The snapshot's OWN extracted tree (`snap.dir`), never `snap.app_dir`
      // (the LIVE folder the sha resolved from) — exporting that would silently
      // ship the live app labelled as the picked commit.
      const exportPath = snapshotResolved ? snapshotResolved.dir : dir;
      // The filename carries the version so a v7 export sitting beside a live
      // export in Downloads is never ambiguous about which is which.
      const exportName = isLive ? name : `${name}-${versionLabel}`;
      // Same capture-on-export as the /apps card (appShot, D396): the shown
      // preview frame IS the app rendering, so it is the crop source — no
      // navigation, no flash. exportAppFile itself skips capture when the folder
      // carries an authored preview.png; the probe below is only so a pointless
      // native shot (and, on a Mac that has not granted Screen Recording, its
      // permission dialog) isn't taken for a capture the server would discard
      // anyway (stat failure reads as "no authored still" — worst case is that
      // redundant shot, never a lost export).
      //
      // `.is-shown` satisfies appShot's crop-source contract (pixels that ARE
      // the app, not a box it may fill): the class rides `shown`, which the
      // frame swap only sets once that frame paints. Only checked for a LIVE
      // export: a snapshot's preview.png (if any) lives under the extracted
      // tree, and `entry_html` is omitted below for a snapshot anyway.
      const authored = isLive
        ? await statPath(dir + "/preview.png").then(
            (s) => !s.is_dir,
            () => false,
          )
        : false;
      await exportAppFile(
        {
          path: exportPath,
          name: exportName,
          // Omitted for a snapshot export: with no on-screen capture element
          // threaded to this target folder, `exportAppFile`'s stage fallback
          // would reload the ENTRY PAGE'S LIVE copy to shoot it — a present-day
          // screenshot baked into a file labelled as the old commit.
          entry_html: isLive ? fsPath : undefined,
          preview_image: isLive && authored ? dir + "/preview.png" : null,
        },
        isLive ? document.querySelector(".preview-frame.is-shown") : null,
      );
    } catch (e) {
      pushToast({ msg: "Could not export " + name + ": " + (e as Error).message, tone: "error" });
    } finally {
      setExporting(false);
    }
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

  const entryRows: OverflowEntry[] = isEntry
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
        {
          label: exporting ? "Exporting…" : "Download app",
          icon: exporting ? <span className="mode-icon-spinner" /> : MenuIcons.download,
          title: "Export " + name + " as a single .fused app file",
          disabled: exportDisabled,
          onClick: () => void doExport(),
        },
        {
          label: "Open as project",
          icon: MenuIcons.open,
          title: "Open " + name + " as a project",
          onClick: () => void openProject(),
        },
        "separator",
      ]
    : [];

  const items: OverflowEntry[] = [
    ...entryRows,
    ...(onOpenEmbed
      ? [
          {
            label: "Open in embed",
            icon: MenuIcons.newTab,
            title: "Open this page in a new tab, without the sidebar and toolbar",
            onClick: onOpenEmbed,
          } satisfies OverflowEntry,
        ]
      : []),
    ...(mcp
      ? [
          {
            label: "MCP config",
            icon: mcp.pending ? <span className="mode-icon-spinner" /> : <Plug {...LUCIDE} />,
            title: mcp.pending
              ? "Checking if this folder publishes MCP tools…"
              : mcp.available
                ? "The MCP tools " + name + " publishes"
                : mcp.reason ?? "This folder publishes no MCP tools",
            disabled: mcp.pending || !mcp.available,
            onClick: onOpenMcp,
          } satisfies OverflowEntry,
        ]
      : []),
  ];

  // A trailing separator with nothing after it (an entry page over a surface
  // with neither embed nor MCP) would draw a rule under the last row.
  while (items.length && items[items.length - 1] === "separator") items.pop();

  // NOTHING QUALIFIES, NO KEBAB: OverflowMenu already renders nothing for an
  // empty list, so a plain folder that is not an app and publishes no MCP gets
  // no `⋮` at all rather than a menu that opens on nothing.
  return (
    <>
      <OverflowMenu
        items={items}
        title="App actions"
        badge={isEntry ? <AppDoctorStatusDot checks={doctorChecks} /> : undefined}
      />
      {doctorOpen && <AppDoctorModal dir={dir} onClose={() => setDoctorOpen(false)} />}
    </>
  );
}

export default EntryActionsMenu;
