# Bring your own framework: React, Vite, TypeScript

A fused-render app is a folder with an `index.html`. The server renders that
page with `window.fused` injected ahead of your code, and everything the
authoring skill documents (`fused.runPython`, `fused.params`, `fused.ai`, file
IO) is there with no import and no build. Plain HTML, CSS and JavaScript is
the default and the fastest way to a first view.

If you would rather write the page as a React component tree, in TypeScript,
with a bundler, this guide is the recipe. A complete app to copy lives in
[`examples/react-vite/`](../examples/react-vite/).

## The two facts the recipe rests on

1. **The page is served at `/render?path=<absolute index.html>`, not from its
   folder.** A bare `<script src="./assets/index.js">` resolves against the
   `/render` URL and 404s, silently. Files beside the page are reached through
   `fused.rawUrl(path)`, which returns a URL the server serves bytes from with
   the right MIME type, and that works for a `<link>`, an `<img>`, and a
   dynamic `import()`.
2. **`window.fused` exists before any script you write runs**, because the
   runtime is injected right after `<head>`. So the page can ask it for those
   URLs at load time.

Everything else follows: build to a known file name, load it via `rawUrl`.

## 1. Build one file with a fixed name

Vite normally hashes output names and splits chunks. Both get in the way
here: a hashed name cannot be loaded by a hand-written `index.html`, and a
relative `import("./chunk-abc.js")` inside the bundle would resolve against
the raw-file URL, not the folder. So:

```ts
// vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    target: "es2020",
    rollupOptions: {
      input: "src/main.tsx",
      output: {
        format: "es",
        entryFileNames: "app.js",        // dist/app.js
        assetFileNames: "app.[ext]",     // dist/app.css
        inlineDynamicImports: true,      // no chunks
      },
    },
  },
});
```

The entry is `src/main.tsx`, not `index.html`: Vite is building a module, and
the page that loads it is yours.

## 2. Hand-write the page

```html
<!DOCTYPE html>
<html data-fused-theme="shell">
<head>
<meta charset="utf-8" />
<meta name="fused-app" />
<meta name="fused-api-version" content="1" />
<title>My app</title>
</head>
<body>
  <div id="root"></div>
  <script>
    var css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = fused.rawUrl("dist/app.css");
    document.head.appendChild(css);
    import(fused.rawUrl("dist/app.js"));
  </script>
</body>
</html>
```

Keep the two `<meta>` tags near the top: `fused-app` is the only thing that
makes the folder an app, and detection reads the first 4 KiB. The
`data-fused-theme="shell"` attribute lets the shell carry light/dark into the
page; the theming skill has the CSS variables to use.

## 3. Type `fused`

A running fused-render serves the declarations at
`http://127.0.0.1:1777/static/fused.d.ts` (in the repo:
[`fused_render/static/fused.d.ts`](../fused_render/static/fused.d.ts)). It is a
script-style declaration file, no import or export, so either of these works:

```jsonc
// tsconfig.json
{ "include": ["src", "fused.d.ts"] }
```

```js
/// <reference path="./fused.d.ts" />   // at the top of a plain .js file
```

Then `fused.runPython<MyResult>("./main.py", { n })` is typed, `fused.params`
autocompletes, and `fused.ai.text({ prompt })` tells you what comes back. The
file is kept in step with the runtime by `tests/test_runtime_types.py`.

## 4. Params are your state, the URL is your router

`fused.params` is a string key/value store that the shell keeps in sync with
the URL. Write it from controls, re-render on `onChange`, read it when you
draw: a refresh or a bookmark reproduces the view, and sharing a view is
copying a link. A hook is a few lines:

```tsx
function useParam(key: string, fallback: string): [string, (v: string) => void] {
  const read = () => fused.params.get(key) ?? fallback;
  const [value, setValue] = useState(read);
  useEffect(() => fused.params.onChange(() => setValue(read())), [key]);
  return [value, (v) => fused.params.set(key, v)];
}
```

Values are strings; `set("n", 5)` throws, `set("n", String(5))` is right.
Keys starting with `_` belong to the shell. Do not call `history.pushState`
yourself: the shell's own keys ride on the same URL.

## 5. Compute in Python

`fused.runPython("./main.py", params)` runs `main(**params)` in a fresh
subprocess beside the page and resolves with its JSON-native return value.
Annotate the parameters and they are coerced from the URL strings:

```python
def main(limit: int = 30) -> dict:
    ...
```

A new call for the same `.py` aborts the one in flight, so a dragged slider
never renders a stale answer; the superseded promise rejects with an
`AbortError` you can ignore. A folder declares third-party dependencies once,
in its `pyproject.toml`; the first render installs them.

## 6. The dev loop

```
npm run watch          # vite build --watch → dist/app.js, dist/app.css
fused-render open .    # serves this folder on an ephemeral port and opens it
```

Or drop the folder under `~/Fused/app/` and open it from Home. After a
rebuild, refresh the tab: auto-reload watches the page and the Python it
calls, not the bundle. Open the browser console for runtime errors; the
`fused-render calls --page <abs index.html>` command shows every
`runPython` call the page made, with tracebacks.

## What stays the same, and what to know

- **Everything on `fused` works unchanged** from React: `fused.ai.*` for local
  models, `fused.tasks` for Claude Code, `fused.bots`, `fused.capture`,
  `fused.readFile`/`writeFile`, jobs. Call them from effects and handlers as
  you would any promise-returning API.
- **Images and fonts** referenced from CSS or JSX: either inline them at build
  time (Vite's `?inline` / `assetsInlineLimit`) or address them with
  `fused.rawUrl("assets/logo.png")` at runtime. A build-time relative URL
  resolves against the raw-file URL and 404s.
- **Cross-browser**: views open in the user's default browser and in the
  macOS WKWebView popover. Target `es2020`, stay on Baseline features, and
  read the cross-browser skill before shipping.
- **Export**: a page that only uses the portable subset (`runPython`,
  `params`, `rawUrl`, `readFile`) exports to a hosted bundle. The exporter
  discovers `rawUrl` targets from string literals, so keep
  `fused.rawUrl("dist/app.js")` literal, as above. `fused.ai`, `tasks`, `bots`
  and `capture` are local-only; branch on `fused.env === "hosted"`.
- **Any framework**: nothing above is React-specific. Svelte, Vue, Solid or a
  plain `tsc` build fit the same two lines in `index.html`.
