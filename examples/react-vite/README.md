# React + Vite on fused-render

A fused-render app whose page is a React component tree. Vite builds it into
one file; `index.html` loads that file through the injected `fused` runtime;
`main.py` does the computing. The URL carries the state.

```
npm install
npm run build        # or: npm run watch
```

Then open this folder in fused-render (drop it under `~/Fused/app/`, or
`fused-render open examples/react-vite`). Move the slider: the page calls
`fused.runPython("./main.py", { limit })`, and `?limit=…` lands in the URL so
a refresh or a bookmark reproduces the view.

What to copy into your own app:

- `vite.config.ts` — one entry, fixed output names, no chunks.
- `index.html` — the two-line loader that fetches `dist/app.css` and
  `dist/app.js` via `fused.rawUrl()`.
- `tsconfig.json` — includes `fused_render/static/fused.d.ts`, so `fused.*`
  is typed. Outside this repository, download the file from a running
  fused-render at `http://127.0.0.1:1777/static/fused.d.ts`.

The full guide, with the reasons behind each line, is
[docs/FRAMEWORKS.md](../../docs/FRAMEWORKS.md).
