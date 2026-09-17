// The rules for index-proposal rows in their own sibling notification card
// (SPEC-index-plugins.md decision #8, "the app proposes, the user confirms
// — never silent"), kept pure so they can be tested without a DOM or a
// poll — the same split repo-updates-lib.ts makes against
// RepoUpdatesDock.tsx: what a row SAYS lives here; the polling, the confirm/
// refuse calls and the pixels live in IndexProposalsDock.tsx.
//
// Unlike repo-updates rows, a row here never needs a client-side dismiss
// store: refusing is the server's OWN state (manifest.refuse_index removes
// the folder from both its pending and confirmed lists), so the next poll
// of GET /api/index/proposals simply stops reporting a refused folder —
// there is no "the server would show this again" case to suppress.

import type { IndexProposal } from "@platform/lib/api";

/** The folder's own last path segment, forward-slash or backslash either
 *  way — mirrors `repoName` in repo-updates-lib.ts; a row has no room for
 *  the full path, which lives in the title tooltip instead. */
export function proposalFolderName(folder: string): string {
  const norm = (folder || "").replace(/\\/g, "/").replace(/\/+$/, "");
  const i = norm.lastIndexOf("/");
  return i === -1 ? norm : norm.slice(i + 1);
}

export interface ProposalRow {
  proposal: IndexProposal;
  /** The folder's own last path segment (see `proposalFolderName`). */
  name: string;
  /** What the row says it wants to do — the kind name when the folder's
   *  manifest still declares one, a generic fallback when it doesn't (the
   *  folder was deleted or its manifest went invalid after proposing, so
   *  there is nothing left to name — `_entry` in the backend router
   *  degrades the same way rather than erroring). */
  title: string;
}

/** One row per PENDING proposal the server reported — confirmed folders
 *  are not rendered here at all (decision #8 is about the gate, not an
 *  ongoing status display; a confirmed folder already got its one-time
 *  prompt and has nothing further to ask the user). */
export function proposalRows(pending: IndexProposal[] | undefined): ProposalRow[] {
  return (pending || []).map((proposal) => ({
    proposal,
    name: proposalFolderName(proposal.folder),
    title: proposal.kind
      ? `"${proposal.kind}" wants to index this folder`
      : "An app wants to index this folder",
  }));
}
