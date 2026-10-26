// Where this copy came from, in words, beside the folder under the app's name
// (handoff A1): "GitHub link · follows alice/x", "Your app", "Remix of
// alice/x", and (stage B, CONTRIB_LOOP.md §18.7) "Marketplace · reviewed ·
// release of Mar 4" / "Marketplace · no longer listed". The repo is a link to its page on GitHub, opened the way the app
// opens every external link (a plain `target="_blank"` anchor — the native
// window hands it to the system browser). Nothing for a scratch Try copy, and
// nothing until the first answer lands, so the row never flashes a guess.
import type { ReactNode } from "react";
import type { AppContrib } from "@platform/lib/contrib";
import { releaseDate } from "@platform/lib/format";
import { githubUrl } from "./words";

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

export function SourceLine({ contrib }: { contrib: AppContrib | null }) {
  if (!contrib?.flags.sources) return null;
  const { kind, repo, remix_of } = contrib.copy;
  let body: ReactNode = null;
  if (kind === "marketplace") {
    const delisted = contrib.copy.listing_status === "delisted" || contrib.panel?.state === "delisted";
    const when = releaseDate(contrib.copy.approved?.at);
    body = delisted
      ? "Marketplace · no longer listed"
      : when
        ? `Marketplace · reviewed · release of ${when}`
        : "Marketplace · reviewed";
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
