// Bundled into fused_render/templates/vendor/pdfjs.worker.bundle.mjs — the
// pdf.js worker as a self-contained module Worker. Side-effect import: the
// worker module wires its own onmessage handlers when evaluated.
import "pdfjs-dist/build/pdf.worker.mjs";
