// The generic app mark: the brand's four-point star (the app icon's own
// sparkle), drawn on `currentColor` so each host tints it with its own tokens.
// What an app WITHOUT an `icon.svg` shows — the sidebar's Projects row, the
// /apps and /home cards, the app page's header. Three hosts, one path, since
// the third copy of a 200-character bezier is where it stops being a coincidence.
//
// Sizing and colour stay with the host (its own class): the row's is 12px and
// follows the row state (muted at rest, accent when active), the card's and
// the page's are a whole slot beside the name.
export function AppStar(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 64 64" fill="currentColor" aria-hidden="true" {...props}>
      <path d="M32 2 C36.5 20.5 43.5 27.5 62 32 C43.5 36.5 36.5 43.5 32 62 C27.5 43.5 20.5 36.5 2 32 C20.5 27.5 27.5 20.5 32 2 Z" />
    </svg>
  );
}
