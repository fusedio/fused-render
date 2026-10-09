// THE NO-POLL INVARIANT (Fused Events Bus, D3): no `setInterval` and no
// chained `setTimeout` in frontend/src or static/runtime.js wraps a fetch,
// except the health probe (ServerStatusBanner) and the pure UI timers listed
// by path below. A client that decides when to ask the server is the pool
// pressure the bus exists to remove; a live fact is a subscription.
//
// The check is lexical and deliberately simple: a file is a violation when
//   * it calls `setInterval(` and also calls a fetch-like function, or
//   * it schedules a NAMED function with `setTimeout(name` and that function's
//     own body calls a fetch-like function (the "poll again after the answer"
//     shape: `setTimeout(poll, ms)`, `setTimeout(tick, ms)`, `setTimeout(step`).
// Fetch-like: `fetch(`, `getJson(`, `postJson(`, `putJson(`, `request(`,
// `taskFetch(`, `envPost(`, `aiPost(`. Comments and strings are stripped
// first so prose about the old polls cannot trip it.
//
// Run via `npm run check:no-polls`; wired into `npm run build` beside
// check-boundaries.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src", import.meta.url));
const RUNTIME = fileURLToPath(new URL("../../fused_render/static/runtime.js", import.meta.url));

/**
 * Files allowed to keep a timer that fetches, with the reason. Every entry is
 * a decision, not a convenience: add one only with a sentence that says why
 * the bus cannot carry the fact.
 */
const ALLOW = new Map([
  // D13: the banner measures the one path the bus does not own — whether this
  // document can get an HTTP answer through the pool at all.
  ["platform/ui/ServerStatusBanner.tsx", "the health probe (D13)"],
]);

const FETCH_LIKE = /\b(fetch|getJson|postJson|putJson|request|taskFetch|envPost|aiPost)\s*(<[^>]*>)?\s*\(/;

const posix = (p) => p.split(path.sep).join("/");

function strip(text) {
  // Block comments, line comments, then string/template literals (kept as
  // empty quotes so the surrounding syntax still parses for the regexes).
  return text
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:\\])\/\/.*$/gm, "$1")
    .replace(/`(?:\\[\s\S]|[^`\\])*`/g, "``")
    .replace(/"(?:\\.|[^"\\\n])*"/g, '""')
    .replace(/'(?:\\.|[^'\\\n])*'/g, "''");
}

/** The body of the function named `name`, if one is declared in `code`. */
function bodyOf(code, name) {
  const decl = new RegExp(
    `(?:function\\s+${name}\\s*\\([^)]*\\)\\s*\\{|(?:const|let|var)\\s+${name}\\s*=\\s*(?:async\\s*)?(?:function\\s*\\([^)]*\\)|\\([^)]*\\)\\s*=>|[A-Za-z_$][\\w$]*\\s*=>)\\s*\\{?)`,
  );
  const m = decl.exec(code);
  if (!m) return null;
  let i = m.index + m[0].length;
  if (!m[0].trimEnd().endsWith("{")) return code.slice(i, code.indexOf("\n", i));
  let depth = 1;
  const start = i;
  for (; i < code.length && depth > 0; i++) {
    if (code[i] === "{") depth++;
    else if (code[i] === "}") depth--;
  }
  return code.slice(start, i);
}

function check(file, rel) {
  const code = strip(fs.readFileSync(file, "utf8"));
  const problems = [];
  if (/\bsetInterval\s*\(/.test(code) && FETCH_LIKE.test(code)) {
    problems.push("setInterval in a file that fetches");
  }
  for (const m of code.matchAll(/\bsetTimeout\s*\(\s*([A-Za-z_$][\w$]*)\s*,/g)) {
    const name = m[1];
    const body = bodyOf(code, name);
    if (body && FETCH_LIKE.test(body)) {
      problems.push(`setTimeout(${name}, …) re-arms a function that fetches`);
    }
  }
  return problems.map((p) => `${rel}: ${p}`);
}

const files = [];
(function walk(d) {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p);
    else if (/\.(tsx|ts)$/.test(e.name) && !/\.test\.(tsx|ts)$/.test(e.name)) files.push(p);
  }
})(SRC);

const violations = [];
for (const file of files) {
  const rel = posix(path.relative(SRC, file));
  if (ALLOW.has(rel)) continue;
  violations.push(...check(file, rel));
}
if (fs.existsSync(RUNTIME)) violations.push(...check(RUNTIME, "fused_render/static/runtime.js"));

if (violations.length) {
  console.error("Timer-driven fetches (the events bus carries live facts, D3):\n" + violations.map((v) => "  " + v).join("\n"));
  console.error(
    "\nSubscribe to the topic that would change the answer (platform/lib/events useTopic / fused.subscribe) " +
      "and re-ask a query on its change; a timer that fetches needs an ALLOW entry with its reason.",
  );
  process.exit(1);
}
console.log(`no-polls OK (${files.length + 1} files; allowed: ${[...ALLOW.keys()].join(", ")})`);
