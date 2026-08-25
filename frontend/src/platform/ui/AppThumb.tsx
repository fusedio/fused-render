// The thumbnail of an app card — the mechanics, not the card. Extracted from
// AppPreviewCard so two card designs (Home's `.app-pcard`, the /apps hub's
// shadcn card) share one implementation of the fallback chain, the lazy
// iframe scheduling and the hover crossfade. The thumb has three shapes, in
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
//      width (1280px) scaled down to fit the box.
//   3. no entry file at all — an empty box. It keeps its aspect and
//      background so the card holds its shape; it just says nothing about an
//      app that has nothing to show (D365).
//
// The precedence is a FALLBACK CHAIN, not a fixed choice, and it has to be:
// `preview_image` says a file of that name exists and is non-empty, not that
// it decodes. A corrupt or half-written PNG in an <img> renders as nothing, so
// a wrongly-confident step 1 would be a permanently blank card — strictly
// worse than the live render it replaced. An image error drops to step 2.
//
// Display-only either way: a pointer-events shield keeps every click on the
// card, which opens the app. The image is lazy via `loading="lazy"`; the
// iframe goes further — `loading="lazy"` only defers the FIRST load, it never
// reclaims an iframe once scrolled past, so a workspace with many entry_html
// apps and no preview.png would end up with every card that has ever scrolled
// through the viewport pinned open, each a whole sandboxed page + JS runtime.
// useNearViewport instead mounts the iframe only while its card is within
// ~300px of the viewport and unmounts it once it has scrolled well past, and
// usePreviewStart admits at most two live previews at a time (preview-start).
//
// The class names here (`.app-pcard-thumb`, `-shot`, `-skel`, `-shield`,
// apps.css) are the thumb's own and stay the same under both card designs:
// Home's skeleton row and preferences.css compound on `.app-pcard-thumb`, and
// the /apps export capture finds a painted thumb by
// `.app-pcard-thumb[data-capture-ready]` (Apps.tsx openCardMenu). The card
// passes `className` for the box's shape (radius, border) only.
import { useEffect, useRef, useState } from "react";
import type { AppInfo } from "@platform/lib/api";
import { appfilePreviewUrl, rawUrl } from "@platform/lib/api";
import { withNoFocus } from "@platform/lib/frame-focus";
import { embedUrlForFsPath, withPreviewFlag } from "@platform/lib/router";
import { useNearViewport, usePreviewStart } from "@platform/lib/preview-start";

const PREVIEW_SCALE = 0.25;

// The /render URL for a thumb: the app at desktop width, flagged as a preview
// so the page can skip anything heavy, and with focus-stealing disabled.
function thumbSrc(entryHtml: string): string {
  return withNoFocus(withPreviewFlag(`/render?path=${encodeURIComponent(entryHtml)}`));
}

export function AppThumb({
  app,
  hovered,
  className,
  onBodyLive,
}: {
  app: AppInfo;
  // The CARD's hover state, owned by the card because the whole card is the
  // hover target (the export chip, the border) — the thumb only reads it to
  // decide when to swap a still for the live app.
  hovered: boolean;
  className?: string;
  // Fires once, when the body iframe has loaded — i.e. the thumb is now a
  // picture of the APP and not an empty box — with the thumb element, which is
  // what the export capture crops (appShot.exportAppFile). Never re-fires: the
  // body iframe is never torn down and re-created for the same card.
  onBodyLive?: (el: HTMLSpanElement | null) => void;
}) {
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
  // means the shot just finished loading and gets the transition; from the
  // first hover on, reaching it again means a live preview just unmounted
  // underneath, and it has to snap back with none — a fade there would show
  // the still fading in over the iframe's blank box.
  const everHoveredRef = useRef(false);
  if (hovered) everHoveredRef.current = true;
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
  // (a safety net — cards are keyed by `app.path`).
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
  // Reset on every enter AND leave.
  const [liveReady, setLiveReady] = useState(false);
  useEffect(() => {
    setLiveReady(false);
  }, [hovered]);
  // The body iframe has loaded — the thumb is a picture of the app. Separate
  // from `liveReady`, which is the hover crossfade's and is cleared by the very
  // hover that reaches the export chip. One-way for the life of the mount; it
  // CAN stay true after the iframe unmounts by scrolling far out of
  // `nearViewport`, and that is harmless: appShot's cropRect refuses an
  // off-viewport element anyway.
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
    ? thumbSrc(app.entry_html)
    : app.kind === "appfile" && app.opened_at != null
      ? withNoFocus(withPreviewFlag(embedUrlForFsPath(app.path)))
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
  // (preview-start's Priority). A getter rather than a dependency because
  // usePreviewStart's effect restarts the iframe whenever its deps change:
  // promoting a waiting card through the deps would tear down a running one.
  const { started: liveStarted, settled: liveSettled } = usePreviewStart(
    wantsLive,
    hoverPriority || onScreen,
  );
  // Whether the CURRENTLY MOUNTED body iframe has painted — separate from
  // `bodyLive` above on purpose: a card that once painted, then scrolled out
  // of view and back in, must not show its brand-new, not-yet-loaded iframe at
  // FULL opacity. Resets whenever the iframe itself is torn down and remounted.
  const [bodyPainted, setBodyPainted] = useState(false);
  useEffect(() => {
    setBodyPainted(false);
  }, [liveStarted, liveSrc]);

  const bodyLoaded = () => {
    liveSettled();
    setBodyPainted(true);
    if (!bodyLive) {
      setBodyLive(true);
      onBodyLive?.(thumbRef.current);
    }
  };

  return (
    // `data-capture-ready` marks the thumb as a picture of the APP — the
    // export capture's crop-source contract (appShot.exportAppFile). Absent,
    // not "0", so the selector is a plain presence test.
    <span
      className={"app-pcard-thumb" + (className ? " " + className : "")}
      aria-hidden="true"
      ref={thumbRef}
      data-capture-ready={bodyLive ? "" : undefined}
    >
      {/* Shimmer while something is actually COMING: an authored still not
          yet decoded, or a live iframe the card wants but has not painted.
          Never for the "nothing to show" case (D365) — that's `liveSrc ==
          null`, which keeps `wantsLive` false and this condition with it.
          The "has it painted yet" signal depends on WHICH live branch is live:
          a still-thumbed card's hover preview sets `liveReady` and never
          touches `bodyPainted`. */}
      {((shotSrc && !shotFailed && !shotLoaded) ||
        (wantsLive &&
          (!liveStarted || !(shotSrc && !shotFailed ? liveReady : bodyPainted)))) && (
        <span className="app-pcard-skel" />
      )}
      {shotSrc && !shotFailed ? (
        <>
          {/* Hover live preview, mounted BELOW the img in the stacking order so
              the still stays on top until the app has painted. No
              `loading="lazy"`: mounting is already gated, and the UA's lazy
              heuristics read the layout box — 400% wide and `scale(0.25)`-ed —
              and can defer past the point a `load` event ever fires, which
              would leave `liveReady` stuck and a scheduler slot held. */}
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
              onError={() => {
                liveSettled();
                setLiveReady(true);
              }}
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
            onLoad={() => setShotLoaded(true)}
            onError={() => setShotFailed(true)}
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
          {/* The same shield the iframe gets: an <img> carries the browser's
              native drag-the-image gesture, which would start a drag instead
              of the click that opens the card. */}
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
              opacity: bodyPainted ? 1 : 0,
              transition: "opacity 0.15s ease",
            }}
            tabIndex={-1}
            scrolling="no"
            title=""
            onLoad={bodyLoaded}
            onError={bodyLoaded}
          />
          {/* Shield: the preview is display-only — every pointer event lands
              on the card's link, never inside the app — which is also what
              keeps middle-click over the preview a new tab for the app. */}
          <span className="app-pcard-shield" />
        </>
      ) : null}
    </span>
  );
}
