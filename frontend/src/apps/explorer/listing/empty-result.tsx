// What the listing's search shows for a settled, empty answer.
//
// Pure and separate from Listing.tsx for the same reason index-caveat.ts is:
// which message renders is a claim about WHY nothing is showing, and getting
// it wrong tells the user their files have no matches when the real story is
// that the index cannot see this folder at all — a mount, a package, the
// ignore list, indexing turned off, no Full Disk Access, or a folder that
// hasn't been scanned (yet, or ever). `indexGap` (lib/home-search) is the
// same classifier the home page's search box already renders these reasons
// through; this reuses its copy rather than inventing new wording for the
// same states.
import { indexGap } from "@apps/explorer/lib/home-search";
import { IndexFdaCta } from "@apps/explorer/IndexFdaCta";
import type { RankReason } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";

export function EmptyResultMessage({
  reason,
  scanning,
  ourScanRunning,
  filesScanned,
}: {
  /** The server's reason for the last settled answer, "" when it simply
   * found nothing — the same field `useListingSearch` hands back unchanged. */
  reason: RankReason;
  /** The LIVE index-status poll's own `scanning` flag, tri-state so a
   * definite `false` can contradict a `reason` frozen at rank time (see
   * `indexGap`'s own doc comment for why this matters in both directions).
   * Feeds `indexGap` for the UNCOVERED case only — see `ourScanRunning`
   * below for why the covered-but-empty case can't reuse this. */
  scanning: boolean | null;
  /** Whether THIS box's own covered-but-empty scan trigger
   * (`useListingSearch`, SPEC-empty-search-scan.md) has confirmed — via
   * `requestFolderScan`'s own `started` reply — that a scan of this exact
   * root is running right now. Deliberately NOT the same signal as
   * `scanning` above: that poll is machine-wide (true for ANY scan of ANY
   * root), and code review finding 2 caught that using it here made an
   * unrelated scan elsewhere claim a build was in progress for a root
   * nothing is scanning. `started` is the one signal that actually means
   * "a scan we asked for, for THIS root, is running". */
  ourScanRunning: boolean;
  /** `indexScan.files`, for the "still building" progress note. */
  filesScanned: number;
}) {
  // "" (covered) normally means what "No matches" already says. The one
  // exception: a scan this box itself asked for (the covered-but-empty
  // trigger in useListingSearch) can be running right now — `ourScanRunning`
  // is that confirmation, and `reason` (frozen at rank time) can't say so on
  // its own.
  const gap = reason !== "" ? indexGap(reason, scanning) : ourScanRunning ? "scanning" : null;
  if (gap === "disabled") {
    return (
      <>
        File indexing is off —{" "}
        <button
          type="button"
          className="fh-link-button"
          onClick={() => navigateUrl("/preferences?tab=indexing")}
        >
          enable it in Preferences
        </button>
        .
      </>
    );
  }
  if (gap === "fda") return <IndexFdaCta />;
  if (gap === "scanning") {
    return (
      <>
        {`The file index is still building${
          filesScanned > 0 ? ` (${filesScanned.toLocaleString()} files so far)` : ""
        }`}
      </>
    );
  }
  if (gap === "buildable") {
    // Covers both a folder never scanned and one `requestFolderScan` (the
    // hook, on demand) already asked for and that gave up before an answer
    // came back — the same "uncovered, but nothing more specific" state
    // `indexGap` folds into `buildable` for the home page's box. No "index
    // my files" button here: the on-demand scan already fired when the query
    // went out, so there is nothing left for this row to offer beyond saying
    // so.
    return <>Your files aren’t indexed yet — one scan is all it takes.</>;
  }
  if (gap === "unavailable") {
    // mount / package / ignored: no scan will ever cover this folder, so
    // nothing is coming to wait for.
    return <>This location can’t be indexed.</>;
  }
  return <>No matches</>;
}
