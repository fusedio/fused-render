// Marketplace app entry point — the community app store at /marketplace
// (discover, installed, app detail). Behind the `marketplace_enabled` pref
// (platform/lib/marketplace-flag.ts): the shell lazy-loads this barrel only
// when the route is opened with the flag on (shell/App.tsx). Data layer:
// platform/lib/marketplace.ts; presentational blocks: platform/ui/appstore.
export { MarketplacePage, parseMarketplacePath } from "./MarketplacePage";
