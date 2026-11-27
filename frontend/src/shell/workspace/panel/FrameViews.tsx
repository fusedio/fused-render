// THE PANEL'S FRAMED VIEWS (SPEC §56 WS-19): Bots (the real Bots page), the
// Git companion of a folder, and a file drawn in one of its modes — each a
// slim head (the hide, a title) over one frame. A frame here sits directly
// under the host, which is already a param boundary, so no PeekFrame.
import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import { NAVIGATE_URL_EVENT, type NavDetail } from "@platform/lib/router";
import { TAB_VISIBILITY_EVENT } from "@platform/lib/pause-while-hidden";
import { attachEmbedUrlChange, detachEmbedUrlChange, embedSrc, type UrlChangeHook } from "@platform/lib/layout-codec";
import { basename, dirname } from "@platform/lib/format";
import { useDirMode } from "@apps/explorer/lib/dir-mode";
import { modeTitle } from "@apps/explorer/ModeSwitcher";
import { readFrameLoc } from "../workspace-lib";

/** The head every non-chat view wears: the hide, then the title — the
 *  chat head's height and paddings (workspace.css `.ws-panel-head`). */
export function PanelHead({ title, onHide }: { title: string; onHide: () => void }) {
  return (
    <div className="ws-panel-head ws-panel-slimhead">
      <button type="button" className="task-head-btn task-head-lead is-close" aria-label="Hide the panel" data-hint="Hide the panel" onClick={onHide}>
        <X size={14} strokeWidth={2} aria-hidden="true" />
      </button>
      <span className="ws-panel-title">{title}</span>
    </div>
  );
}

/** THE BOTS PAGE in a `panel` frame. Its live URL is the view's (a guarded
 *  replace, `onSync`); a view URL changed from outside (a bot card's Open,
 *  Back) is navigated INSIDE the frame by its own router, never reloaded.
 *  KEPT while another view shows (WorkspacePanel's header): `url` is null
 *  then, and the frame reads as hidden to its polls, as a hidden tab does. */
export function BotsView({
  url,
  initialUrl,
  onSync,
  onHide,
}: {
  url: string | null;
  initialUrl: string;
  onSync: (url: string) => void;
  onHide: () => void;
}) {
  const ref = useRef<HTMLIFrameElement | null>(null);
  // Frozen at mount: the frame's src is set once (stage 1's TM-5 rule).
  const src = useRef(initialUrl).current;
  const hook = useRef<UrlChangeHook | null>(null);
  const onSyncRef = useRef(onSync);
  onSyncRef.current = onSync;
  const shown = url !== null;
  const shownRef = useRef(shown);
  shownRef.current = shown;
  useEffect(() => () => detachEmbedUrlChange(hook.current), []);
  // An outside change of the view: the frame goes there by its router.
  useEffect(() => {
    const iframe = ref.current;
    const win = iframe?.contentWindow;
    const loc = readFrameLoc(iframe);
    if (url === null || !win || loc === null || loc === url) return;
    try {
      const detail: NavDetail = { fn: "navigateUrl", url };
      win.dispatchEvent(new (win as Window & typeof globalThis).CustomEvent(NAVIGATE_URL_EVENT, { detail }));
    } catch {
      /* mid-load: its own load reports where it is */
    }
  }, [url]);
  // Hidden to its polls while another view shows (pause-while-hidden).
  useEffect(() => signalHidden(ref.current, !shown), [shown]);
  const onLoad = () => {
    const iframe = ref.current;
    if (!iframe) return;
    iframe.classList.add("is-loaded");
    signalHidden(iframe, !shownRef.current);
    detachEmbedUrlChange(hook.current);
    const report = () => {
      const loc = readFrameLoc(iframe);
      if (loc) onSyncRef.current(loc);
    };
    hook.current = attachEmbedUrlChange(iframe, report);
    report();
  };
  return (
    <>
      <PanelHead title="Bots" onHide={onHide} />
      <div className="ws-panel-body">
        <iframe
          // BEFORE src: the page reads its role at module init (router WS_ROLE).
          data-ws-role="panel"
          ref={ref}
          className="ws-panel-frame"
          title="Bots"
          src={src}
          onLoad={onLoad}
        />
      </div>
    </>
  );
}

/** Tell a frame's document whether it is hidden (WorkspaceHost's
 *  `signalVisibility`, for a panel frame): the flag, and the event when it
 *  changed. */
function signalHidden(iframe: HTMLIFrameElement | null, hidden: boolean): void {
  const win = iframe?.contentWindow;
  if (!win) return;
  try {
    if (win._fusedTabHidden === hidden) return;
    win._fusedTabHidden = hidden;
    win.dispatchEvent(new (win as Window & typeof globalThis).Event(TAB_VISIBILITY_EVENT));
  } catch {
    /* frame mid-navigation */
  }
}

/** THE GIT COMPANION of a folder — the page the app page's Git tab frames
 *  (useAppPagePaneTab `gitSrc`). A file's path asks its folder. */
export function GitView({ path, onHide }: { path: string; onHide: () => void }) {
  const own = useDirMode(path, "git");
  // A file (or a folder outside a repository whose parent is in one) asks
  // the parent once its own answer is no.
  const parentDir = dirname(path);
  const askParent = !own.pending && !own.entry && parentDir !== path;
  const parent = useDirMode(askParent ? parentDir : null, "git");
  const hit = own.entry ? { mode: own, dir: path } : askParent && parent.entry ? { mode: parent, dir: parentDir } : null;
  const pending = own.pending || (askParent && parent.pending);
  const src =
    hit && hit.mode.entry && hit.mode.entry.path !== null
      ? `/render?path=${encodeURIComponent(hit.mode.entry.path)}&_file=${encodeURIComponent(hit.dir)}`
      : null;
  const name = basename((hit?.dir ?? path).replace(/[/\\]+$/, "")) || hit?.dir || path;
  return (
    <>
      <PanelHead title={`Git · ${name}`} onHide={onHide} />
      <div className="ws-panel-body">
        {src ? (
          <iframe key={src} data-ws-role="panel" className="ws-panel-frame is-loaded" title={`Git · ${name}`} src={src} />
        ) : (
          <p className="ws-panel-note">{pending ? "Loading…" : "This folder is not in a Git repository."}</p>
        )}
      </div>
    </>
  );
}

/** A FILE IN ONE OF ITS MODES — the frame the rail's mode card draws
 *  (Preview's `fileModes.src`). */
export function ModeView({ mode, file, rev, onHide }: { mode: string; file: string; rev: string | null; onHide: () => void }) {
  // At the tab's revision when it shows one (Preview's `fileModes.src`).
  const src = embedSrc(file, "?" + new URLSearchParams({ _mode: mode, ...(rev ? { _snapshot: rev } : {}) }).toString());
  const title = `${modeTitle(mode)} · ${basename(file) || file}`;
  return (
    <>
      <PanelHead title={title} onHide={onHide} />
      <div className="ws-panel-body">
        <iframe key={src} data-ws-role="panel" className="ws-panel-frame is-loaded" title={title} src={src} />
      </div>
    </>
  );
}
