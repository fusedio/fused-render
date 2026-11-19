// The overlay a specialist's "show" card opens (docs/specialists.md §15): a canvas, or one node of a canvas (a UDF,
// a widget, an HTML node, a note), drawn the way the explorer draws it when the same file is opened there: a node
// through the `_node` native view (platform/ui/NativeView, the canvases app's NodeView: the hosted workbench in a
// frame, cloned and synced as needed), a whole canvas as its workspace page in a chrome-free frame. Nothing is
// mounted until the card (apps/claude/ui/ViewCard) opens it, and closing unmounts it (the frame stops).
import { NODE_MODE } from "@platform/lib/mode-visibility";
import { navigateUrl, urlForFsPath } from "@platform/lib/router";
import { Modal } from "@platform/ui/modal/Modal";
import { NativeView, hasNativeView } from "@platform/ui/NativeView";
import { CanvasIcon, HtmlNodeIcon, PythonNodeIcon, TextNodeIcon, WidgetNodeIcon } from "@platform/ui/NodeTypeIcons";
import { viewOf, type ViewRef } from "@platform/lib/view-ref";
import "./view-modal.css";

export { viewOf, type ViewRef };

const KIND_LABEL: Record<string, string> = { canvas: "Canvas", udf: "UDF", widget: "Widget", html: "HTML", note: "Note" };
// The workbench's own marks for its node types (NodeTypeIcons: the ones its add-node bar wears), so a card reads
// as the same thing the canvas shows.
const KIND_ICON: Record<string, (p: { size?: number }) => JSX.Element> = {
  canvas: CanvasIcon, udf: PythonNodeIcon, widget: WidgetNodeIcon, html: HtmlNodeIcon, note: TextNodeIcon,
};

/** The glyph for a card's kind; a kind this page does not know wears the canvas mark. */
export function ViewKindIcon({ kind, size = 16 }: { kind: string; size?: number }) {
  const Icon = Object.prototype.hasOwnProperty.call(KIND_ICON, kind) ? KIND_ICON[kind]! : CanvasIcon;
  return <Icon size={size} />;
}

export const viewKindLabel = (kind: string): string => KIND_LABEL[kind] || "File";

/** The whole canvas, as its own page without the shell's chrome (`?embed=1`, router IS_QUERY_EMBED). */
export const canvasFrameUrl = (v: ViewRef): string =>
  `/canvases/${encodeURIComponent(v.env)}/${encodeURIComponent(v.canvas)}?embed=1`;
export const canvasPageUrl = (v: ViewRef): string => `/canvases/${encodeURIComponent(v.env)}/${encodeURIComponent(v.canvas)}`;

/** What the card says under the title: "Widget · in Parcels_overview · prod". */
export function viewSubtitle(v: ViewRef): string {
  return [viewKindLabel(v.kind), v.kind !== "canvas" && v.canvas ? `in ${v.canvas}` : "", v.env].filter(Boolean).join(" · ");
}

export function ViewModal({ view, onClose, getContainer }: { view: ViewRef; onClose: () => void; getContainer?: () => Element | null }) {
  const isCanvas = view.kind === "canvas";
  const go = (url: string) => { onClose(); void navigateUrl(url); };
  return (
    <Modal
      titleLead={<span className="view-modal-ico"><ViewKindIcon kind={view.kind} /></span>}
      title={<>{view.title} <span className="view-modal-sub">{viewSubtitle(view)}</span></>}
      ariaLabel={`${viewKindLabel(view.kind)} ${view.title}`}
      onClose={onClose}
      dialogClassName="view-modal"
      plainBody
      {...(getContainer ? { getContainer } : {})}
      headActions={
        <>
          {/* One way to the canvas per overlay: a node's own strip below has "Open in canvas", which also
              selects the node, so the head offers it only for a whole canvas. */}
          {isCanvas && view.canvas && view.env ? <button type="button" className="view-modal-btn" onClick={() => go(canvasPageUrl(view))}>Open canvas</button> : null}
          {!isCanvas ? <button type="button" className="view-modal-btn" onClick={() => go(urlForFsPath(view.path))}>Open in explorer</button> : null}
        </>
      }
    >
      <div className="view-modal-body">
        {isCanvas ? (
          view.canvas && view.env
            ? <iframe className="view-modal-frame" src={canvasFrameUrl(view)} title={`Canvas ${view.title}`} />
            : <p className="view-modal-missing">This canvas can&rsquo;t be opened from here.</p>
        ) : hasNativeView(NODE_MODE) ? (
          <NativeView mode={NODE_MODE} path={view.path} placement="content" />
        ) : (
          <p className="view-modal-missing">This view isn&rsquo;t available in this window. Use &ldquo;Open in explorer&rdquo;.</p>
        )}
      </div>
    </Modal>
  );
}
