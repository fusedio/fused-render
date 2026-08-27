// Runs the MCP panel's OWN pure functions (SPEC §44 / MC-8, MC-12).
//
// * The three functions that decide what a pin's VALUE is — `pinKind`,
//   `seedPin`, `coercePin` — are the whole of the "a pinned `False` must not
//   become the string \"False\"" contract (MC-8). A source assertion could
//   say they exist; only running them can say a boolean default seeds a
//   boolean.
// * `registrationDefinition` is the {command, args} shape written into the
//   user's GLOBAL ~/.claude.json on Register (MC-12) — the shape a Windows
//   "fix" would silently corrupt by routing `command` through a `cmd.exe`
//   hop (see its own comment in template.html, D549). Running it, not just
//   grepping the literal, is what makes that revert fail loudly regardless
//   of how it's phrased.
//
// The panel's script cannot be evaluated whole (its top level touches
// `document` and `window.fused` and starts a load), so this slices out those
// declarations by brace matching and evaluates just them — the same posture
// as the other template probes in this directory.
//
// Usage: node _mcp_pins_probe.mjs <template.html> '<json cases>'
import { readFileSync } from "node:fs";

const [templatePath, casesJson] = process.argv.slice(2);
const html = readFileSync(templatePath, "utf8");

function slice(name) {
  const head = `function ${name}(`;
  const start = html.indexOf(head);
  if (start < 0) throw new Error(`${name} is not in the template`);
  let depth = 0;
  for (let i = html.indexOf("{", start); i < html.length; i++) {
    if (html[i] === "{") depth++;
    else if (html[i] === "}") {
      depth--;
      if (depth === 0) return html.slice(start, i + 1);
    }
  }
  throw new Error(`${name} is unbalanced`);
}

const source = ["pinKind", "seedPin", "coercePin", "registrationDefinition"]
  .map(slice).join("\n");
const run = new Function(
  source + "\nreturn { pinKind, seedPin, coercePin, registrationDefinition };"
)();

const out = JSON.parse(casesJson).map((c) => {
  if (c.fn === "pinKind") return run.pinKind(c.param);
  if (c.fn === "seedPin") return run.seedPin(c.param);
  if (c.fn === "coercePin") return run.coercePin(c.kind, c.value);
  return run.registrationDefinition(c.fused, c.path);
});
process.stdout.write(JSON.stringify(out));
