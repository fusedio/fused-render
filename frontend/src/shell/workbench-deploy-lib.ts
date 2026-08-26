export function canvasNameForApp(name: string): string {
  return (
    name.replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 128) ||
    "Fused_App"
  );
}
