import { Component, type ReactNode } from "react";
import { TroubleCard } from "./TroubleCard";
import { isChunkLoadError } from "../lib/chunk-load";
import { reportShellError } from "../lib/shell-errors";

/**
 * The boundary above the whole shell — the one `ChatChunkBoundary` is for the
 * chat alone.
 *
 * Without it, ANY render-time throw unmounts to the root: React 18 tears the
 * whole tree down and leaves `#root` empty, which on screen is the themed
 * background and nothing else — a black (or white) window at a valid URL, no
 * message, no button, and nothing in the server log, because the failure
 * happened after the HTML was served. The commonest throw is the one
 * `Suspense` has no opinion about: a lazy route's chunk failing to import —
 * a tab or window open across an update asking for a hash that is no longer
 * on disk, or a request landing in the seconds of a relaunch. Reload is the
 * fix for that, and the card says so. Everything else gets a plain card with
 * the message; reload is still the only recovery a boundary has.
 *
 * Reported through `reportShellError` so the server log carries the message
 * and stack beside the request lines, which is how the next blank window
 * stops being a guess.
 */
export class ShellErrorBoundary extends Component<
  { children: ReactNode },
  { error: string | null }
> {
  state: { error: string | null } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : String(error) };
  }

  componentDidCatch(error: unknown) {
    console.error("shell failed to render", error);
    reportShellError({
      where: "boundary",
      message: error instanceof Error ? error.message : String(error),
      stack: error instanceof Error ? error.stack : undefined,
    });
  }

  render() {
    if (this.state.error === null) return <>{this.props.children}</>;
    const chunk = isChunkLoadError(this.state.error);
    return (
      <div className="trouble-page">
        {chunk ? (
          <TroubleCard
            what="loading part of the app"
            error={this.state.error}
            title="This page didn't load"
            explain={
              "This window is running an older copy of the app than the one on " +
              "disk, or the app was restarting while it loaded. Reloading the page " +
              "fetches the current one."
            }
            facts={{ page: location.pathname }}
            onRetry={() => location.reload()}
            retryLabel="Reload the page"
          />
        ) : (
          <TroubleCard
            what="drawing the app"
            error={this.state.error}
            title="The app hit an error"
            explain={
              "Something broke while the page was drawing. Reloading the page is " +
              "the only way to bring it back; nothing on disk is affected."
            }
            facts={{ page: location.pathname }}
            onRetry={() => location.reload()}
            retryLabel="Reload the page"
          />
        )}
      </div>
    );
  }
}
