// One Fused App running live inside a Home widget. The app is looked up in the
// full apps list by `widget.appPath`; its page is framed through the chrome-free
// embed route. `_preview=1` is deliberately NOT added — see SPEC-home-widgets.md
// ("App embed widget"): it would make a Claude chat inside the app read-only.
import { useEffect, useState } from "react";
import { getApps, statPath, type AppInfo, type HttpError } from "@platform/lib/api";
import { hrefFor, isBrowserHandledClick, openApp, openTargetFor } from "@platform/lib/appEntry";
import { useNearViewport } from "@platform/lib/preview-start";
import { embedUrlForFsPath, urlForFsPath } from "@platform/lib/router";
import type { Widget } from "../layout";
import { pageTitle } from "../appPicker";
import { softNavigate } from "../strip";
import { EmptyLine, ErrorLine } from "./bits";

// One shared fetch for every app widget on a page and the frame's title lookup.
// Kept a few seconds so siblings mounting together share it, never longer: an
// app created after that must not read as "removed". A failure is never kept.
const APPS_TTL_MS = 5000;
let appsCache: { at: number; p: Promise<AppInfo[]> } | null = null;

export function loadAllApps(): Promise<AppInfo[]> {
  if (!appsCache || Date.now() - appsCache.at > APPS_TTL_MS) {
    const entry = { at: Date.now(), p: getApps().then((r) => r.apps) };
    appsCache = entry;
    entry.p.catch(() => {
      if (appsCache === entry) appsCache = null;
    });
  }
  return appsCache.p;
}

export type AppsState = { apps: AppInfo[] | null; error: boolean; retry: () => void };

export function useAllApps(enabled = true): AppsState {
  const [apps, setApps] = useState<AppInfo[] | null>(null);
  const [error, setError] = useState(false);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    loadAllApps().then(
      (a) => alive && (setApps(a), setError(false)),
      () => alive && (setApps(null), setError(true)),
    );
    return () => {
      alive = false;
    };
  }, [nonce, enabled]);
  return {
    apps,
    error,
    retry: () => {
      setError(false);
      setApps(null);
      setNonce((n) => n + 1);
    },
  };
}

/** The app record a widget points at, for the frame's title and header link.
    `undefined` while loading or failed, `null` once the list says it is gone. */
export function useWidgetApp(widget: Widget): AppInfo | null | undefined {
  const { apps } = useAllApps(widget.source === "app");
  if (widget.source !== "app" || !apps) return undefined;
  return apps.find((a) => a.path === widget.appPath) ?? null;
}

export function appName(app: AppInfo): string {
  return app.title || app.name;
}

export { pageTitle };

export function OpenPageLink({ path }: { path: string }) {
  const href = urlForFsPath(path);
  return (
    <a className="home-sec-more" href={href} onClick={(e) => softNavigate(e, href)}>
      Open ↗
    </a>
  );
}

export function OpenAppLink({ app }: { app: AppInfo }) {
  return (
    <a
      className="home-sec-more"
      href={hrefFor(app)}
      onClick={(e) => {
        if (e.defaultPrevented || isBrowserHandledClick(e)) return;
        e.preventDefault();
        openApp(app);
      }}
    >
      Open ↗
    </a>
  );
}

export function AppEmbedWidget({
  widget,
  edit,
  onRemove,
}: {
  widget: Widget;
  edit: boolean;
  onRemove: () => void;
}) {
  const { apps, error, retry } = useAllApps();
  const [ref, near] = useNearViewport<HTMLDivElement>();
  const [loaded, setLoaded] = useState(false);
  const app = apps?.find((a) => a.path === widget.appPath) ?? null;
  const path = widget.appPath ?? "";
  // The apps list only decides app vs plain path. A path widget frames at once;
  // if the list later says it IS an app, src flips to the entry page and the
  // frame reloads once. The entry page (the folder otherwise) is the same rule
  // hrefFor/Open uses; framing an app folder renders a directory listing.
  const src = app ? embedUrlForFsPath(openTargetFor(app).path) : path ? embedUrlForFsPath(path) : null;
  const [gone, setGone] = useState(false);
  useEffect(() => {
    setGone(false);
    if (apps === null || app || !path) return;
    let alive = true;
    statPath(path).catch((e: HttpError) => {
      if (alive && (e.status === undefined || e.status === 404)) setGone(true);
    });
    return () => {
      alive = false;
    };
  }, [apps, app, path]);
  // A new frame (another app, or remounted after scrolling away) starts unpainted.
  useEffect(() => {
    setLoaded(false);
  }, [src, near]);

  let inner;
  if (gone) {
    inner = (
      <div className="hw-error">
        <EmptyLine>This page is gone.</EmptyLine>
        <button type="button" className="hw-btn" onClick={onRemove}>
          Remove widget
        </button>
      </div>
    );
  } else if (!src && error) {
    inner = <ErrorLine message="Couldn't load apps." onRetry={retry} />;
  } else if (!src) {
    inner = <span className="hw-app-skel skel-bar" role="status" aria-busy="true" aria-label="Loading app" />;
  } else {
    inner = (
      <div className="hw-app-frame">
        {!loaded ? <span className="hw-app-skel skel-bar" role="status" aria-busy="true" aria-label="Loading app" /> : null}
        {near ? (
          <iframe
            className={"hw-app-iframe" + (loaded ? " is-loaded" : "")}
            src={src}
            title={app ? appName(app) : pageTitle(path)}
            onLoad={() => setLoaded(true)}
            onError={() => setLoaded(true)}
          />
        ) : null}
        {edit ? <div className="hw-app-shield" aria-hidden="true" /> : null}
      </div>
    );
  }
  return (
    <div ref={ref} className="hw-body hw-app-body">
      {inner}
    </div>
  );
}
