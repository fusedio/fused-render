// NATIVE MODES DRAWN BY ANOTHER APP (PT-6 amendment, SPEC §53.4).
//
// A native template (a folder with a `native` marker and no template.html) is
// a mode the React shell draws itself. The first one, `claude`, is a shared
// app (apps/claude) the explorer may import directly. The second, `udf` — the
// canvas node view — lives in apps/canvases, which the explorer may NOT import
// (scripts/check-boundaries.mjs), and which must stay out of the main bundle
// anyway: it is loaded with the rest of the canvases app, on first use.
//
// So the shell, which composes the apps, REGISTERS a loader per mode
// (shell/App.tsx), and the explorer asks this module to mount whatever is
// registered for a mode. Nothing here knows what a mode does. An unregistered
// mode renders `fallback` (null), never a frame: a native template has no page
// to frame.
import { Component, Suspense, lazy, type ComponentType, type ReactNode } from "react";
import { isChunkLoadError } from "@platform/lib/chunk-load";

/** What every native view is given: the file (or folder) it is a view of,
 *  and where it stands — the content pane (a layout pane is an embed
 *  document's content pane). */
export interface NativeViewProps {
  path: string;
  placement: "content";
}

type Loader = () => Promise<{ default: ComponentType<NativeViewProps> }>;

const loaders = new Map<string, Loader>();
const components = new Map<string, ComponentType<NativeViewProps>>();

/** Register the component for a native mode. The loader runs on the first
 *  mount, so the chunk it imports stays out of the main bundle. */
export function registerNativeView(mode: string, loader: Loader): void {
  loaders.set(mode, loader);
  components.delete(mode);
}

/** Whether something is registered to draw `mode`. */
export function hasNativeView(mode: string): boolean {
  return loaders.has(mode);
}

function componentFor(mode: string): ComponentType<NativeViewProps> | null {
  const cached = components.get(mode);
  if (cached) return cached;
  const loader = loaders.get(mode);
  if (!loader) return null;
  const made = lazy(loader);
  components.set(mode, made);
  return made;
}

/** A failed chunk (a tab open across an update) says so, instead of taking
 *  the whole preview down with it. */
class NativeViewBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
  render() {
    if (this.state.error === null) return this.props.children;
    return (
      <div className="preview-resolving" role="alert">
        {isChunkLoadError(this.state.error)
          ? "This view was updated. Reload the window to show it."
          : `This view failed: ${this.state.error}`}
      </div>
    );
  }
}

export function NativeView({
  mode,
  fallback = null,
  ...props
}: NativeViewProps & { mode: string; fallback?: ReactNode }) {
  const View = componentFor(mode);
  if (!View) return <>{fallback}</>;
  return (
    // Keyed by mode AND file, outside the boundary: a view that failed for
    // one file must not keep its error when the pane moves to another.
    <NativeViewBoundary key={`${mode}:${props.path}`}>
      <Suspense
        fallback={
          <div className="preview-resolving">
            <span className="mode-icon-spinner" />
            Loading…
          </div>
        }
      >
        {/* Keyed by the file: a native view is a view of ONE file, and what
            it holds for it (the node view's frame id, whose server record is
            scoped to that file's node; its start flow) must not carry over
            when the same pane moves to another file. */}
        <View key={props.path} {...props} />
      </Suspense>
    </NativeViewBoundary>
  );
}
