// Git auto-sync notifications inside a macOS native app window.
//
// A native window is the chrome-free embed (router.ts IS_EMBED +
// IS_NATIVE_WINDOW): no status bar, so RepoUpdatesDock — which owns the sync
// failure rows and the "Updated <app> with N changes" popup — is never mounted
// and nothing the background sync did would be visible. This mounts the same
// poll (`useRepoUpdates`, scoped to the repo this window's app lives in) and
// draws just the persistent failure rows, with the dock's own row component
// (Retry / Fix with Claude). The pull popup is the hook's own `notify()` ->
// MessagePopupCard, which NotificationHost already draws in an embed.
//
// The window cannot SPA-navigate to the explorer (an embed stays an embed for
// life), so the rows' "leave" actions load the explorer URL in this same window
// — what the title bar's Edit does. A staged Fix-with-Claude prompt rides
// across that load in sessionStorage (pending-claude-ask.ts).
import { fsPathFromLocation } from "@platform/lib/router";
import { SyncFailureRowView, useRepoUpdates } from "@shell/RepoUpdatesDock";

function leaveWindowTo(url: string): void {
  window.location.assign(url);
}

export default function NativeAppSyncNotices() {
  const { syncFailures, setSyncFailures } = useRepoUpdates({ forApp: fsPathFromLocation() });
  if (syncFailures.length === 0) return null;
  return (
    <div className="native-sync-host" role="region" aria-label="Git sync">
      {syncFailures.map((f) => (
        <SyncFailureRowView
          key={`${f.id}:${f.at}`}
          failure={f}
          onGone={(id) => setSyncFailures((fs) => fs.filter((x) => x.id !== id))}
          leaveTo={leaveWindowTo}
        />
      ))}
    </div>
  );
}
