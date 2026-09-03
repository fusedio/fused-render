// /ai-models — the page chrome, the tab strip, and the dispatch. Nothing else.
//
// Five surfaces share this heading, and only this heading: a playground (pick a
// local model and use it), the Local view (what the Hugging Face cache holds,
// what to download next, a search of the whole Hub, and the deletions that free
// the disk), Benchmark (how fast each of those runs here, on a fixed workload),
// Engines (which backend serves each capability) and Usage (what this process has
// generated). Each owns a directory beside this file; this file owns the frame
// they hang in.
//
// **A tab is a PATH, not a query param** (`/ai-models/local`, routes.ts) — but
// still one mounted component, unkeyed by the nav epoch. The reason is in
// lib/useCacheScan.ts: the cache walk is a filesystem crawl that three of the
// five tabs read, and a remount on every tab click would re-walk every blob in the
// Hugging Face cache. The URL is where the CHOICE lives (so the back button
// undoes it, and so every tab has an address); the mount is where the SHARED
// WORK lives.
//
// Frame: the Flow composites (Page / PageHeader / PageBody) and shadcn Tabs. The
// tabs are real anchors under base-ui triggers (`render`), the same pattern the
// app page's strip uses — a tab is an address, so middle-click and copy-link
// must reach it.
//
// This page's sidebar entry is UNCONDITIONAL (HF-8, D265), so nothing here
// reports the cache's existence to anyone. `data.exists` stays, with the two
// readers it always had: the caption, which only links a cache directory that
// is really there, and the empty state, which says WHICH nothing it found.
import { useMemo, type MouseEvent } from "react";
import { BenchmarkTab } from "./benchmark/BenchmarkTab";
import EnginesTab from "./engines/EnginesTab";
import { LocalTab } from "./local/LocalTab";
import PlaygroundTab from "./playground/PlaygroundTab";
import UsageTab from "./usage/UsageTab";
import { useCacheScan } from "./lib/useCacheScan";
import { refreshAiRuntime } from "./lib/aiRuntime";
import { AI_MODELS_TABS, tabFromPath, tabHref, tabLabel, type AiModelsTab } from "./routes";
import { useNavEpoch } from "@platform/lib/hooks";
import { formatSize } from "@platform/lib/format";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { Page, PageBody, PageHeader } from "@platform/ui/flow/Typography";

/** The strip's hover for each tab, in strip order (AI_MODELS_TABS). The label
 *  itself lives in `routes.ts` (`tabLabel`) — the one thing about a tab that
 *  copy elsewhere names too — so it has exactly one definition. A `Record` over
 *  the union, so adding a tab to `AiModelsTab` fails to compile until it has a
 *  title. */
const TAB_CHROME: Record<AiModelsTab, { label: string; title: string }> = {
  playground: {
    label: tabLabel("playground"),
    title: "Try a local model — chat, images, transcription",
  },
  // **"Models", not "Local"** (D438): with the curation's recommendations in
  // every row and a Hub search box at the top, three of the things on the tab
  // are not local at all. The PATH stays `/ai-models/local` — it is in
  // bookmarks and in every `tabHref` cross-link.
  local: { label: tabLabel("local"), title: "Models on this machine, what to get next, and the Hub" },
  benchmark: {
    label: tabLabel("benchmark"),
    title: "How fast each downloaded model runs on this machine",
  },
  engines: { label: tabLabel("engines"), title: "Which backend runs each kind of local model" },
  usage: { label: tabLabel("usage"), title: "Tokens this app has generated since the server started" },
};

/** Plain left-click only — every modified or non-primary click keeps the
 *  anchor's native behaviour (new tab, copy link). */
function plainClick(e: MouseEvent): boolean {
  return !(e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey);
}

export default function AiModelsPage() {
  // **The tab lives in the URL, not in state.** `useNavEpoch` counts pushState
  // and popstate alike, so a back out of any tab re-reads the path.
  const navEpoch = useNavEpoch();
  const tab = useMemo(() => tabFromPath(location.pathname), [navEpoch]);

  // The cache walk, held here and read by three tabs — see lib/useCacheScan.ts.
  const scan = useCacheScan();
  const { data, repos } = scan;

  // The Engines tab changed something this page is showing: the listing is
  // re-read because `repo.engine` is the registry's verdict under the current
  // preference, and the runtime because a switch can EVICT. Called only for a
  // switch that moved something (`switchOutcome`).
  const onEnginesSwitched = () => {
    scan.bumpScan();
    refreshAiRuntime();
  };

  // No caption on the PLAYGROUND, where the tab below it is the explanation.
  // Every other tab carries a fact its tab does not repeat.
  const caption =
    tab === "playground" ? null : tab === "usage" ? (
      "Tokens, speed and failures through fused.ai since this server started"
    ) : tab === "benchmark" ? (
      "A fixed workload per capability, timed on this machine"
    ) : tab === "engines" ? (
      "Which backend runs each kind of local model"
    ) : data ? (
      <span className="font-mono text-xs">
        {/* The path is a DESTINATION: a real <a href>, left-click intercepted for
            client-side navigation — but only where there is something to open. */}
        {data.exists ? (
          <a
            className="text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
            href={urlForFsPath(data.cacheDir)}
            title={`Open ${data.cacheDir} in the explorer`}
            onClick={(e) => {
              if (!plainClick(e)) return;
              e.preventDefault();
              navigate(data.cacheDir, { isDir: true });
            }}
          >
            {data.cacheDir}
          </a>
        ) : (
          data.cacheDir
        )}
        {repos.length ? ` · ${repos.length} cached · ${formatSize(data.totalSize)} total` : ""}
      </span>
    ) : (
      "Hugging Face cache"
    );

  const strip = (
    <Tabs value={tab}>
      <TabsList variant="line" aria-label="AI models" className="h-auto p-0">
        {AI_MODELS_TABS.map((name) => (
          <TabsTrigger
            key={name}
            value={name}
            // The tour's per-tab anchor (platform/lib/tours/ai.ts). An attribute
            // rather than nth-child, so reordering AI_MODELS_TABS cannot
            // silently repoint a step.
            data-tab={name}
            title={TAB_CHROME[name].title}
            className="flex-none px-3 py-1.5"
            // Base UI assumes a native <button> unless told otherwise: without
            // this the anchor gets type="button" and Space does not activate it.
            nativeButton={false}
            render={
              <a
                href={tabHref(name)}
                onClick={(e) => {
                  if (!plainClick(e)) return;
                  e.preventDefault();
                  if (name !== tab) navigateUrl(tabHref(name));
                }}
              />
            }
          >
            {TAB_CHROME[name].label}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );

  return (
    <Page>
      <PageHeader title="AI Models" description={caption} actions={strip} />
      {/* Mounted only while selected, every one of them: each tab holds a
          subscription of its own that has no business running behind a tab
          nobody is looking at. The one thing that DOES run across all five is
          the cache walk, and that is why it lives above them (useCacheScan). */}
      {tab === "playground" ? (
        // The playground fills the viewport and scrolls its own columns, so it
        // gets a flex column that owns the remaining height rather than the
        // scrolling PageBody the other tabs use.
        <div className="flex flex-1 min-h-0 flex-col overflow-y-hidden pb-4">
          <PlaygroundTab />
        </div>
      ) : (
        <PageBody>
          {tab === "local" && <LocalTab scan={scan} />}
          {/* Takes the same `scan` the Local tab does rather than re-walking
              the cache — "which models could I benchmark" is the very question
              that walk already answers. */}
          {tab === "benchmark" && <BenchmarkTab scan={scan} />}
          {tab === "engines" && <EnginesTab onSwitched={onEnginesSwitched} />}
          {tab === "usage" && <UsageTab />}
        </PageBody>
      )}
    </Page>
  );
}
