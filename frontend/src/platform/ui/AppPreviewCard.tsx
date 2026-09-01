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
import { useEffect, useRef, useState } from "react";
import type { AppInfo } from "@platform/lib/api";
import { appfilePreviewUrl, rawUrl } from "@platform/lib/api";
import { exportAppFile } from "@platform/lib/appShot";
import { pushToast } from "@platform/lib/toast";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { Button } from "@platform/shadcn/ui/button";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { thumbFrame } from "@platform/lib/thumb-frame";
import { embedUrlForFsPath, navigateUrl } from "@platform/lib/router";
import {
  appRecency,
  hrefFor,
  isBrowserHandledClick,
  onAppCardClick,
  openTargetFor,
} from "@platform/lib/appEntry";
import { useNearViewport, usePreviewStart } from "@platform/lib/preview-start";

import { timeAgo } from "@platform/lib/format";

// The iframe renders at a fixed desktop width and is scaled to the card by a
// pure-CSS trick: 400% width/height + scale(0.25) means the visual size is
// exactly the .app-pcard-thumb box, whatever the grid column resolves to.
const PREVIEW_SCALE = 0.25;

// The page a card's live thumbnail shows. Plain: everything that makes it a
// PICTURE rather than a use of the app — the two URL stamps, the sandbox seal,
// the markup — arrives at the iframe from `thumbFrame` below, which is the one
// place that description lives (platform/lib/thumb-frame.ts).
function entryRenderUrl(entryHtml: string): string {
  return `/render?path=${encodeURIComponent(entryHtml)}`;
}

export function AppPreviewCard({
  app,
  onContextMenu,
  badge,
  href,
}: {
  app: AppInfo;
  // Right-click: the card only forwards the event and its own app — the menu
  // state lives one level up (Apps.tsx), so the whole grid shares one portal.
  onContextMenu?: (e: React.MouseEvent, app: AppInfo) => void;
  // Extra word in the meta row (e.g. "cloned" on a showcase app the user has
  // copied into Fused/local). Decoration only — the card behaves the same.
  badge?: string;
  // Overrides where the card LANDS — an entry-page URL that carries a query
  // string (the Playground's model handoff, D442). hrefFor deliberately
  // carries none, so a caller with params to hand over supplies the whole
  // URL; left-click then goes through navigateUrl instead of openApp, and
  // the browser gestures use the same href, so the two still can't disagree.
  href?: string;
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
  // Set from the still <img>'s onLoad — gates the shimmer/fade below. Separate
  // from `shotFailed`: a still can be slow to decode without ever failing, and
  // that gap is exactly what used to paint the box background underneath it.
  const [shotLoaded, setShotLoaded] = useState(false);
  // Whether the pointer has ever entered the card. The still's ENTRANCE fade
  // (first decode, below) and the hover crossfade's INSTANT snap-back
  // (`.app-pcard-shot` in apps.css) land on the same end state — opaque, not
  // hovered — so a style computed purely from (hovered, liveReady, shotLoaded)
  // cannot tell the two apart; a CSS transition only looks at the style being
  // entered, not how it got there. Before the first hover, reaching that state
  // means the shot just finished loading and gets the transition (the fade
  // this feature adds); from the first hover on, reaching it again means a
  // live preview just unmounted underneath, and it has to snap back with none
  // — a fade there would show the still fading in over the iframe's blank box,
  // which is the exact regression the original (pre-shimmer) code avoided.
  const everHoveredRef = useRef(false);
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
  // Reset if the still's own URL ever changes under an already-mounted card
  // (narrow edge case — cards are keyed by `app.path`, so this is a safety net
  // rather than a path this component normally takes; unlike the live
  // iframe below, this `<img>` is never conditionally unmounted by scrolling).
  useEffect(() => {
    setShotLoaded(false);
  }, [shotSrc]);
  // Gates the live-iframe branch only — preview.png costs nothing to keep
  // mounted and the empty thumb costs nothing at all, so neither needs this.
  const [thumbRef, nearViewport, onScreen] = useNearViewport<HTMLSpanElement>();
  // Hover on a png-thumbed card swaps in the live app: the iframe mounts
  // UNDER the still image on mouseenter and the image only fades once the
  // iframe has loaded (`liveReady`), so the swap never shows a blank frame
  // mid-boot. Mouseleave unmounts the iframe and the png is back instantly.
  const [hovered, setHovered] = useState(false);
  const [liveReady, setLiveReady] = useState(false);
  // The card BODY's live iframe has loaded — i.e. the thumb is a picture of the
  // app and not an empty box. Separate state from `liveReady`, which is the
  // hover crossfade's and is deliberately reset on every enter AND leave: the
  // export chip is only reachable while hovering, so gating a capture on
  // `liveReady` would gate it on a flag the hover just cleared. One-way for the
  // life of the mount, which is exact — the body iframe is never torn down and
  // re-created for the same card, and cards are keyed by path, so a different
  // app is a different mount. It CAN stay true after the iframe unmounts by
  // scrolling far out of `nearViewport`, and that is harmless: appShot's
  // cropRect refuses an off-viewport element anyway.
  const [bodyLive, setBodyLive] = useState(false);
  // What the live branch renders. An ordinary app live-renders its entry
  // page. An exported .fused card (kind "appfile") has no page to point
  // /render at — its live look is its own fusedapp view under `_preview=1`,
  // which re-uses the existing extract (never extracts, never records — the
  // server's reuse_only preview contract, D396) — offered only for a file the
  // user has OPENED before (`opened_at`): one they never ran stays the empty
  // thumb rather than a placeholder-in-a-frame, and a peek must not be the
  // first run of a stranger's pages anyway.
  const liveSrc = app.entry_html
    ? entryRenderUrl(app.entry_html)
    : app.kind === "appfile" && app.opened_at != null
      ? embedUrlForFsPath(app.path)
      : null;
  const wantsLive = Boolean(
    liveSrc && nearViewport && ((!shotSrc || shotFailed) || hovered),
  );
  // The still's hover path keeps the queue's `true` fast lane: a gesture skips
  // the idle wait and jumps the queue. It only ever flips together with
  // `wantsLive` above, so the extra effect run it causes is the one that
  // mounts the hover iframe — no started preview is torn down for it.
  const hoverPriority = Boolean(shotSrc && !shotFailed && hovered);
  // Every other card ranks by whether it is ON SCREEN — useNearViewport's
  // third slot, a STABLE getter the queue reads at admission time
  // (preview-start's Priority). The 300px lookahead still means a scroll
  // queues a row or so the reader cannot quite see yet, and with two slots the
  // cards they ARE looking at used to wait behind those in request order. A getter
  // rather than a dependency because usePreviewStart's effect restarts the
  // iframe whenever its deps change: promoting a waiting card through the deps
  // would tear down a running one.
  const { started: liveStarted, settled: liveSettled } = usePreviewStart(
    wantsLive,
    hoverPriority || onScreen,
  );
  // Whether the CURRENTLY MOUNTED body iframe has painted — separate from
  // `bodyLive` above on purpose. `bodyLive` is deliberately one-way for the
  // export capture's sake (see its comment); reusing it here would mean a
  // card that once painted, then scrolled out of view and back in, shows its
  // brand-new, not-yet-loaded iframe at FULL opacity — a blank/booting frame
  // presented as finished, the same bug `loaded` in BookmarkCards.tsx's
  // LivePreview has this same fix for. `bodyPainted` resets whenever the
  // iframe itself is torn down and remounted (`liveStarted` or `liveSrc`
  // changing) and drives the fade/shimmer instead; `bodyLive` keeps its
  // existing one-way contract untouched.
  const [bodyPainted, setBodyPainted] = useState(false);
  useEffect(() => {
    setBodyPainted(false);
  }, [liveStarted, liveSrc]);
  // An anchor, not a button — see AppCard. The href is what makes middle-click
  // and "Open in new tab" land on the same place a left click does.
  return (
    <a
      className="app-pcard"
      href={href ?? hrefFor(app)}
      onClick={(e) => {
        if (!href) return onAppCardClick(e, app);
        if (e.defaultPrevented || isBrowserHandledClick(e)) return;
        e.preventDefault();
        navigateUrl(href, { isDir: false });
      }}
      // On the <a>, not on the body: the thumbnail's pointer-events shield sits
      // INSIDE this element, so a right-click over the preview bubbles up here
      // (the iframe itself never sees it) and one handler covers the whole card.
      onContextMenu={onContextMenu && ((e) => onContextMenu(e, app))}
      // liveReady resets on ENTER as well as leave: a straggler onLoad from the
      // previous hover's iframe could have re-set it after leave cleared it, and
      // a stale true would blank the still before the new iframe has painted.
      onMouseEnter={() => {
        everHoveredRef.current = true;
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
      {/* `data-capture-ready` marks the thumb as a picture of the APP — the
          export capture's crop-source contract (appShot.exportAppFile). The
          card's own chip below reads `bodyLive` directly; the context menu is
          opened by Apps.tsx, which has no access to this component's state and
          finds the element by this attribute instead. Same posture as the
          preview pane's `data-fused-annotate-target`: one attribute naming the
          element that is showing what the reader is looking at. Absent, not
          "0", so the selector is a plain presence test. */}
      <span
        className="app-pcard-thumb"
        aria-hidden="true"
        ref={thumbRef}
        data-capture-ready={bodyLive ? "" : undefined}
      >
        {/* Shimmer while something is actually COMING: an authored still not
            yet decoded, or a live iframe the card wants but has not painted.
            Never for the "nothing to show" case (D365, the module comment) —
            that's `liveSrc == null`, which keeps `wantsLive` false and this
            condition with it, so a card with no entry file stays the plain
            empty box it always was rather than shimmering forever.
            The second clause's "has it painted yet" signal depends on WHICH
            live branch is live: a still-thumbed card's hover preview sets
            `liveReady` (below) and never touches `bodyPainted` — that branch
            doesn't render at all when there's a still — so testing
            `bodyPainted` here would stay permanently true-less and shimmer
            for the entire duration of every hover on a still-thumbed card. */}
        {((shotSrc && !shotFailed && !shotLoaded) ||
          (wantsLive &&
            (!liveStarted || !(shotSrc && !shotFailed ? liveReady : bodyPainted)))) && (
          <Skeleton className="pointer-events-none absolute inset-0 rounded-none" />
        )}
        {shotSrc && !shotFailed ? (
          <>
            {/* Hover live preview, mounted BELOW the img in the stacking
                order so the still stays on top until the app has painted.
                No `loading="lazy"`: mounting is already gated by
                `useNearViewport`/`liveStarted`, so lazy adds no savings, and
                the UA's lazy heuristics read the layout box — which here is
                400% wide and `scale(0.25)`-ed — and can defer past the point
                a `load` event ever fires, which would leave `liveReady` (and
                the shimmer above) stuck forever and a scheduler slot held
                until the 10s timeout. */}
            {hovered && liveSrc && nearViewport && liveStarted && (
              <iframe
                {...thumbFrame(liveSrc)}
                style={{
                  width: `${100 / PREVIEW_SCALE}%`,
                  height: `${100 / PREVIEW_SCALE}%`,
                  transform: `scale(${PREVIEW_SCALE})`,
                }}
                onLoad={() => {
                  liveSettled();
                  setLiveReady(true);
                }}
                // An error is still a painted result (the frame shows the
                // app's own error page) — `onError={liveSettled}` alone freed
                // the scheduler slot but left `liveReady` false forever, so
                // the still never faded out and the shimmer clause above
                // never cleared either.
                onError={() => {
                  liveSettled();
                  setLiveReady(true);
                }}
              />
            )}
            <img
              className="app-pcard-shot"
              src={shotSrc}
              alt=""
              loading="lazy"
              onLoad={() => setShotLoaded(true)}
              onError={() => setShotFailed(true)}
              // Transition inline with the opacity: hover-end removes the whole
              // style, so the still snaps back instantly instead of fading in
              // over the unmounted iframe's blank. The `!shotLoaded` branch is
              // the one new case (the entrance fade over the skeleton above);
              // `everHoveredRef` is why it can share this ternary with the
              // hover crossfade without the two fighting over the same
              // opacity:1-not-hovered end state — see its declaration above.
              style={
                hovered && liveReady
                  ? { opacity: 0, transition: "opacity 0.15s ease" }
                  : !shotLoaded
                    ? { opacity: 0, transition: "opacity 0.15s ease" }
                    : everHoveredRef.current
                      ? undefined
                      : { opacity: 1, transition: "opacity 0.15s ease" }
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
            {/* No `loading="lazy"` — see the comment on the hover iframe
                above; the same failure mode applies here to `bodyPainted`. */}
            <iframe
              {...thumbFrame(liveSrc)}
              style={{
                width: `${100 / PREVIEW_SCALE}%`,
                height: `${100 / PREVIEW_SCALE}%`,
                transform: `scale(${PREVIEW_SCALE})`,
                // Fades in over the skeleton above rather than popping in
                // mid-boot. Gated on `bodyPainted`, NOT `bodyLive`: `bodyLive`
                // is one-way for the export capture's sake (see its
                // declaration) and stays true across a scroll-away/back
                // remount, which would otherwise show the freshly-mounted,
                // not-yet-loaded iframe at full opacity.
                opacity: bodyPainted ? 1 : 0,
                transition: "opacity 0.15s ease",
              }}
              // `bodyLive` as well as the queue's release: settling frees the
              // NEXT card's start slot, which says nothing about whether this
              // frame painted, and the export capture needs the latter.
              onLoad={() => {
                liveSettled();
                setBodyLive(true);
                setBodyPainted(true);
              }}
              // An error is still a painted result (the frame shows the app's
              // own error page) — `onError={liveSettled}` alone freed the
              // scheduler slot but left `bodyLive`/`bodyPainted` false
              // forever, so the shimmer never cleared, the frame never faded
              // in, and `data-capture-ready` was never set (silently breaking
              // export-from-card for an app whose live render errors).
              onError={() => {
                liveSettled();
                setBodyLive(true);
                setBodyPainted(true);
              }}
            />
            {/* No shield span here. `.app-pcard-thumb iframe` is already
                `pointer-events: none` (apps.css), which retargets every press —
                middle-click included — onto the card's own <a>. The `<img>`
                branch above keeps its shield for a different reason: an image
                carries the browser's native drag gesture, which pointer-events
                does not stop. */}
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
      {/* `.app-pcard-export` STAYS alongside the shadcn Button: apps.css owns
          the hover-reveal (opacity on `.app-pcard:hover`), the absolute
          placement over the thumb, and the `body[data-capture-shooting]` hide
          rule that keeps the chip out of every exported preview.png. */}
      {app.kind !== "appfile" && (
      <Button
        variant="outline"
        size="icon-sm"
        className="app-pcard-export"
        title={"Export " + (app.title || app.name) + " as a .fused app file"}
        aria-label="Export app file"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          // Also bakes a native screen shot in as the file's preview.png when
          // the folder has no authored one (appShot, D396). The thumb element
          // rides along as the crop source: a card without a preview.png is
          // already showing the live app there, so nothing has to flash —
          // but ONLY once that frame has loaded (`bodyLive`). Two card
          // previews start at a time, so an unstarted card's thumb is an
          // empty box, and cropping it would bake the empty box in as the
          // artifact's permanent thumbnail. Offer nothing instead and
          // appShot stages the app full-screen for the shot.
          exportAppFile(app, bodyLive ? thumbRef.current : null).catch((err: Error) =>
            pushToast({
              msg: "Could not export " + app.name + ": " + err.message,
              tone: "error",
            }),
          );
        }}
      >
        {MenuIcons.download}
      </Button>
      )}
    </a>
  );
}
