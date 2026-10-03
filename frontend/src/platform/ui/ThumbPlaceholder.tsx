// The picture a card thumbnail shows when it may not show the app: the brand
// mark, muted, centred in whatever box the caller gives it — the same recipe
// BookmarkCards' empty-folder note uses. Rendered by AppPreviewCard and
// BookmarkCards' LivePreview when live previews are turned off
// (live-previews-flag.ts) and there is no authored still to show instead.
//
// Both theme renders are in the DOM; CSS (`.thumb-placeholder-mark-*` in
// apps.css) shows the one matching the <html> data-theme stamp, exactly as
// `.fhb-empty-mark` does. Static on purpose: motion here would read as a
// loading state, and this is the one thumbnail that is never loading.
// aria-hidden: pure decoration inside a card whose name is the accessible
// text. `draggable={false}` for the reason the still's shield exists — an
// <img> carries the browser's native drag-the-image gesture.
import logoMarkDark from "@assets/logo-black-bg-transparent.png";
import logoMarkLight from "@assets/logo-white-bg-transparent.png";

export function ThumbPlaceholder() {
  return (
    <span className="thumb-placeholder" aria-hidden="true">
      <img
        className="thumb-placeholder-mark thumb-placeholder-mark-dark"
        src={logoMarkDark}
        alt=""
        draggable={false}
      />
      <img
        className="thumb-placeholder-mark thumb-placeholder-mark-light"
        src={logoMarkLight}
        alt=""
        draggable={false}
      />
    </span>
  );
}
