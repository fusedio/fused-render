import { describe, expect, it } from "bun:test";
import { formatSize } from "@platform/lib/format";
import type { Job } from "@platform/lib/jobs";
import {
  catalogSizeBytes,
  liveModelTotal,
  modelSizeHint,
  modelSizeLabel,
} from "./modelSize";

// A card used to show the catalog's approximate constant BESIDE the job row's
// own total — `~64 GB` next to `68 GB / 68 GB`, one download, two numbers, and
// the only reading available to the person looking at it is that the download
// is overrunning its own size.
//
// What these pin is the rule that resolves it — the shown figure NEVER
// UNDERSTATES — and the two traps in getting there: the units are not the same
// (decimal in the catalog, base-1024 everywhere on the page), and a row's total
// is one PHASE of a download that can have several.

/** The advertised figure and the live one, in the units each really uses. */
const CATALOG_GB = 64; // 6.4e10 bytes, decimal
const BIGGER_TOTAL = 73_014_444_032; // 68 GiB — a stale constant, row is right
const SMALLER_TOTAL = 8_000_000_000; // one phase of a bigger download

function job(extra: Partial<Job> = {}): Job {
  return {
    id: "sys:ai-model:org-m",
    title: "org/m",
    detail: "Fetching weights…",
    model: "",
    kind: "download",
    state: "running",
    done: 1_000,
    total: BIGGER_TOTAL,
    total_scope: "phase",
    unit: "bytes",
    message: "",
    page: "",
    owner: "server",
    cancellable: true,
    cancel_requested: false,
    started_at: 0,
    updated_at: 0,
    finished_at: null,
    stalled: false,
    waiting_for: "",
    ...extra,
  };
}

describe("catalogSizeBytes", () => {
  it("reads size_gb in the unit catalog.py documents, which is decimal", () => {
    // 4.62e9 bytes is written as 4.6 there; reading it as 4.6 GiB would put the
    // figure ~7% out, and pressing Download would then visibly SHRINK the
    // announced size as the row's real bytes replaced it.
    expect(catalogSizeBytes(4.6)).toBe(4_600_000_000);
    expect(catalogSizeBytes(null)).toBeNull();
    expect(catalogSizeBytes(undefined)).toBeNull();
  });

  it("is formatted like every other size on the page, so the two agree", () => {
    // The cell and the progress row under it must be the same measurement in
    // the same unit — `formatSize` is base-1024, and that is the app's unit.
    expect(modelSizeLabel(4.6, undefined)).toBe(formatSize(4_600_000_000));
  });
});

describe("modelSizeLabel", () => {
  it("prefers a running download's total when the constant understates it", () => {
    // The fetcher summed this from the live listing for exactly the files it is
    // pulling; the constant is hand-written and stale.
    expect(modelSizeLabel(CATALOG_GB, job())).toBe(formatSize(BIGGER_TOTAL));
  });

  it("keeps the advertised figure when the row reports LESS than it", () => {
    // A download can be several sequential fetches with a scoped total each
    // (`torch_image.py`'s GGUF recipe: an allow-listed snapshot, then a
    // quantized transformer from a second repo). Quoting a phase would promise
    // a download cheaper than it is, and change the number twice on the way.
    expect(modelSizeLabel(CATALOG_GB, job({ total: SMALLER_TOTAL }))).toBe(
      formatSize(catalogSizeBytes(CATALOG_GB) as number),
    );
  });

  it("falls back to the catalog figure when the row has no total yet", () => {
    // Every phase that does not know its size reports null — a venv build, a
    // weight load, a download still listing the repo.
    expect(modelSizeLabel(CATALOG_GB, job({ total: null }))).toBe(
      formatSize(catalogSizeBytes(CATALOG_GB) as number),
    );
  });

  it("ignores a total of zero, which is not a size", () => {
    expect(modelSizeLabel(CATALOG_GB, job({ total: 0 }))).toBe(
      formatSize(catalogSizeBytes(CATALOG_GB) as number),
    );
  });

  it("ignores a finished row, whose total measured a pull that is over", () => {
    expect(modelSizeLabel(CATALOG_GB, job({ state: "done" }))).toBe(
      formatSize(catalogSizeBytes(CATALOG_GB) as number),
    );
  });

  it("ignores a cancelled row, which never measured the whole repo", () => {
    expect(modelSizeLabel(CATALOG_GB, job({ state: "cancelled" }))).toBe(
      formatSize(catalogSizeBytes(CATALOG_GB) as number),
    );
  });

  it("ignores a row whose total is not bytes at all", () => {
    // `total` counts steps on an image job and seconds on a transcription;
    // formatted as bytes, 16 steps would read "16 B".
    expect(modelSizeLabel(CATALOG_GB, job({ unit: "s", total: 16 }))).toBe(
      formatSize(catalogSizeBytes(CATALOG_GB) as number),
    );
  });

  it("falls back to the catalog figure when there is no job", () => {
    expect(modelSizeLabel(CATALOG_GB, undefined)).toBe(
      formatSize(catalogSizeBytes(CATALOG_GB) as number),
    );
  });

  it("shows the em-dash when nobody recorded a size and nothing is running", () => {
    expect(modelSizeLabel(null, undefined)).toBe("—");
  });

  it("shows a live total for a model the catalog never measured", () => {
    // Nothing to understate against, and the row's figure is strictly better
    // than a dash — it is the number the bar below is counting towards.
    expect(modelSizeLabel(null, job({ total: SMALLER_TOTAL }))).toBe(
      formatSize(SMALLER_TOTAL),
    );
  });
});

describe("a \"download\"-scoped total (SPEC AI-5n, D498)", () => {
  it("wins outright over a catalog constant that is stale HIGH", () => {
    // The never-understate rule alone could never fix this: a live total
    // SMALLER than the constant used to always lose. `total_scope: "download"`
    // is `worker_base.download_plan`'s claim that this total is the WHOLE
    // download, summed before a byte moved — never a fraction the way a bare
    // phase total can be — so it wins even here.
    expect(
      modelSizeLabel(CATALOG_GB, job({ total: SMALLER_TOTAL, total_scope: "download" })),
    ).toBe(formatSize(SMALLER_TOTAL));
  });

  it("still loses to the phase rule when the row never claims the whole download", () => {
    // The default every reporter has always sent, unmigrated — same figure,
    // same job, but "phase" keeps the never-understate behaviour exactly as
    // it worked before `total_scope` existed.
    expect(
      modelSizeLabel(CATALOG_GB, job({ total: SMALLER_TOTAL, total_scope: "phase" })),
    ).toBe(formatSize(catalogSizeBytes(CATALOG_GB) as number));
  });

  it("is not approximate — a whole download total is not a hedge", () => {
    expect(
      modelSizeHint(CATALOG_GB, job({ total: SMALLER_TOTAL, total_scope: "download" }))?.approx,
    ).toBe(false);
  });

  it("does not let a PHASE total win just because the row once said \"download\"", () => {
    // Code review: `total_scope` is STICKY on the job row (the backend only
    // overwrites it when a tick's body names it), and a multi-repo download
    // interleaves two tickers — `download_plan`'s own, beating
    // `total_scope="download"` with the GRAND total, and each phase's
    // `fetch_with_progress`, ticking every second with that phase's own
    // (smaller) total. This module has no memory across ticks — it only ever
    // sees the row's CURRENT snapshot — so it can only be correct here if the
    // row it is handed already carries the RIGHT scope for its OWN total.
    // `worker_base.fetch_with_progress` fixes this at the source: every one
    // of its ticks asserts `total_scope="phase"` explicitly, so a phase
    // total is never left sitting under a leftover "download" claim from the
    // previous ticker. This test is the contract that fix depends on: a row
    // correctly scoped "phase" keeps the never-understate behaviour even
    // though nothing here can tell that another tick, a moment earlier,
    // claimed "download" about a different total.
    expect(
      modelSizeLabel(CATALOG_GB, job({ total: SMALLER_TOTAL, total_scope: "phase" })),
    ).toBe(formatSize(catalogSizeBytes(CATALOG_GB) as number));
  });
});

describe("modelSizeHint", () => {
  it("is null when there is nothing to say, so the phrase can be left out", () => {
    expect(modelSizeHint(null, undefined)).toBeNull();
  });

  it("marks the catalog's constant as approximate and a live total as not", () => {
    // "~68 GB" over a figure the download itself reported would keep the hedge
    // the live number exists to remove.
    expect(modelSizeHint(CATALOG_GB, undefined)).toEqual({
      text: formatSize(catalogSizeBytes(CATALOG_GB) as number),
      approx: true,
    });
    expect(modelSizeHint(CATALOG_GB, job())).toEqual({
      text: formatSize(BIGGER_TOTAL),
      approx: false,
    });
  });

  it("stays approximate when the advertised figure is the one being shown", () => {
    // The row reported a phase, so what is on the card is still the estimate —
    // and must still say so.
    expect(modelSizeHint(CATALOG_GB, job({ total: SMALLER_TOTAL }))?.approx).toBe(true);
  });
});

describe("liveModelTotal", () => {
  it("is null for every row that is not a running byte count", () => {
    expect(liveModelTotal(undefined)).toBeNull();
    expect(liveModelTotal(job({ state: "error" }))).toBeNull();
    expect(liveModelTotal(job())).toBe(BIGGER_TOTAL);
  });
});
