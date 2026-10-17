// Fixtures shared by netTiming.test.ts and netTiming-runtime.test.ts: the route
// rule exists three times (fused_render/netlog.py, netTiming.ts, runtime.js) and
// the client's queue stats only join the server's groups if all agree.
export const ROUTE_CASES: [string, string][] = [
  ["/api/fs/list?path=/x", "/api/fs/list"],
  ["/api/tasks/123/stream", "/api/tasks/{id}/stream"],
  ["/api/terminal/deadbeef0123/stream", "/api/terminal/{id}/stream"],
  ["/api/jobs/123e4567-e89b-12d3-a456-426614174000", "/api/jobs/{id}"],
  ["/api/x/" + "a".repeat(41), "/api/x/{id}"],
  ["/api/fs/facade", "/api/fs/facade"],
  ["/api/system/activity", "/api/system/activity"],
];
