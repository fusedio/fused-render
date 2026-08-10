// "Open as app" / "Add as app" — the button a FOLDER THAT HOLDS A LONE HTML
// PAGE gets, and the one place that decides what it says and where it goes.
//
// It is rendered on two surfaces: the title bar, for the folder currently open
// (Preview), and the preview pane's header, for a folder SELECTED in the
// listing (ListingPreviewPane). They are the same action one level apart, and
// they used to answer this separately — the pane knew only that the folder had
// a page, so it could only ever say "Open as app", and for an unlinked folder
// that promise could not be kept: templates/app/condition.py offers the app
// view only for <workspace>/<tag>/<project> or a folder in the linked-app
// registry. Everywhere else `_mode=app` resolves to nothing and the folder
// falls back to its own `_listing` — the file list, which is exactly what the
// button exists to avoid showing.
//
// So the button has two states, and which one it is in is a question about the
// REGISTRY, not about the listing:
//
//   unlinked  → "Add as app". Register the folder (linkApp), which puts it on
//               the Home grid and makes the app view available. The button
//               then flips to "Open as app" in place — no reload, because the
//               status it reads is state, not a fetch cache.
//   otherwise → "Open as app", at the BUILDER ROUTE (/apps/<tag>/<name>) in
//               the app view when the status carries the identity that route
//               needs, and at the folder's own page when it does not.
//
// The decision is pure and the fetching is a hook beside it, so the rule can be
// pinned by a test while both surfaces share one implementation of it.
import { useEffect, useState } from "react";
import { getAppLinkStatus, linkApp, type AppLinkStatus } from "@platform/lib/api";
import { APP_OPEN_MODE, appRouteUrl } from "@platform/lib/appEntry";
import { navigate, navigateUrl } from "@platform/lib/router";
import { pushToast } from "@platform/lib/toast";

// Where an "Open as app" goes. A route is a whole shell URL (the builder
// namespace, which is not an fs path); a path is an ordinary navigation.
export type AppButtonTarget =
  | { kind: "route"; url: string }
  | { kind: "path"; path: string; isDir: boolean };

export type AppButtonSpec =
  | { action: "link"; label: string }
  | { action: "open"; label: string; target: AppButtonTarget };

// The button for a folder, or null when there shouldn't be one:
//   • `appFile` null — the folder holds no single unambiguous top-level page,
//     so it is not an app in this sense at all;
//   • `link` null — the link-status probe is still in flight. A button that
//     appears and then changes its own label under the cursor is worse than
//     one that arrives a beat late.
export function appButtonSpec(
  appFile: string | null,
  link: AppLinkStatus | null,
): AppButtonSpec | null {
  if (!appFile || !link) return null;
  if (link.status === "unlinked") return { action: "link", label: "Add as app" };
  // The builder route needs BOTH halves of the identity; a half-known one (an
  // older backend that reports no `tag`, a workspace folder that isn't exactly
  // an app dir) cannot build /apps/<tag>/<name>.
  if (link.tag && link.name) {
    return {
      action: "open",
      label: "Open as app",
      target: {
        kind: "route",
        // `_mode=app` is not optional: without it the destination takes its own
        // default template, and a directory's default is `_listing`.
        url: appRouteUrl({ tag: link.tag, name: link.name }) + "?_mode=" + APP_OPEN_MODE,
      },
    };
  }
  // Fallback: the folder's own PAGE, which renders as the app. Deliberately not
  // the folder — a folder with no mode is its file listing.
  return { action: "open", label: "Open as app", target: { kind: "path", path: appFile, isDir: false } };
}

// The button as a surface can render it: a label and a click, or null. Both
// callers pass their own folder + lone-page facts and their own CSS class; the
// behaviour is entirely here.
//
// `folder` null switches the whole thing off (a file target, a non-listing
// view), and no probe is made for it.
export function useAppButton(
  folder: string | null,
  appFile: string | null,
): { label: string; onClick: () => void } | null {
  // How the folder relates to the app system. A fetch failure (older backend)
  // resolves as "workspace" so the button degrades to a plain "Open as app"
  // rather than vanishing or offering to link a folder that may already be one.
  const [link, setLink] = useState<AppLinkStatus | null>(null);
  useEffect(() => {
    setLink(null);
    if (!folder || !appFile) return;
    let stale = false;
    getAppLinkStatus(folder).then(
      (s) => {
        if (!stale) setLink(s);
      },
      () => {
        if (!stale) setLink({ status: "workspace", name: null });
      },
    );
    return () => {
      stale = true;
    };
  }, [folder, appFile]);

  const spec = appButtonSpec(appFile, link);
  if (!spec || !folder) return null;

  if (spec.action === "link") {
    return {
      label: spec.label,
      onClick: () => {
        void linkApp(folder).then(
          ({ app }) => {
            // Straight into state: the button flips to "Open as app" in place,
            // and the identity it just learned is what that open uses.
            setLink({ status: "linked", name: app.name, tag: app.tag });
            pushToast({ msg: `Linked as app “${app.name}” — it's on the Home grid now`, tone: "info" });
          },
          (e: Error) => pushToast({ msg: e.message, tone: "error" }),
        );
      },
    };
  }
  const { target } = spec;
  return {
    label: spec.label,
    onClick: () => {
      if (target.kind === "route") navigateUrl(target.url, { isDir: true });
      else navigate(target.path, { isDir: target.isDir });
    },
  };
}
