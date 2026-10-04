// shareRow(...) — the Share… row's decision logic, pulled out of Preview.tsx's
// fileGroups() as a pure function (share-any-file-plan.md task 7) so it is
// testable without mounting Preview itself. Preview.tsx is a very large
// component wired to statPath/getAppEntry/resolveConditions/getPrefs/
// getShareFileStatus and more; Listing.test.tsx's own precedent (testing
// snapshotListing and useDirListing directly, never a full component mount)
// is why this file does the same rather than attempting one.
import { expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

// A dynamic import, not a static one: Preview.tsx transitively pulls in
// router.ts (its module-init IIFE reads `location` at import time), and a
// static specifier is hoisted above installDomShim() regardless of where it
// sits textually — share-file.test.ts and ShareFileModal.test.tsx hit the
// same trap and dodge it the same way.
const { shareRow } = await import("@apps/explorer/Preview");

const noop = () => {};

test("flag off: row is absent entirely, not merely disabled", () => {
  const rows = shareRow({
    sharingEnabled: false,
    isDir: false,
    isAppEntry: false,
    name: "demo.parquet",
    eligibility: { canShare: true, refusal: null },
    onClick: noop,
  });
  expect(rows).toEqual([]);
});

test("a directory: row is absent entirely — that's the app sheet's job, not this one's", () => {
  const rows = shareRow({
    sharingEnabled: true,
    isDir: true,
    isAppEntry: false,
    name: "some-folder",
    eligibility: { canShare: true, refusal: null },
    onClick: noop,
  });
  expect(rows).toEqual([]);
});

test("flag on, a file, eligible: one enabled Share… row", () => {
  const rows = shareRow({
    sharingEnabled: true,
    isDir: false,
    isAppEntry: false,
    name: "demo.parquet",
    eligibility: { canShare: true, refusal: null },
    onClick: noop,
  });
  expect(rows.length).toBe(1);
  const row = rows[0];
  if (row === "separator") throw new Error("expected a MenuItem, not a separator");
  expect(row.label).toBe("Share…");
  expect(row.disabled).toBeFalsy();
  expect(row.title).toContain("demo.parquet");
});

test("flag on, a file, no viewer for the extension: present but disabled, with the refusal as the reason", () => {
  const rows = shareRow({
    sharingEnabled: true,
    isDir: false,
    isAppEntry: false,
    name: "notebook.ipynb",
    eligibility: { canShare: false, refusal: "No Fused viewer for .ipynb yet" },
    onClick: noop,
  });
  expect(rows.length).toBe(1);
  const row = rows[0];
  if (row === "separator") throw new Error("expected a MenuItem, not a separator");
  expect(row.label).toBe("Share…");
  expect(row.disabled).toBe(true);
  expect(row.title).toBe("No Fused viewer for .ipynb yet");
});

test("flag on, a file, indeterminate (no refusal text yet): disabled with a generic reason", () => {
  const rows = shareRow({
    sharingEnabled: true,
    isDir: false,
    isAppEntry: false,
    name: "demo.parquet",
    eligibility: { canShare: false, refusal: null },
    onClick: noop,
  });
  expect(rows.length).toBe(1);
  const row = rows[0];
  if (row === "separator") throw new Error("expected a MenuItem, not a separator");
  expect(row.disabled).toBe(true);
  expect(row.title).toBe("This file type can't be shared yet");
});

test("onClick is passed through unchanged", () => {
  let clicked = false;
  const rows = shareRow({
    sharingEnabled: true,
    isDir: false,
    isAppEntry: false,
    name: "demo.parquet",
    eligibility: { canShare: true, refusal: null },
    onClick: () => {
      clicked = true;
    },
  });
  const row = rows[0];
  if (row === "separator") throw new Error("expected a MenuItem, not a separator");
  row.onClick?.();
  expect(clicked).toBe(true);
});

// The app rows above this one already carry a Share… (useAppActionRows), so an
// entry file that also got this row showed the word twice in one menu — owner,
// 2026-09-22. The app's Share is the one that survives: it publishes the folder
// the page runs out of, not the lone .html.
test("an app's entry file: row is absent — the app rows' Share… is the only one", () => {
  const rows = shareRow({
    sharingEnabled: true,
    isDir: false,
    isAppEntry: true,
    name: "index.html",
    eligibility: { canShare: true, refusal: null },
    onClick: noop,
  });
  expect(rows).toEqual([]);
});
