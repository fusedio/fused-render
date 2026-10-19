// Pins vite.config.js's `build.target`. Below es2021 esbuild lowers `||=` and
// breaks xterm's InputHandler.requestMode (see the comment in the config), which
// freezes the terminal drawer on the first DECRQM query. The config is parsed
// as text rather than imported: importing it would pull in the react and
// tailwind vite plugins just to read one string.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { transform } from "esbuild";

const config = readFileSync(new URL("../../../vite.config.js", import.meta.url), "utf8");

// The shape of xterm 6.0.0's minified requestMode: a `let` holding an enum
// object that is built through `||=`.
const SNIPPET =
  'export class A{requestMode(e){let r;(P=>(P[P.SET=1]="SET"))(r||={});let n=this.x;return n+e}}';

test("vite build.target keeps xterm's `let r; r||={}` intact when minified", async () => {
  const m = /\n\s*target:\s*"([^"]+)"/.exec(config);
  expect(m).not.toBeNull();
  const out = await transform(SNIPPET, {
    minify: true,
    format: "esm",
    target: m![1],
  });
  expect(out.code).toContain("||={}");
  expect(out.code).not.toContain("void 0||(");
});
