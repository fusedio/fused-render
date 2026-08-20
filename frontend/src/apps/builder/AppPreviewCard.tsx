// Big preview card for the /apps hub. Its thumbnail has three shapes, in
// precedence order:
//
//   1. `preview.png` at the app folder's root — an AUTHORED still, served as
//      bytes through /api/fs/raw. First because it is the only one the author
//      chose: a live render shows the page in whatever state it comes up in
//      (empty, mid-load, asking for a file), and a screenshot shows the app
//      making its point. It is also by far the cheapest of the three.
//      Hovering the card swaps the still for the live app (step 2's iframe),
//      and hover-end swaps it back — see the hover state below.
//   2. the app itself, live: `entry_html` in a sandboxed iframe at desktop
//      width (1280px) scaled down to fit the card.
//   3. no entry file at all — an empty thumb. The box keeps its 16/10 aspect,
//      background and top border, so the card holds its shape; it just says
//      nothing about an app that has nothing to show (D365).
//
// The precedence is a FALLBACK CHAIN, not a fixed choice, and it has to be:
// `preview_image` says a file of that name exists and is non-empty, not that it
// decodes. A corrupt or half-written PNG in an <img> renders as nothing, so a
// wrongly-confident step 1 would be a permanently blank card — strictly worse
// than the live render it replaced. An image error drops to step 2.
//
// Display-only either way: a pointer-events shield keeps every click on the
// card, which opens the app. The image is lazy via `loading="lazy"`; the
// iframe goes further — see useNearViewport below.
//
// `loading="lazy"` only defers the FIRST load, it never reclaims an iframe
// once scrolled past. A workspace with many entry_html apps and no
// preview.png would still end up with every card that has ever scrolled
// through the viewport pinned open — each a whole sandboxed page + JS
// runtime. useNearViewport instead mounts the iframe only while its card is
// near the viewport and unmounts it once scrolled well past, showing step 3's
// empty thumb in between — an offloaded card reads the same as an app with no
// live preview (D365).
import { useState } from "react";
import type { AppInfo } from "@platform/lib/api";
import { appfilePreviewUrl, rawUrl } from "@platform/lib/api";
import { exportAppFile } from "@platform/lib/appShot";
import { pushToast } from "@platform/lib/toast";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { withNoFocus } from "@platform/lib/frame-focus";
import { embedUrlForFsPath, withPreviewFlag } from "@platform/lib/router";
import { appRecency, hrefFor, onAppCardClick, openTargetFor } from "@platform/lib/appEntry";
import { useNearViewport, usePreviewStart } from "@platform/lib/preview-start";

import { timeAgo } from "@platform/lib/format";

// The iframe renders at a fixed desktop width and is scaled to the card by a
// pure-CSS trick: 400% width/height + scale(0.25) means the visual size is
// exactly the .app-pcard-thumb box, whatever the grid column resolves to.
const PREVIEW_SCALE = 0.25;

// The URL a thumbnail's iframe loads. Both stamps say the same thing in two
// registers — this frame is a PICTURE of the app, not a use of it:
//
//   • `_preview=1` — don't record an open (D301), or scrolling the grid would
//     reshuffle the recency order the grid is sorted by.
//   • `_nofocus=1` — don't take the keyboard (D348). Focusing an element inside
//     a frame also scrolls that frame into view, and the scroll propagates out
//     to the embedder's scroller: an app that focuses an input on boot yanked
//     .apps-page down to its own card the moment the card mounted, so scrolling
//     the grid jumped to whatever row that app sits in. The contract, and the
//     runtime half that enforces it, are in platform/lib/frame-focus.ts.
function thumbSrc(entryHtml: string): string {
  return withNoFocus(withPreviewFlag(`/render?path=${encodeURIComponent(entryHtml)}`));
}

export function AppPreviewCard({
  app,
  onContextMenu,
  badge,
}: {
  app: AppInfo;
  // Right-click: the card only forwards the event and its own app — the menu
  // state lives one level up (Apps.tsx), so the whole grid shares one portal.
  onContextMenu?: (e: React.MouseEvent, app: AppInfo) => void;
  // Extra word in the meta row (e.g. "cloned" on a showcase app the user has
  // copied into Fused/local). Decoration only — the card behaves the same.
  badge?: string;
}) {
  const title = app.title || app.name;
  // The same timestamp the grid SORTS by (last opened, modified standing in) —
  // a card ranked first for being opened just now must not label itself with a
  // stale modified time. appRecency's 0-for-neither is falsy, so timeAgo still
  // returns null and the label hides.
  const ago = timeAgo(appRecency(app));
  // Set when the authored thumbnail fails to decode — see the fallback chain in
  // the module comment. One-way: a retry would loop on a file that is broken.
  const [shotFailed, setShotFailed] = useState(false);
  // The still's source. An exported .fused card (kind "appfile", D396) has no
  // folder to hold a preview.png — its still is the payload's, streamed by a
  // single-member zip read; the endpoint 404s when the file ships without one
  // and this <img>'s ordinary onError drops the card to the live branch
  // (an opened file's fusedapp preview — see liveSrc) or the empty thumb.
  const shotSrc =
    app.kind === "appfile"
      ? appfilePreviewUrl(app.path)
      : app.preview_image
        ? rawUrl(app.preview_image)
        : null;
  // Gates the live-iframe branch only — preview.png costs nothing to keep
  // mounted and the empty thumb costs nothing at all, so neither needs this.
  const [thumbRef, nearViewport] = useNearViewport<HTMLSpanElement>();
  // Hover on a png-thumbed card swaps in the live app: the iframe mounts
  // UNDER the still image on mouseenter and the image only fades once the
  // iframe has loaded (`liveReady`), so the swap never shows a blank frame
  // mid-boot. Mouseleave unmounts the iframe and the png is back instantly.
  const [hovered, setHovered] = useState(false);
  const [liveReady, setLiveReady] = useState(false);
  // What the live branch renders. An ordinary app live-renders its entry
  // page. An exported .fused card (kind "appfile") has no page to point
  // /render at — its live look is its own fusedapp view under `_preview=1`,
  // which re-uses the existing extract (never extracts, never records — the
  // server's reuse_only preview contract, D396) — offered only for a file the
  // user has OPENED before (`opened_at`): one they never ran stays the empty
  // thumb rather than a placeholder-in-a-frame, and a peek must not be the
  // first run of a stranger's pages anyway.
  const liveSrc = app.entry_html
    ? thumbSrc(app.entry_html)
    : app.kind === "appfile" && app.opened_at != null
      ? withNoFocus(withPreviewFlag(embedUrlForFsPath(app.path)))
      : null;
  const wantsLive = Boolean(
    liveSrc && nearViewport && ((!shotSrc || shotFailed) || hovered),
  );
  // Priority is only the still's hover path. A card whose normal body is
  // already live must not tear down and restart its iframe merely because
  // the pointer crossed it.
  const livePriority = Boolean(shotSrc && !shotFailed && hovered);
  const { started: liveStarted, settled: liveSettled } = usePreviewStart(
    wantsLive,
    livePriority,
  );
  // An anchor, not a button — see AppCard. The href is what makes middle-click
  // and "Open in new tab" land on the same place a left click does.
  return (
    <a
      className="app-pcard"
      href={hrefFor(app)}
      onClick={(e) => onAppCardClick(e, app)}
      // On the <a>, not on the body: the thumbnail's pointer-events shield sits
      // INSIDE this element, so a right-click over the preview bubbles up here
      // (the iframe itself never sees it) and one handler covers the whole card.
      onContextMenu={onContextMenu && ((e) => onContextMenu(e, app))}
      // liveReady resets on ENTER as well as leave: a straggler onLoad from the
      // previous hover's iframe could have re-set it after leave cleared it, and
      // a stale true would blank the still before the new iframe has painted.
      onMouseEnter={() => {
        setHovered(true);
        setLiveReady(false);
      }}
      onMouseLeave={() => {
        setHovered(false);
        setLiveReady(false);
      }}
      title={openTargetFor(app).path}
    >
      <span className="app-pcard-body">
        <span className="app-pcard-title">{title}</span>
        <span className="app-pcard-meta">
          <span className="app-pcard-tag">{app.tag}</span>
          {title !== app.name && <span className="app-pcard-name">{app.name}</span>}
          {badge && <span className="app-pcard-name">{badge}</span>}
          {ago && <span className="app-pcard-ago">{ago}</span>}
        </span>
      </span>
      <span className="app-pcard-thumb" aria-hidden="true" ref={thumbRef}>
        {shotSrc && !shotFailed ? (
          <>
            {/* Hover live preview, mounted BELOW the img in the stacking
                order so the still stays on top until the app has painted. */}
            {hovered && liveSrc && nearViewport && liveStarted && (
              <iframe
                src={liveSrc}
                style={{
                  width: `${100 / PREVIEW_SCALE}%`,
                  height: `${100 / PREVIEW_SCALE}%`,
                  transform: `scale(${PREVIEW_SCALE})`,
                }}
                onLoad={() => {
                  liveSettled();
                  setLiveReady(true);
                }}
                onError={liveSettled}
                tabIndex={-1}
                scrolling="no"
                title=""
              />
            )}
            <img
              className="app-pcard-shot"
              src={shotSrc}
              alt=""
              loading="lazy"
              onError={() => setShotFailed(true)}
              // Transition inline with the opacity: hover-end removes the whole
              // style, so the still snaps back instantly instead of fading in
              // over the unmounted iframe's blank.
              style={
                hovered && liveReady ? { opacity: 0, transition: "opacity 0.15s ease" } : undefined
              }
            />
            {/* The same shield the iframe gets. An <img> swallows no clicks of
                its own, but it DOES carry the browser's native drag-the-image
                gesture, which starts a drag on the card instead of the click
                that opens it. */}
            <span className="app-pcard-shield" />
          </>
        ) : liveSrc && nearViewport && liveStarted ? (
          <>
            <iframe
              src={liveSrc}
              style={{
                width: `${100 / PREVIEW_SCALE}%`,
                height: `${100 / PREVIEW_SCALE}%`,
                transform: `scale(${PREVIEW_SCALE})`,
              }}
              tabIndex={-1}
              scrolling="no"
              title=""
              onLoad={liveSettled}
              onError={liveSettled}
            />
            {/* Shield: the preview is display-only — every pointer event lands
                on the card's link, never inside the app — which is also what
                keeps middle-click over the preview a new tab for the app. */}
            <span className="app-pcard-shield" />
          </>
        ) : null}
      </span>
      {/* Hover-revealed export (SPEC §43 AF-4, D391): the same action as the
          right-click menu's "Export App File", surfaced so it is one visible
          click. A SIBLING of the thumb, not a child: the thumb span is
          aria-hidden (it is decoration), and a focusable button inside an
          aria-hidden subtree is announced as nothing by assistive tech while
          still taking tab focus. Positioned over the thumb via the card's own
          positioning context. A <button> inside the card's <a>: it must both
          preventDefault (or the card link opens the app) and stopPropagation
          (or the click ALSO bubbles to onAppCardClick). Not rendered on an
          exported .fused card (kind "appfile", D396): its path is the file
          itself and the export route only takes app folders. */}
      {app.kind !== "appfile" && (
      <button
        type="button"
        className="app-pcard-export"
        title={"Export " + (app.title || app.name) + " as a .fused app file"}
        aria-label="Export app file"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          // Also captures a tab screenshot into the file's preview.png when
          // the folder has no authored one (appShot, D396). The thumb element
          // rides along as the crop source: a card without a preview.png is
          // already showing the live app there, so nothing has to flash.
          exportAppFile(app, thumbRef.current).catch((err: Error) =>
            pushToast({
              msg: "Could not export " + app.name + ": " + err.message,
              tone: "error",
            }),
          );
        }}
      >
        {MenuIcons.download}
      </button>
      )}
    </a>
  );
}
