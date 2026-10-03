// The in-shell half of `fused-render://open?url=` (SPEC §26 DL-8): a web
// page's "Open in fused-render" link to a hosted `.fused`. GET /clone
// redirected to Home with the http(s) link in `?_fetch_appfile=`. Mounted
// once in the top document beside `EditAppFileBoot` (same `!IS_EMBED`
// guard); it reads the param on first mount, strips it BEFORE any async work
// (so a reload mid-download or a Back never re-runs the hand-off), then
// downloads through the guarded POST /api/appfile/fetch — into
// ~/.fused-render/downloads/<app_id>.fused, one file per app however many
// links point at it — and opens the saved file AS AN APP, the way a Finder
// double-click on a `.fused` does (D390): inside a native window the app gets
// a window of its own (native-window.ts, title-bar Edit is the way into the
// explorer); anywhere else the top document hard-loads the file's chrome-free
// EMBED url, where the `fusedapp` template runs it and the EmbedStrip's
// "Open in explorer" is the way out. Never the explorer's view page: the
// link said "open the app", not "show me its file".
//
// No confirm step, by owner decree (Render App's lite PR #30 is the
// reference): the link click is the gesture, and the page the user lands on
// is the app, not a question about it. The download itself is reported as a
// transient toast so a slow link is not a blank Home.
//
// Same discipline as EditAppFileBoot: BOOT_URL is read at module init from the
// URL the redirect landed on, and `consumed` keeps the second mount (the
// setup wizard route also mounts this) from replaying it.
import { useEffect } from "react";

import { fetchAppFile } from "@platform/lib/api";
import { openAppWindow } from "@platform/lib/native-window";
import { notify } from "@platform/lib/notifications";
import { IS_NATIVE_WINDOW, currentUrl, embedUrlForFsPath, replaceSearch } from "@platform/lib/router";

import { fetchAppFileFromSearch, withoutFetchAppFile } from "./edit-appfile-lib";

const BOOT_URL: string | null =
  typeof location === "undefined" ? null : fetchAppFileFromSearch(location.search);
let consumed = false;

export default function FetchAppFileBoot() {
  useEffect(() => {
    const url = BOOT_URL;
    if (!url || consumed) return;
    consumed = true;
    // Strip first: whatever happens below, this URL must not replay it.
    replaceSearch(withoutFetchAppFile(currentUrl()));
    if (!/^https?:\/\//i.test(url)) {
      // The server ferried a malformed payload verbatim so the error surfaces
      // here, in-app, rather than on a page of its own.
      notify({ title: "Could not open app link: not an http(s) URL — " + url, tone: "error" });
      return;
    }
    let alive = true;
    const toast = notify({ title: "Downloading app from " + url + "…", tone: "info" });
    (async () => {
      try {
        const r = await fetchAppFile(url);
        if (!alive) return;
        notify({ title: "Opening app", tone: "info" }, toast);
        // Own window when this is one (the Home window that took the redirect
        // stays, exactly as after an app-card click); in place otherwise.
        if (IS_NATIVE_WINDOW && (await openAppWindow(r.file))) return;
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
