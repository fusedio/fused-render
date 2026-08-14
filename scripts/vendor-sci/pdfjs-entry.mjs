// Bundled into fused_render/templates/vendor/pdfjs.bundle.mjs. pdf/template.html
// does `import * as pdfjs from "…"` and uses getDocument/GlobalWorkerOptions.
// The worker is a SEPARATE bundle (pdfjs.worker.bundle.mjs) — pdf.js requires it
// as its own module; the template points GlobalWorkerOptions.workerSrc at it.
export * from "pdfjs-dist";
