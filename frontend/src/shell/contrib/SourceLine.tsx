// Where this copy came from, in words, beside the folder under the app's name
// (handoff A1): "GitHub link · follows alice/x", "Your app", "Remix of
// alice/x", and (stage B, CONTRIB_LOOP.md §18.7) "Marketplace · reviewed ·
// release of Mar 4" / "Marketplace · no longer listed". The repo is a link to its page on GitHub, opened the way the app
// opens every external link (a plain `target="_blank"` anchor — the native
// window hands it to the system browser). Nothing for a scratch Try copy, and
// nothing until the first answer lands, so the row never flashes a guess.
//
// SIMPLE SHARING (§20): for an app the user owns, while the Community switch
// is on, this line is the app's whole status in one sentence — the header is on
// every tab and the Overview stays the app, so this is the one place a
// novice reads where the app stands: "Only on this Mac", "Online · Copy
// install link", "Online · in the Community since Oct 28", "Online · Fused is
// reviewing it for the Community", "Online · 2 edits not online yet". Read
// off the ladder (`useLadder`'s answer the page already holds); the install
// link alone follows Contributing (`sources`).
import type { ReactNode } from "react";
import type { AppContrib, LadderAnswer } from "@platform/lib/contrib";
import { ladderListing } from "@platform/lib/contrib";
import { copyToClipboard } from "@platform/lib/clipboard";
import { releaseDate } from "@platform/lib/format";
import { notify } from "@platform/lib/notifications";
import { githubUrl, plural } from "./words";

function RepoLink({ repo }: { repo: string }) {
  return (
    <a
      className="contrib-source-repo"
      href={githubUrl(repo)}
      target="_blank"
      rel="noopener noreferrer"
      title={`Open ${repo} on GitHub`}
    >
      {repo}
    </a>
  );
}

/** Simple sharing applies (§20): an own app's ladder with the Community
 *  switch (`marketplace`, the store — Settings' "Community" row) on. Shared
 *  by the Share sheet, this line and the Community tab. */
export function simpleOn(ladder: LadderAnswer | null | undefined): boolean {
  return !!ladder?.ok && ladder.mine && !!ladder.flags?.marketplace;
}

/** The one status sentence of Simple sharing for an own app, as parts joined
 *  by " · ". Null when the ladder is not an own app's. Exported for the
 *  Community tab's Simple view, which says the same thing. */
export function simpleStatus(ladder: LadderAnswer | null | undefined): string[] | null {
  if (!ladder?.ok || !ladder.mine || !ladder.github) return null;
  const rung = ladder.rung ?? 1;
  if (rung < 2) return ["Only on this Mac"];
  const parts = ["Online"];
  const { listing, sentence } = ladderListing(ladder.listing);
  const state = ladder.listing?.state ?? listing?.review?.status ?? (listing?.listed ? "live" : null);
  if (state === "live") {
    const when = releaseDate(listing?.approved?.at ?? null);
    parts.push(when ? `in the Community since ${when}` : "in the Community");
  } else if (state === "in-review") {
    parts.push("Fused is reviewing it for the Community");
  } else if (state === "needs-confirmation") {
    parts.push("the Community needs your confirmation");
  } else if (state === "failed") {
    parts.push("the Community review didn't pass");
  } else if (state === "removing") {
    parts.push(sentence || "leaving the Community");
  }
  if (ladder.github.unpushed > 0) parts.push(`${plural(ladder.github.unpushed, "edit")} not online yet`);
  return parts;
}

export function SourceLine({ contrib, ladder }: { contrib: AppContrib | null; ladder?: LadderAnswer | null }) {
  const simple = simpleStatus(ladder);
  if (simpleOn(ladder) && simple) {
    const link = ladder?.flags.sources ? ladder.github.link : null;
    const copyLink = async () => {
      if (!link) return;
      const ok = await copyToClipboard(link);
      notify({ title: ok ? "Install link copied" : "Couldn't copy the link", tone: ok ? "info" : "error" });
    };
    return (
      <span className="contrib-source" data-simple="true">
        {simple.join(" · ")}
        {link && (
          <>
            {" · "}
            <button type="button" className="contrib-source-action" onClick={() => void copyLink()}>
              Copy install link
            </button>
          </>
        )}
      </span>
    );
  }
  // A Marketplace copy says where it came from with the Marketplace switch
  // alone (the server only answers for it then, §18.8).
  if (!contrib || (!contrib.flags.sources && contrib.copy.kind !== "marketplace")) return null;
  const { kind, repo, remix_of } = contrib.copy;
  let body: ReactNode = null;
  if (kind === "marketplace") {
    const delisted = contrib.copy.listing_status === "delisted" || contrib.panel?.state === "delisted";
    const when = releaseDate(contrib.copy.approved?.at);
    body = delisted
      ? "Community · no longer listed"
      : when
        ? `Community · reviewed · release of ${when}`
        : "Community · reviewed";
  } else if (kind === "github" && repo) {
    body = (
      <>
        GitHub link · follows <RepoLink repo={repo} />
      </>
    );
  } else if (kind === "own" && remix_of) {
    body = (
      <>
        Remix of <RepoLink repo={remix_of} />
      </>
    );
  } else if (kind === "own") {
    body = "Your app";
  }
  if (!body) return null;
  return <span className="contrib-source">{body}</span>;
}
