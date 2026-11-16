// WHAT A CANVAS NODE IS, AS A GLYPH (Akshil, 2026-11-16: "if it is udf then
// python icon, if it is html then html icon" — "check canvas those icons").
// The workbench's own marks for its node types (application
// `canvas-node-type-icons.tsx` and its add-node controls), so a node wears the
// same icon on the canvas and in a task head's where-tag:
//
//   .py   UDF      the Python mark   (Font Awesome Free `python`, CC BY 4.0)
//   .html HTML     the HTML file     (Phosphor `file-html-fill`, MIT)
//   .md   text     the text T        (Radix `text`, MIT)
//   .json widget   the sliders       (Font Awesome Free `sliders`, CC BY 4.0)
//
// Inlined paths, no npm dependency (FileIcons' rule); colourless, they take
// the tag's ink through `currentColor`. A file type the canvas has no mark for
// wears the explorer's icon for it (FileIcons `iconForEntry`).
import { iconForEntry } from "./FileIcons";

interface GlyphProps {
  size?: number;
  className?: string;
}

function filled(viewBox: string, d: string, evenOdd = false) {
  return function Glyph({ size = 12, className }: GlyphProps) {
    return (
      <svg
        className={"node-type-icon" + (className ? ` ${className}` : "")}
        width={size}
        height={size}
        viewBox={viewBox}
        fill="currentColor"
        aria-hidden="true"
      >
        <path d={d} {...(evenOdd ? { fillRule: "evenodd" as const, clipRule: "evenodd" as const } : {})} />
      </svg>
    );
  };
}

export const PythonNodeIcon = filled(
  "0 0 448 512",
  "M439.8 200.5c-7.7-30.9-22.3-54.2-53.4-54.2h-40.1v47.4c0 36.8-31.2 67.8-66.8 67.8H172.7c-29.2 0-53.4 25-53.4 54.3v101.8c0 29 25.2 46 53.4 54.3 33.8 9.9 66.3 11.7 106.8 0 26.9-7.8 53.4-23.5 53.4-54.3v-40.7H226.2v-13.6h160.2c31.1 0 42.6-21.7 53.4-54.2 11.2-33.5 10.7-65.7 0-108.6zM286.2 404c11.1 0 20.1 9.1 20.1 20.3 0 11.3-9 20.4-20.1 20.4-11 0-20.1-9.2-20.1-20.4.1-11.3 9.1-20.3 20.1-20.3zM167.8 248.1h106.8c29.7 0 53.4-24.5 53.4-54.3V91.9c0-29-24.4-50.7-53.4-55.6-35.8-5.9-74.7-5.6-106.8.1-45.2 8-53.4 24.7-53.4 55.6v40.7h106.9v13.6h-147c-31.1 0-58.3 18.7-66.8 54.2-9.8 40.7-10.2 66.1 0 108.6 7.6 31.6 25.7 54.2 56.8 54.2H101v-48.8c0-35.3 30.5-66.4 66.8-66.4zm-6.7-142.6c-11.1 0-20.1-9.1-20.1-20.3.1-11.3 9-20.4 20.1-20.4 11 0 20.1 9.2 20.1 20.4s-9 20.3-20.1 20.3z",
);

export const HtmlNodeIcon = filled(
  "0 0 256 256",
  "M44,128H212a4,4,0,0,0,4-4V88a8,8,0,0,0-2.34-5.66l-56-56A8,8,0,0,0,152,24H56A16,16,0,0,0,40,40v84A4,4,0,0,0,44,128ZM152,44l44,44H152ZM68,160v48a8,8,0,0,1-16,0V192H32v16a8,8,0,0,1-16,0V160a8,8,0,0,1,16,0v16H52V160a8,8,0,0,1,16,0Zm56,0a8,8,0,0,1-8,8h-8v40a8,8,0,0,1-16,0V168H84a8,8,0,0,1,0-16h32A8,8,0,0,1,124,160Zm72,0v48a8,8,0,0,1-16,0V184l-9.6,12.8a8,8,0,0,1-12.8,0L148,184v24a8,8,0,0,1-16,0V160a8,8,0,0,1,14.4-4.8L164,178.67l17.6-23.47A8,8,0,0,1,196,160Zm56,48a8,8,0,0,1-8,8H216a8,8,0,0,1-8-8V160a8,8,0,0,1,16,0v40h20A8,8,0,0,1,252,208Z",
);

export const TextNodeIcon = filled(
  "0 0 15 15",
  "M3.94993 2.95002L3.94993 4.49998C3.94993 4.74851 3.74845 4.94998 3.49993 4.94998C3.2514 4.94998 3.04993 4.74851 3.04993 4.49998V2.50004C3.04993 2.45246 3.05731 2.40661 3.07099 2.36357C3.12878 2.18175 3.29897 2.05002 3.49993 2.05002H11.4999C11.6553 2.05002 11.7922 2.12872 11.8731 2.24842C11.9216 2.32024 11.9499 2.40682 11.9499 2.50002L11.9499 2.50004V4.49998C11.9499 4.74851 11.7485 4.94998 11.4999 4.94998C11.2514 4.94998 11.0499 4.74851 11.0499 4.49998V2.95002H8.04993V12.05H9.25428C9.50281 12.05 9.70428 12.2515 9.70428 12.5C9.70428 12.7486 9.50281 12.95 9.25428 12.95H5.75428C5.50575 12.95 5.30428 12.7486 5.30428 12.5C5.30428 12.2515 5.50575 12.05 5.75428 12.05H6.94993V2.95002H3.94993Z",
  true,
);

export const WidgetNodeIcon = filled(
  "0 0 512 512",
  "M0 416c0 17.7 14.3 32 32 32l54.7 0c12.3 28.3 40.5 48 73.3 48s61-19.7 73.3-48L480 448c17.7 0 32-14.3 32-32s-14.3-32-32-32l-246.7 0c-12.3-28.3-40.5-48-73.3-48s-61 19.7-73.3 48L32 384c-17.7 0-32 14.3-32 32zm128 0a32 32 0 1 1 64 0 32 32 0 1 1 -64 0zM320 256a32 32 0 1 1 64 0 32 32 0 1 1 -64 0zm32-80c-32.8 0-61 19.7-73.3 48L32 224c-17.7 0-32 14.3-32 32s14.3 32 32 32l246.7 0c12.3 28.3 40.5 48 73.3 48s61-19.7 73.3-48l54.7 0c17.7 0 32-14.3 32-32s-14.3-32-32-32l-54.7 0c-12.3-28.3-40.5-48-73.3-48zM192 128a32 32 0 1 1 0-64 32 32 0 1 1 0 64zm73.3-64C253 35.7 224.8 16 192 16s-61 19.7-73.3 48L32 64C14.3 64 0 78.3 0 96s14.3 32 32 32l86.7 0c12.3 28.3 40.5 48 73.3 48s61-19.7 73.3-48L480 128c17.7 0 32-14.3 32-32s-14.3-32-32-32L265.3 64z",
);

/** The canvas itself: connected nodes — the sidebar's Canvases mark
 *  (GlobalSidebar draws this same component). */
export function CanvasIcon({ size = 12, className }: GlyphProps) {
  return (
    <svg
      className={"node-type-icon" + (className ? ` ${className}` : "")}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="6" cy="6" r="3" />
      <circle cx="18" cy="10" r="3" />
      <circle cx="9" cy="18" r="3" />
      <path d="M8.8 7.1 15.2 9M7.9 15.4 6.8 8.9M11.6 16.6l4.2-4.4" />
    </svg>
  );
}

const NODE_ICONS: Record<string, (p: GlyphProps) => JSX.Element> = {
  py: PythonNodeIcon,
  html: HtmlNodeIcon,
  htm: HtmlNodeIcon,
  md: TextNodeIcon,
  json: WidgetNodeIcon,
};

/** The icon for a canvas node, by its file (`hex.py`, `dashboard.html`). */
export function nodeTypeIcon(file: string): JSX.Element {
  const dot = file.lastIndexOf(".");
  const Icon = dot > 0 ? NODE_ICONS[file.slice(dot + 1).toLowerCase()] : undefined;
  return Icon ? <Icon /> : iconForEntry(file, false);
}
