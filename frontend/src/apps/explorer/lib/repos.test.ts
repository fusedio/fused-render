import { describe, expect, it } from "bun:test";
import { emptyReposMessage } from "@apps/explorer/lib/repos";

describe("emptyReposMessage", () => {
  it("says there are none only when the index has actually looked", () => {
    const msg = emptyReposMessage({ indexed: true, scanning: false, repos: [] });
    expect(msg).toMatch(/No git repositories/);
  });

  it("keeps saying 'none' during a RESCAN over an existing index", () => {
    // scanning is independent of has_index (index-store.md §4: a rescan keeps
    // serving the last completed generation), so a rescan must not turn a real
    // answer into "still building".
    const msg = emptyReposMessage({ indexed: true, scanning: true, repos: [] });
    expect(msg).toMatch(/No git repositories/);
  });

  it("says the index is still building while the first scan runs", () => {
    const msg = emptyReposMessage({ indexed: false, scanning: true, repos: [] });
    expect(msg).toMatch(/Still building/);
    expect(msg).not.toMatch(/No git repositories/);
  });

  it("points at the rebuild control when nothing has ever indexed", () => {
    const msg = emptyReposMessage({ indexed: false, scanning: false, repos: [] });
    expect(msg).toMatch(/Preferences → Indexing/);
    expect(msg).not.toMatch(/No git repositories/);
  });
});
