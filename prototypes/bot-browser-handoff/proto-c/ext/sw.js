// Relay: ws://127.0.0.1:17777  <->  chrome.debugger
const URL_ = "ws://127.0.0.1:17777/bridge";
let ws = null;
const attached = new Set();
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
function connect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  try { ws = new WebSocket(URL_); } catch (e) { return; }
  ws.onopen = async () => {
    const tabs = await chrome.tabs.query({});
    send({type: "hello", ext: chrome.runtime.id, attached: [...attached],
          tabs: tabs.map(t => ({id: t.id, url: t.url, windowId: t.windowId, active: t.active}))});
  };
  ws.onclose = () => { ws = null; setTimeout(connect, 500); };
  ws.onerror = () => {};
  ws.onmessage = async (ev) => {
    const m = JSON.parse(ev.data);
    try { send({id: m.id, result: await handle(m)}); }
    catch (e) { send({id: m.id, error: String(e && e.message || e)}); }
  };
}
async function handle(m) {
  const t = m.tabId;
  switch (m.cmd) {
    case "ping": return {pong: Date.now()};
    case "attach": await chrome.debugger.attach({tabId: t}, "1.3"); attached.add(t); return {};
    case "detach": attached.delete(t); await chrome.debugger.detach({tabId: t}); return {};
    case "cdp": return await chrome.debugger.sendCommand({tabId: t}, m.method, m.params || {});
    case "createTab": { const tab = await chrome.tabs.create({url: m.url || "about:blank", active: !!m.active, windowId: m.windowId}); return {tabId: tab.id, windowId: tab.windowId}; }
    case "createWindow": { const w = await chrome.windows.create({url: m.url || "about:blank", focused: !!m.focused, state: m.state || "normal", width: m.width || 900, height: m.height || 700, left: m.left, top: m.top}); return {windowId: w.id, tabId: w.tabs[0].id}; }
    case "activate": { const tab = await chrome.tabs.update(t, {active: true}); await chrome.windows.update(tab.windowId, {focused: true, state: "normal"}); return {windowId: tab.windowId}; }
    case "windowState": { await chrome.windows.update(m.windowId, m.update); return {}; }
    case "getTab": return await chrome.tabs.get(t);
    case "listTabs": return (await chrome.tabs.query({})).map(x => ({id: x.id, url: x.url, windowId: x.windowId, active: x.active}));
    case "closeTab": await chrome.tabs.remove(t); return {};
    case "targets": return await chrome.debugger.getTargets();
    default: throw new Error("bad cmd " + m.cmd);
  }
}
chrome.debugger.onEvent.addListener((src, method, params) => send({type: "event", tabId: src.tabId, method, params}));
chrome.debugger.onDetach.addListener((src, reason) => { attached.delete(src.tabId); send({type: "detached", tabId: src.tabId, reason}); });
chrome.tabs.onRemoved.addListener((tabId) => send({type: "tabRemoved", tabId}));
chrome.alarms.create("keepalive", {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
