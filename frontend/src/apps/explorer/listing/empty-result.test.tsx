// The listing's settled-empty-answer render: which of the index-gap messages
// (or plain "No matches") a `reason` produces. `react-test-renderer` (the
// same tool hook-harness.ts uses) rather than a plain function test, because
// two of the five gap states (`disabled`, `fda`) are JSX — a button and a
// shared callout component, not just a string.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { Clock } from "@apps/explorer/listing/hook-harness";

// router.ts (imported transitively through empty-result.tsx, for the
// "enable it in Preferences" button's `navigateUrl`) reads `location` at
// MODULE INIT — before any `beforeEach` runs — so the stub has to exist
// before the dynamic import below, the same reason Listing.test.tsx and
// FilesHome.render.test.tsx both do this ahead of their own imports.
(globalThis as Record<string, unknown>).location = { pathname: "/explorer", search: "" };
const { EmptyResultMessage } = await import("@apps/explorer/listing/empty-result");

const clock = new Clock();
let mounted: ReactTestRenderer | null = null;

beforeEach(() => {
  clock.install();
});

afterEach(() => {
  if (mounted) {
    act(() => mounted!.unmount());
    mounted = null;
  }
  clock.restore();
});

function mount(reason: string, scanning: boolean | null = null, filesScanned = 0) {
  act(() => {
    mounted = create(
      createElement(EmptyResultMessage, {
        reason: reason as never,
        scanning,
        filesScanned,
      }),
    );
  });
  return mounted!.toJSON();
}

function text(node: ReturnType<ReactTestRenderer["toJSON"]>): string {
  if (node === null) return "";
  if (Array.isArray(node)) return node.map(text).join("");
  if (typeof node === "string") return node;
  return text(node.children as never);
}

describe("EmptyResultMessage", () => {
  test("a genuinely empty, covered answer says plain No matches", () => {
    expect(text(mount(""))).toBe("No matches");
  });

  test("mount / package / ignored: no scan will ever cover it", () => {
    for (const reason of ["mount", "package", "ignored"]) {
      expect(text(mount(reason))).toContain("can’t be indexed");
    }
  });

  test("disabled: names the pref, not a scan that will never come", () => {
    const out = text(mount("disabled"));
    expect(out).toContain("File indexing is off");
    expect(out).toContain("enable it in Preferences");
  });

  test("fda: renders the shared Full Disk Access callout, not a scan button", () => {
    const json = mount("fda");
    const asString = JSON.stringify(json);
    expect(asString).toContain("fh-index-cta");
  });

  test("scanning: reports progress when the poll has a file count", () => {
    expect(text(mount("scanning", true, 4321))).toContain("4,321 files so far");
    expect(text(mount("scanning", true, 0))).toBe("The file index is still building");
  });

  test("the frozen answer's own reason still reads as scanning before the poll answers", () => {
    // `scanning: null` — the poll has not answered yet — and `reason` frozen
    // at rank time as "scanning": `indexGap` still calls this scanning
    // (its own doc comment covers why the poll's `false` is what would
    // override it, not its absence).
    expect(text(mount("scanning", null, 0))).toContain("still building");
  });

  test("uncovered — the on-demand scan gave up, or never started — offers no button here", () => {
    // The on-demand folder scan already fired when the query went out
    // (`requestFolderScan`, in useListingSearch); this row has nothing left
    // to offer beyond saying so, unlike the home page's own "Index my files"
    // CTA.
    const out = text(mount("uncovered"));
    expect(out).toContain("aren’t indexed yet");
    expect(out).not.toContain("<button");
  });
});
