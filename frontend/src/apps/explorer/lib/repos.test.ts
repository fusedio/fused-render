import { describe, expect, it } from "bun:test";
import { emptyReposMessage, withLiveScanning } from "@apps/explorer/lib/repos";

describe("withLiveScanning", () => {
  const stale = { indexed: false, scanning: true, repos: [] };

  it("does not lower scanning when a scan just finished", () => {
    // The flicker: indexed is still false from the pre-scan response, so lowering
    // scanning here renders "go rebuild the index" for as long as the refetch
    // takes — telling the user to do the thing that just happened.
    const merged = withLiveScanning(stale, false);
    expect(merged.scanning).toBe(true);
    expect(emptyReposMessage(merged)).toMatch(/Still building/);
  });

  it("raises scanning the moment a scan starts", () => {
    const idle = { indexed: false, scanning: false, repos: [] };
    expect(emptyReposMessage(idle)).toMatch(/Preferences → Indexing/);
    expect(emptyReposMessage(withLiveScanning(idle, true))).toMatch(/Still building/);
  });

  it("is a no-op with no poll data yet", () => {
    expect(withLiveScanning(stale, null)).toBe(stale);
  });

  it("defers to a fresh response: once refetched, the server's value stands", () => {
    // The post-refetch state for an index that finished but is still unusable
    // (e.g. another configured root is unreconciled) must reach the real message.
    const fresh = { indexed: false, scanning: false, repos: [] };
    expect(withLiveScanning(fresh, false).scanning).toBe(false);
    expect(emptyReposMessage(withLiveScanning(fresh, false)))
      .toMatch(/Preferences → Indexing/);
  });
});

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
