// Server API wrappers. Non-ok responses throw with the server's error message.
async function getJson(url) {
  const res = await fetch(url);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

export function getConfig() {
  return getJson("/api/config");
}

export function listDir(fsPath) {
  return getJson("/api/fs/list?path=" + encodeURIComponent(fsPath));
}

export function statPath(fsPath) {
  return getJson("/api/fs/stat?path=" + encodeURIComponent(fsPath));
}

export function rawUrl(fsPath) {
  return "/api/fs/raw?path=" + encodeURIComponent(fsPath);
}
