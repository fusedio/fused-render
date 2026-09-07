// The top-level embed's one piece of chrome.
//
// `/explorer/embed/<path>` is chrome-free by design (D39/D390): no sidebar,
// no crumb, no preview header. That is right for every FRAMED embed — a panel
// pane, a tab, a bookmark card, the canvases workspace — where a host owns the
// chrome. It is wrong for the embed that IS the window: a Finder double-click
// on a `.fused` (the view-URL codec lands OS opens on the embed prefix), a CLI
// or deeplink embed URL, a pasted link. There the user is stranded — nothing
// on screen leads back to the explorer, and D397's Clone (the only way from a
// read-only `.fused` to an editable copy) sat in the hidden header. That gap
// was recorded as accepted in D390/D397; this strip is the owner reversing it.
//
// WHERE: rendered by StatView above `#content`, gated on IS_TOP_EMBED
// (router.ts) — never inside Preview, whose header is CSS-hidden in embed, and
// never inside the fusedapp template, which frames the entry page as a second
// embed of its own (only the outer shell is top-level).
//
// WHAT: "Open in explorer" for any target — the same path under the VIEW
// prefix, full shell chrome; for a FOLDER embed (an app dir opened from the
// CLI or a link) it opens the folder's app ENTRY page (`/api/apps/entry`, the
// one rule in app_listing), not the listing: the user was looking at the app,
// and the explorer should show them that same page with its chrome, falling
// back to the folder only when there is no entry — plus, for a `.fused`, the shared
// CloneAppFileButton (one control, one label rule: "Clone" / "Go to local
// version") told to land on the view URL, since `navigate` would keep the
// embed prefix and drop the clone folder into a chrome-free listing with no
// way out (D282's dead end). Export, Migrate and Reveal in Finder are
// deliberately absent (owner): the first two they act on an app's entry FOLDER, not on
// a read-only artifact, and the view-mode header already has them; Reveal
// belongs to the explorer's own menus once you are there.
//
// DISMISS is per page load — component state, nothing persisted. The strip is
// the only route to Clone from an opened `.fused`; a remembered "✕" would
// rebuild exactly the dead end it exists to close. Same "not now, not never"
// posture as FdaStrip.
import { useState } from "react";

import { getAppEntry } from "@platform/lib/api";
import { IS_TOP_EMBED, viewUrlForFsPath } from "@platform/lib/router";
import { MenuIcons } from "@platform/ui/MenuIcons";

import { CloneAppFileButton } from "@apps/explorer/Preview";

export default function EmbedStrip({ fsPath, isDir }: { fsPath: string; isDir: boolean | null }) {
  const [dismissed, setDismissed] = useState(false);
  if (!IS_TOP_EMBED || dismissed) return null;
  const isFused = isDir === false && fsPath.toLowerCase().endsWith(".fused");
  const name = fsPath.split("/").filter(Boolean).pop() || fsPath;
  // Full page load, not `navigate`: the prefix (embed vs view) is read once
  // at module init, so switching it IS a new document. A folder resolves to
  // its entry page first; an entry-less or unreadable folder opens as itself.
  const openInExplorer = async () => {
    let target = fsPath;
    if (isDir) {
      try {
        const info = await getAppEntry(fsPath);
        if (info.entry) target = info.entry;
      } catch {
        /* no entry answer — the folder itself is still the right place */
      }
    }
    location.assign(viewUrlForFsPath(target));
  };
  return (
    <div className="embed-strip" role="toolbar" aria-label="Embedded view">
      <span className="embed-strip-name" title={fsPath}>
        {name}
        {isFused && <span className="embed-strip-note">read-only app file</span>}
      </span>
      <div className="embed-strip-actions">
        <button
          type="button"
          className="bar-ctl bar-ctl-bordered"
          title="Open this page in the explorer, with the sidebar and toolbar"
          onClick={() => void openInExplorer()}
        >
          {MenuIcons.open}
          Open in explorer
        </button>
        {isFused && <CloneAppFileButton fsPath={fsPath} toView />}
        <button
          type="button"
          className="bar-ctl embed-strip-close"
          title="Hide this bar"
          aria-label="Hide this bar"
          onClick={() => setDismissed(true)}
        >
          ✕
        </button>
      </div>
    </div>
  );
}
