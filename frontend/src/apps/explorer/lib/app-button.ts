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
//   otherwise → "Open as app", at the FOLDER in `?_mode=app` when the status
//               says templates/app/condition.py will accept it, and at the
//               folder's own page when it will not.
//
// This is now the ONLY producer of `?_mode=app` in the shell. App CARDS
// deliberately do not pin the mode (platform/lib/appEntry) — a card opens the
// folder's plain listing and the user picks the view. This button is the
// opposite request, made explicitly: it says "show me this folder AS the app",
// so it names the mode.
//
// The decision is pure and the fetching is a hook beside it, so the rule can be
// pinned by a test while both surfaces share one implementation of it.
import { useEffect, useState } from "react";
import { getAppLinkStatus, linkApp, type AppLinkStatus } from "@platform/lib/api";
import { navigate } from "@platform/lib/router";
import { pushToast } from "@platform/lib/toast";

// The template an app folder opens in: the app itself, full-bleed
// (fused_render/templates/app). Named EXPLICITLY rather than left to the
// default — with `_mode` absent, Preview's defaultTemplate picks the first
// UNCONDITIONAL entry, which for a directory is `_listing`, i.e. the file list
// this button exists to avoid showing.
export const APP_OPEN_MODE = "app";

// Where an "Open as app" goes — an ordinary fs navigation, since there is no
// app URL namespace any more: the folder in the app mode, or its lone page.
export type AppButtonTarget = { path: string; isDir: boolean; mode?: string };

export type AppButtonSpec =
  | { action: "link"; label: string }
  | { action: "open"; label: string; target: AppButtonTarget };

// The button for a folder, or null when there shouldn't be one:
//   • `folder`/`appFile` null — no folder in view, or it holds no single
//     unambiguous top-level page, so it is not an app in this sense at all;
//   • `link` null — the link-status probe is still in flight. A button that
//     appears and then changes its own label under the cursor is worse than
//     one that arrives a beat late.
export function appButtonSpec(
  folder: string | null,
  appFile: string | null,
  link: AppLinkStatus | null,
): AppButtonSpec | null {
  if (!folder || !appFile || !link) return null;
  if (link.status === "unlinked") return { action: "link", label: "Add as app" };
  // Both halves of the identity present is the SERVER's answer to "will
  // templates/app/condition.py offer the `app` mode for this folder" — the
  // handler mirrors that gate clause for clause, mount refusal included
  // (server/routers/apps.py::api_link_status), rather than merely describing
  // the folder. A half-known identity (an older backend that reports no `tag`,
  // a folder nested deeper than <tag>/<name>, a path on a mount) means the gate
  // refuses, and asking for a mode it refuses gets `_listing` — the file list,
  // which is what this button exists to avoid.
  if (link.tag && link.name) {
    return {
      action: "open",
      label: "Open as app",
      target: { path: folder, isDir: true, mode: APP_OPEN_MODE },
    };
  }
  // Fallback: the folder's own PAGE, which renders as the app. Deliberately not
  // the folder — a folder whose mode the gate would refuse is its file listing.
  return { action: "open", label: "Open as app", target: { path: appFile, isDir: false } };
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

  const spec = appButtonSpec(folder, appFile, link);
  // `folder` is re-tested only to narrow it for `linkApp` below — a non-null
  // spec already implies it.
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
    onClick: () => navigate(target.path, { isDir: target.isDir, mode: target.mode }),
  };
}
