// The in-shell half of `fused-render://open?url=` (SPEC §26 DL-8): a web
// page's "Open in fused-render" link to a hosted `.fused`. GET /clone
// redirected to Home with the http(s) link in `?_fetch_appfile=` — or, for
// `open?git=` (DL-9), the GitHub repo link in `?_fetch_appgit=`, which the
// same POST builds into a `.fused` from the repo's one app. Mounted
// once in the top document (`!IS_EMBED` guard); it reads the param on first
// mount, strips it BEFORE any async work (so a reload mid-download or a Back
// never re-runs the hand-off), then downloads through the guarded
// POST /api/appfile/fetch — into ~/.fused-render/downloads/<app_id>.fused,
// one file per app however many links point at it — and opens the saved
// file the way a Finder double-click on a `.fused` does (D390): THIS document
// hard-loads the file's chrome-free EMBED url, where the `fusedapp` template
// shows its INSTALL PAGE (AF-14). Nothing in the file runs until the user
// installs it there; the page is the confirm.
//
// The same document load inside a native window. A deep link always arrives in a FRESH window (app.py `_open_target`
// → `WindowManager.open`), which the 303 parked on Home; loading the embed
// into that same window makes it the file's window, and the install's own
// navigation then turns it into the app's — the URL observer re-keys it
// (mac_window.py KVO) and the title-bar Edit button lights up as the way into
// the explorer — exactly the Finder-open shape. Asking the server for a
// second window would leave this one orphaned on Home per click. In a browser
// tab the same load lands under the EmbedStrip (IS_TOP_EMBED), whose "Open in
// explorer" is the way out there.
//
// The download itself is reported as a transient toast so a slow link is not
// a blank Home. BOOT_URL is read at module init from the URL the redirect
// landed on, and `consumed` keeps the second mount (the setup wizard route
// also mounts this) from replaying it.
//
// The same component reports a MALFORMED `fused-render://open?file=` link
// (DL-7): the server ferries a payload it could not parse to Home under
// `?_edit_appfile=` so the error surfaces in-app; a well-formed one is
// redirected straight to the file's install page and never reaches here.
import { useEffect } from "react";

import { fetchAppFile } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import { currentUrl, embedUrlForFsPath, replaceSearch } from "@platform/lib/router";

import {
  editAppFileFromSearch,
  fetchAppFileFromSearch,
  fetchAppGitFromSearch,
  withoutEditAppFile,
  withoutFetchAppFile,
  withoutFetchAppGit,
} from "./edit-appfile-lib";

const BOOT_URL: string | null =
  typeof location === "undefined" ? null : fetchAppFileFromSearch(location.search);
const BOOT_GIT: string | null =
  typeof location === "undefined" ? null : fetchAppGitFromSearch(location.search);
const BOOT_BAD_FILE: string | null =
  typeof location === "undefined" ? null : editAppFileFromSearch(location.search);
let consumed = false;

export default function FetchAppFileBoot() {
  useEffect(() => {
    if (consumed) return;
    const url = BOOT_URL;
    const git = BOOT_GIT;
    const bad = BOOT_BAD_FILE;
    if (!url && !git && !bad) return;
    consumed = true;
    // Strip first: whatever happens below, this URL must not replay it.
    replaceSearch(withoutFetchAppGit(withoutFetchAppFile(withoutEditAppFile(currentUrl()))));
    if (bad) {
      notify({ title: "Could not open app file link: not an absolute .fused path — " + bad, tone: "error" });
    }
    if (url && !/^https?:\/\//i.test(url)) {
      // The server ferried a malformed payload verbatim so the error surfaces
      // here, in-app, rather than on a page of its own.
      notify({ title: "Could not open app link: not an http(s) URL — " + url, tone: "error" });
      return;
    }
    // A malformed repo link is reported by the server's own validation.
    const src = git ? { git } : url ? { url } : null;
    if (!src) return;
    let alive = true;
    const toast = notify({ title: "Downloading app from " + (git || url) + "…", tone: "info" });
    (async () => {
      try {
        const r = await fetchAppFile(src);
        if (!alive) return;
        notify({ title: "Opening app file", tone: "info" }, toast);
        // A full load, not navigateUrl: the embed-vs-view prefix (IS_EMBED,
        // IS_TOP_EMBED) is read once at module init, so a pushState from Home
        // onto the embed url would keep the shell's chrome and never mount
        // the EmbedStrip — the same rule EmbedStrip applies going back.
        location.assign(embedUrlForFsPath(r.file));
      } catch (e) {
        if (!alive) return;
        notify(
          { title: "Could not download app: " + ((e as Error).message || "download failed"), tone: "error" },
          toast,
        );
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return null;
}
