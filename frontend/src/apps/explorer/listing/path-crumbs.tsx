// Decision 1: the breadcrumbs shown inside the merged search field
// (Listing.tsx) while it is empty — one field carrying either a path or a
// pattern, magnifier at the left edge, breadcrumbs behind it when there is
// nothing typed. Same segments as Breadcrumb.tsx's own path strip, built the
// same way, but click-to-navigate only: no spring-loaded drag targets, no
// GNOME-style shrink-on-overflow. Those stay with the plain-file bar
// (deferred, see DECISIONS-one-field-search.md) — decision 1 is the merge
// itself, not carrying every crumb-strip behavior into the new home.
import type { ReactNode } from "react";
import { navigate } from "@platform/lib/router";

export function PathCrumbs({
  fsPath,
  home,
}: {
  fsPath: string;
  home?: string;
}) {
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

  return <div className="crumbs listing-search-crumbs">{pieces}</div>;
}
