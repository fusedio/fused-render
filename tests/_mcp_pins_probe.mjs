// Runs the MCP panel's OWN pin-typing functions (SPEC §44 / MC-3).
//
// The three functions that decide what a pin's VALUE is — `pinKind`, `seedPin`,
// `coercePin` — are pure, and they are the whole of the "a pinned `False` must
// not become the string \"False\"" contract. A source assertion could say they
// exist; only running them can say a boolean default seeds a boolean.
//
// The panel's script cannot be evaluated whole (its top level touches
// `document` and `window.fused` and starts a load), so this slices out those
// three declarations by brace matching and evaluates just them — the same
// posture as the other template probes in this directory.
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

const source = ["pinKind", "seedPin", "coercePin"].map(slice).join("\n");
const run = new Function(
  source + "\nreturn { pinKind, seedPin, coercePin };"
)();

const out = JSON.parse(casesJson).map((c) => {
  if (c.fn === "pinKind") return run.pinKind(c.param);
  if (c.fn === "seedPin") return run.seedPin(c.param);
  return run.coercePin(c.kind, c.value);
});
process.stdout.write(JSON.stringify(out));
