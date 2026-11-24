// The crumbs for a remote (`s3://`) location — one copy for both places a
// path strip is drawn: the crumb bar's own strip (Breadcrumb.tsx, over an S3
// file) and the merged search field's resting crumbs (path-crumbs.tsx, over
// an S3 folder, whose Listing claims the bar exactly as a local folder's does).
import type { ReactNode } from "react";
import { spaLinkProps } from "@platform/lib/router";
import { parseRemoteLocation } from "@platform/lib/remote";

// The crumbs for a remote (`s3://`) location: `s3://bucket` › segment › …,
// each ancestor a link to its own prefix. Plain links — no spring-load, no
// drop target and no crumb-path attribute: an S3 prefix is not a folder a
// file can be moved into, and the bar's ancestor right-click (Reveal in
// Finder) has nothing local to reveal.
export function remoteCrumbs(loc: string): ReactNode[] {
  const p = parseRemoteLocation(loc);
  if (!p) return [];
  const segs = p.key.split("/").filter((s) => s.length > 0);
  const crumb = (key: string, label: string, target: string, isLast: boolean) => {
    const shrink = label.length > (isLast ? 28 : 7) * 1.5;
    const cls = "path-crumb" + (isLast ? " last" : "") + (shrink ? " shrink" : "");
    if (isLast) {
      return (
        <span key={key} className={cls} title={target}>
          {label}
        </span>
      );
    }
    const link = spaLinkProps(target, { isDir: true });
    return (
      <a key={key} href={link.href} className={cls} title={target} onClick={link.onClick}>
        {label}
      </a>
    );
  };
  const pieces: ReactNode[] = [
    crumb("root", "s3://" + p.bucket, `s3://${p.bucket}/`, segs.length === 0),
  ];
  let acc = `s3://${p.bucket}/`;
  segs.forEach((seg, i) => {
    acc += seg + "/";
    pieces.push(
      <span key={"sep" + i} className="path-crumb-sep">
        /
      </span>,
    );
    // The last crumb names the location itself (file or prefix); a link is
    // never needed for it, so its trailing "/" does not matter.
    pieces.push(crumb(acc, seg, acc, i === segs.length - 1));
  });
  return pieces;
}
