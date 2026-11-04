// A connected service's mark (GET /api/connect/<key>/icon). The packs' SVGs
// draw with `currentColor` (lucide-style line marks), which an <img> renders
// black whatever the theme; so the file is used as a CSS mask over the
// current text colour instead, and the mark follows light and dark like an
// inline icon would. A pack without an icon gets its initial.
import { connectPath, type ConnectProvider } from "@platform/lib/connect";

export function ConnectIcon({ provider: p, className = "size-5" }: { provider: Pick<ConnectProvider, "key" | "name" | "icon">; className?: string }) {
  if (p.icon) {
    const url = `url("${connectPath(p.key, "/icon")}")`;
    return (
      <span
        aria-hidden="true"
        className={`inline-block shrink-0 bg-current ${className}`}
        style={{ maskImage: url, WebkitMaskImage: url, maskSize: "contain", WebkitMaskSize: "contain", maskRepeat: "no-repeat", WebkitMaskRepeat: "no-repeat", maskPosition: "center", WebkitMaskPosition: "center" }}
      />
    );
  }
  return (
    <span aria-hidden="true" className={`grid shrink-0 place-items-center rounded bg-muted text-[11px] font-semibold uppercase ${className}`}>
      {(p.name || p.key).slice(0, 1)}
    </span>
  );
}
