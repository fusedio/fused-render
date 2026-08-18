// Generates the cross-language ranking fixture from the JS ranker, which is
// the authority: the live walk is ranked by it and cannot be ranked by
// anything else, so Python must agree with it, not the other way round.
//
// Run:  bun scripts/gen-rank-fixture.ts
// Writes tests/fixtures/rank-parity.json, asserted by BOTH
// tests/test_index_rank.py and
// frontend/src/apps/explorer/listing/rank-parity.test.ts.
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const { scoreEntries, rankCompare, queryWantsHidden } = await import(
  join(ROOT, "frontend/src/apps/explorer/listing/search.ts")
);

// Synthetic but realistic: covers substring-over-fuzzy, exact-name and
// prefix-name bonuses, camelCase humps, separator segment starts, dot-entries,
// ancestor-only matches, depth tie-breaks, and spans the maxSpan bound refuses.
// Deliberately synthetic — a committed fixture carries no real user paths.
const paths = [
  "README.md",
  "docs/README.md",
  "docs/index.md",
  "docs/architecture/index.md",
  "docs/LINUX_DESKTOP_SPEC.md",
  "docs/export/EXPORT.md",
  "index.md",
  "index/specs/index-store.md",
  "index/specs/server-api.md",
  "index/store.py",
  "src/index.ts",
  "src/indexStore.ts",
  "src/IndexStoreView.tsx",
  "src/render/renderer.ts",
  "src/render/RenderTarget.ts",
  "src/myrender.ts",
  "render/a/b/c/d/e/f/deep-thing.bin",
  "Downloads",
  "Downloads/report.pdf",
  "DownloadStage/notes.txt",
  "downloads-old/report.pdf",
  ".env",
  ".env.local",
  ".config/fused/settings.json",
  "env/bin/activate",
  "environment.yml",
  "mycfgfile.txt",
  "c/f/g/notes.txt",
  "a/b/config.yaml",
  "config.yaml",
  "frontend/src/apps/explorer/FilesHome.tsx",
  "frontend/src/apps/explorer/listing/search.ts",
  "frontend/src/platform/lib/fuzzy.ts",
  "fused_render/index/query.py",
  "fused_render/server/routers/index.py",
  "tests/test_index_api.py",
  "Zarr v3 multiscale pyramid budget notes.md",
  "project/sub/deep/nested/thing/file.txt",
  "file.txt",
  "FILE.TXT",
  // Ties the ordering rules the list above leaves to chance: two rels equal on
  // longestRun/tier/score/depth, separated only by the final case-insensitive
  // path compare (and by case alone, which is what makes it case-INsensitive).
  "notes/Alpha.txt",
  "notes/alpha.txt",
];

const dirs = new Set([
  "docs", "docs/architecture", "index", "index/specs", "src", "src/render",
  "Downloads", "DownloadStage", "downloads-old", ".config", ".config/fused",
  "env", "env/bin", "frontend", "a", "a/b", "c", "c/f", "c/f/g", "notes",
]);

const entries = [
  ...paths.map((rel) => ({ rel, is_dir: false, size: 10, mtime: 1 })),
  ...[...dirs].map((rel) => ({ rel, is_dir: true, size: null, mtime: null })),
];

const queries = [
  "readme", "README.md", "index.md", "index", "indexstore", "render",
  "myrender", "download", "Downloads", "env", ".env", ".py", "cfg",
  "fr/fe", "search", "zmp", "file.txt", "q", "notafile",
  // Ancestor-only (tier 3), a hidden query with a mid-path dot segment,
  // an alpha tie-break, a case-only difference, and a camel-hump name.
  "specs/", "frontend/.", "alpha", "FILE.TXT", "downloadstage",
];

const expected: Record<string, string[]> = {};
for (const q of queries) {
  const hits = scoreEntries(q, entries, 0, queryWantsHidden(q));
  hits.sort(rankCompare);
  expected[q] = hits.map((h: any) => h.entry.rel);
}

writeFileSync(
  join(ROOT, "tests/fixtures/rank-parity.json"),
  JSON.stringify(
    {
      _comment:
        "Generated from the JS ranker (listing/search.ts + platform/lib/fuzzy.ts), " +
        "which is the authority because it is the only thing that can rank a live " +
        "walk. Both the bun test and pytest assert against this. Regenerate with " +
        "`bun scripts/gen-rank-fixture.ts` when the JS ranker's ordering changes on purpose.",
      entries,
      queries,
      expected,
    },
    null,
    2,
  ) + "\n",
);
console.log("queries:", queries.length, "entries:", entries.length);
for (const q of ["index.md", "render", "fr/fe", "zmp", "notafile"]) {
  console.log(`  ${q.padEnd(12)} -> ${expected[q].length} hits`, expected[q].slice(0, 3));
}
