// Marketplace app entry point — the community app store at /marketplace
// (discover, installed, app detail) and a `.fused` file's install page.
// Behind the `marketplace_enabled` pref (platform/lib/marketplace-flag.ts):
// the shell lazy-loads this barrel only when a route needing it is opened
// with the flag on (shell/App.tsx). Data layer: platform/lib/marketplace.ts;
// presentational blocks: platform/ui/appstore.
export { MarketplacePage, parseMarketplacePath, fusedFileHref, MARKETPLACE_FILE } from "./MarketplacePage";
