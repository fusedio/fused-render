// Stub for Node built-ins that a dual browser/node dependency imports behind a
// runtime environment check. The check is false in a browser, so the import is
// never reached — but esbuild still has to resolve it to bundle the module.
export function createRequire() {
  throw new Error("Node built-ins are not available in the browser bundle");
}
export default {};
