// Row shaping for index-proposal rows — the pure half of
// IndexProposalsDock.tsx.
import { describe, expect, it } from "bun:test";
import { proposalFolderName, proposalRows } from "./index-proposals-lib";
import type { IndexProposal } from "@platform/lib/api";

describe("proposalFolderName", () => {
  it("takes the last path segment", () => {
    expect(proposalFolderName("/Users/me/Work/widget")).toBe("widget");
  });

  it("strips a trailing slash before taking the segment", () => {
    expect(proposalFolderName("/Users/me/Work/widget/")).toBe("widget");
  });

  it("handles a backslash path", () => {
    expect(proposalFolderName("C:\\Users\\me\\widget")).toBe("widget");
  });

  it("handles an empty folder", () => {
    expect(proposalFolderName("")).toBe("");
  });
});

describe("proposalRows", () => {
  const proposal = (over: Partial<IndexProposal> = {}): IndexProposal => ({
    folder: "/Users/me/Work/widget",
    kind: "widgets",
    ...over,
  });

  it("names the folder and the declared kind", () => {
    const [row] = proposalRows([proposal()]);
    expect(row.name).toBe("widget");
    expect(row.title).toBe('"widgets" wants to index this folder');
  });

  it("falls back to a generic title when the kind is unknown", () => {
    const [row] = proposalRows([proposal({ kind: null })]);
    expect(row.title).toBe("An app wants to index this folder");
  });

  it("is empty for no pending proposals", () => {
    expect(proposalRows([])).toEqual([]);
    expect(proposalRows(undefined)).toEqual([]);
  });

  it("carries the original proposal through for confirm/refuse to act on", () => {
    const p = proposal({ folder: "/a/b" });
    const [row] = proposalRows([p]);
    expect(row.proposal).toBe(p);
  });
});
