// /ai-models — the page chrome, the tab strip, and the dispatch. Nothing else.
//
// Five surfaces share this heading, and only this heading: a playground (pick a
// local model and use it), the Local inventory (what the Hugging Face cache
// holds and the deletions that free it), Discover (what the Hub has that this
// machine could run), Engines (which backend serves each capability) and Usage
// (what this process has generated). Each owns a directory beside this file;
// this file owns the frame they hang in.
//
// **A tab is a PATH, not a query param** (`/ai-models/local`, routes.ts) — but
// still one mounted component, unkeyed by the nav epoch. The reason is in
// lib/useCacheScan.ts: the cache walk is a filesystem crawl that three of the
// five tabs read, and a remount on every tab click would re-walk it and throw
// away whatever was typed into Discover's search. The URL is where the CHOICE
// lives (so the back button undoes it, and so every tab has an address); the
// mount is where the SHARED WORK lives.
//
// Page chrome AND the cards are the cc-* family — cc-mdgrid/cc-mdcard, the same
// card the Claude config panel's MD Files section uses — so the shell's
// non-explorer pages read as one surface rather than each inventing a list.
// Only what those classes have no answer for is local (styles/ai-models.css):
// the size figure, the Explore link, the revision drawer, and the tab strip.
//
// This page's sidebar entry is UNCONDITIONAL (HF-8, D265), so nothing here
// reports the cache's existence to anyone. `data.exists` stays, with the two
// readers it always had: the caption, which only links a cache directory that
// is really there, and the empty state, which says WHICH nothing it found.
import { useMemo } from "react";
import DiscoverTab from "./discover/DiscoverTab";
import EnginesTab from "./engines/EnginesTab";
import { LocalTab } from "./local/LocalTab";
import PlaygroundTab from "./playground/PlaygroundTab";
import UsageTab from "./usage/UsageTab";
import { useCacheScan } from "./lib/useCacheScan";
import { refreshAiRuntime } from "./lib/aiRuntime";
import { AI_MODELS_TABS, tabFromPath, tabHref, type AiModelsTab } from "./routes";
import { useNavEpoch } from "@platform/lib/hooks";
import { formatSize } from "@platform/lib/format";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";

/** The strip's label and hover for each tab, in strip order (AI_MODELS_TABS).
 *  A table rather than five near-identical <button> blocks: the buttons
 *  differed only in these two strings and the tab they named, and five copies
 *  of the same markup is five places to forget an aria attribute. */
const TAB_CHROME: Record<AiModelsTab, { label: string; title: string }> = {
  playground: {
    label: "Playground",
    title: "Try a local model — chat, images, transcription",
  },
  local: { label: "Local", title: "Models already on this machine" },
  discover: {
    label: "Discover",
    title: "Search the Hugging Face Hub for models this app can run",
  },
  engines: { label: "Engines", title: "Which backend runs each kind of local model" },
  usage: { label: "Usage", title: "Tokens this app has generated since the server started" },
};

export default function AiModelsPage() {
  // **The tab lives in the URL, not in state.** It makes the choice
  // bookmarkable and — the reason it is worth doing — it puts the toggle on the
  // BACK BUTTON, which is where a user reaches for "put it back how it was".
  // `useNavEpoch` is the subscription: it counts pushState and popstate alike,
  // so a back out of Discover re-reads the path and lands where it was.
  const navEpoch = useNavEpoch();
  const tab = useMemo(() => tabFromPath(location.pathname), [navEpoch]);

  // The cache walk, held here and read by three tabs — see lib/useCacheScan.ts
  // for why it cannot live inside any one of them.
  const scan = useCacheScan();
  const { data, repos, jobByModel, onDisk, downloading, settling } = scan;

  // The Engines tab changed something this page is showing. Two refreshes, for
  // two reasons: the listing is re-read because `repo.engine` is the registry's
  // verdict under the current preference (a switch rewrites a tag and a Load
  // refusal on every card without moving a byte), and the runtime is re-read
  // because a switch can EVICT — a Loaded badge on a model the server just
  // unloaded is the page asserting a process that is gone. Called only for a
  // switch that moved something (`switchOutcome`), so re-picking the engine
  // already in force costs no disk walk.
  const onEnginesSwitched = () => {
    scan.bumpScan();
    refreshAiRuntime();
  };

  return (
    <div className="cc-root">
      {/* The playground fills the viewport and scrolls its own columns (the
          sidebar, the chat log) — the other tabs stay ordinary scrolling
          pages, so the flex column is scoped to the one tab that wants it. */}
      <main className={"cc-main" + (tab === "playground" ? " pg-fill" : "")}>
        <div className="cc-page-head">
          <div>
            <h2 className="cc-heading">AI Models</h2>
            {/* Monospace only for the cache PATH below — the Discover and
                Engines captions are plain sentences, and the Engines tab's own
                note (`.am-engines-note`) sits right under this one in the same
                proportional font. Applying `.cc-mono` to every branch made
                that one sentence look like a filesystem fact next to the other
                that does not. */}
            <div className={"cc-caption" + (tab === "local" && data ? " cc-mono" : "")}>
              {tab === "playground" ? (
                // What the tab is FOR: trying a model, not managing one — the
                // other four tabs are the managing.
                "Pick a local model and try it — chat, images, transcription"
              ) : tab === "discover" ? (
                // What the tab is FOR, and the constraint in the same breath.
                // It said "Models on the Hugging Face Hub", which was true of a
                // search returning fill-mask models nothing here could load —
                // and the whole point of D313 is that this tab now only shows
                // what this machine could actually download and run.
                "Models on the Hugging Face Hub this app can run"
              ) : tab === "usage" ? (
                // The window, stated in the chrome, because every figure on the
                // tab is bounded by it and none of them is a lifetime total.
                "Tokens, speed and failures through fused.ai since this server started"
              ) : tab === "engines" ? (
                // Not the cache path: this tab is not about the disk, and a
                // caption naming a directory over a panel of engine pickers is
                // the page's chrome contradicting its content.
                "Which backend runs each kind of local model"
              ) : data ? (
                <>
                  {/* The path is a DESTINATION, not a label. It is the one
                      place on this page that answers "where has all this
                      actually gone", and the app is a file explorer — leaving
                      it as text asks the user to copy it into the thing they
                      are already looking at. A real <a href> so middle-click
                      and copy-link work, with left-click intercepted for
                      client-side navigation like every other in-app link.

                      …but only where there is something to open. `exists:
                      false` means no download has ever created this directory,
                      and a link to a path that is not there is worse than
                      text: it looks like an answer and lands on an error. The
                      path is still SHOWN — it is where the models would go, and
                      the empty state below says so. */}
                  {data.exists ? (
                    <a
                      className="am-cache-dir"
                      href={urlForFsPath(data.cacheDir)}
                      title={`Open ${data.cacheDir} in the explorer`}
                      onClick={(e) => {
                        if (
                          e.defaultPrevented ||
                          e.button !== 0 ||
                          e.metaKey ||
                          e.ctrlKey ||
                          e.shiftKey ||
                          e.altKey
                        )
                          return;
                        e.preventDefault();
                        navigate(data.cacheDir, { isDir: true });
                      }}
                    >
                      {data.cacheDir}
                    </a>
                  ) : (
                    data.cacheDir
                  )}
                  {repos.length
                    ? ` · ${repos.length} cached · ${formatSize(data.totalSize)} total`
                    : ""}
                </>
              ) : (
                "Hugging Face cache"
              )}
            </div>
          </div>
          <div className="am-head-actions">
            <div className="am-tabs" role="tablist" aria-label="AI models">
              {AI_MODELS_TABS.map((name) => (
                // A real <a href>, unlike the <button>s these replaced: a tab
                // is an address now, so middle-click and copy-link should reach
                // it the way they reach every other link in this app. The
                // left-click is still intercepted for client-side navigation —
                // the same shape the cache-path link above uses, and every
                // in-app link beside it.
                <a
                  key={name}
                  role="tab"
                  aria-selected={tab === name}
                  className={"am-tab" + (tab === name ? " active" : "")}
                  href={tabHref(name)}
                  title={TAB_CHROME[name].title}
                  onClick={(e) => {
                    if (
                      e.defaultPrevented ||
                      e.button !== 0 ||
                      e.metaKey ||
                      e.ctrlKey ||
                      e.shiftKey ||
                      e.altKey
                    )
                      return;
                    e.preventDefault();
                    if (name !== tab) navigateUrl(tabHref(name));
                  }}
                >
                  {TAB_CHROME[name].label}
                </a>
              ))}
            </div>
          </div>
        </div>
        {/* Mounted only while selected, every one of them. Each tab holds a
            subscription of its own that has no business running behind a tab
            nobody is looking at — the playground reads the catalog and the
            runtime, Discover queries the Hub, Usage polls every five seconds.
            The one thing that DOES run across all five is the cache walk, and
            that is exactly why it lives above them (useCacheScan). */}
        {tab === "playground" && <PlaygroundTab />}
        {tab === "local" && <LocalTab scan={scan} />}
        {tab === "discover" && (
          // The cache answer comes from the PAGE's walk, not from a second one
          // Discover runs for itself: one listing, one definition of "on this
          // machine", and no window where the two tabs disagree about the same
          // repo.
          <DiscoverTab
            onDisk={onDisk}
            downloading={downloading}
            settling={settling}
            jobByModel={jobByModel}
          />
        )}
        {tab === "engines" && <EnginesTab onSwitched={onEnginesSwitched} />}
        {tab === "usage" && <UsageTab />}
      </main>
    </div>
  );
}
