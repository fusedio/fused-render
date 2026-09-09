// Decision 1: the breadcrumbs shown inside the merged search field
// (Listing.tsx) while it is empty — one field carrying either a path or a
// pattern, magnifier at the left edge, breadcrumbs behind it when there is
// nothing typed. Same segments as Breadcrumb.tsx's own path strip, built the
// same way, but click-to-navigate only: no spring-loaded drag targets, no
// GNOME-style shrink-on-overflow. Those stay with the plain-file bar
// (deferred, see DECISIONS-one-field-search.md) — decision 1 is the merge
// itself, not carrying every crumb-strip behavior into the new home.
import { useLayoutEffect, useRef, type ReactNode } from "react";
import { navigate } from "@platform/lib/router";

export function PathCrumbs({
  fsPath,
  home,
}: {
  fsPath: string;
  home?: string;
}) {
  // Left-aligned at rest, right after the magnifier — a detached path was
  // the exact "icon over here, path over there" arrangement rejected for
  // this bar. A narrow field still has to keep the CURRENT folder readable
  // rather than the root, so overflow is handled by scrolling the strip to
  // its own end (the same tail-pin `#breadcrumb .crumbs` uses) instead of by
  // packing the content against the right edge, which pulled the whole
  // strip away from the glyph even when it was not overflowing at all. Runs
  // after every render (fsPath, home, or just the field's own width can
  // change what overflows) and on resize, since nothing else here observes
  // the field's width.
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const pin = () => {
      el.scrollLeft = el.scrollWidth;
    };
    pin();
    const ro = new ResizeObserver(pin);
    ro.observe(el);
    return () => ro.disconnect();
  });

  const underHome = home !== undefined && fsPath.startsWith(home + "/");
  const rest = underHome ? fsPath.slice((home as string).length) : fsPath;
  const parts = rest.split("/").filter((s) => s.length > 0);
  const rootTarget = underHome ? (home as string) : "/";

  const pieces: ReactNode[] = [
    <a
      key="root"
      href="#"
      className={"path-crumb" + (parts.length === 0 ? " last" : "")}
      onClick={(e) => {
        e.preventDefault();
        navigate(rootTarget, { isDir: true });
      }}
    >
      {underHome ? "~" : "/"}
    </a>,
  ];
  // A Windows path's first segment is the drive ("C:"); its crumb targets
  // "C:/" rather than re-rooting at "/" (Breadcrumb.tsx's own crumbs).
  const isDrive = !underHome && /^[A-Za-z]:$/.test(parts[0] || "");
  let acc = underHome ? (home as string) : "";
  parts.forEach((part, i) => {
    if (i === 0 && isDrive) acc = part + "/";
    else acc = acc + (acc.endsWith("/") ? "" : "/") + part;
    const target = acc;
    const isLast = i === parts.length - 1;
    if (i > 0 || underHome) {
      pieces.push(
        <span key={"sep" + i} className="path-crumb-sep">
          /
        </span>,
      );
    }
    if (isLast) {
      pieces.push(
        <span key={target} className="path-crumb last" title={part}>
          {part}
        </span>,
      );
    } else {
      pieces.push(
        <a
          key={target}
          href="#"
          className="path-crumb"
          title={part}
          onClick={(e) => {
            e.preventDefault();
            navigate(target, { isDir: true });
          }}
        >
          {part}
        </a>,
      );
    }
  });

  return (
    <div className="crumbs listing-search-crumbs" ref={ref}>
      {pieces}
    </div>
  );
}
