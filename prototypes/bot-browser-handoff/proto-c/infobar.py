"""Infobar presence via innerHeight delta (no screen-recording TCC needed), with/without --silent-debugger-extension-api.
Also: is chrome://extensions scriptable over CDP (path f), and can chrome.debugger touch chrome:// tabs."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from relay import Relay
kill_c(); time.sleep(1)
R = {}; BASE = serve_page()

def measure(prof, extra):
    relay = Relay()
    sess, ext_id, ok, dt = launch_bridge(prof, extra=extra, relay=relay)
    w = relay.call("createWindow", url=BASE, focused=False, width=800, height=600, left=60, top=60); t = w["tabId"]; time.sleep(1)
    # observer tab = SEPARATE window not attached by the extension, read via the port (infobar is browser-wide)
    w2 = relay.call("createWindow", url=BASE + "?h=observer", focused=False, width=800, height=600, left=900, top=60); t2 = w2["tabId"]; time.sleep(1)
    tid2 = next(x["id"] for x in relay.call("targets") if x.get("tabId") == t2)
    obs = HumanCDP(sess["port"], tid2)
    ih = lambda: obs.ws.evaluate("innerHeight")
    out = {"before_attach": ih()}
    relay.call("attach", tabId=t); time.sleep(1.0); out["attached"] = ih()
    relay.call("detach", tabId=t); time.sleep(1.0); out["detached"] = ih()
    relay.call("attach", tabId=t); time.sleep(1.0); out["reattached"] = ih()
    # chrome:// tab
    ce = relay.call("createTab", windowId=w2["windowId"], url="chrome://extensions", active=True); time.sleep(1.5)
    try: relay.call("attach", tabId=ce["tabId"]); out["debugger_attach_chrome_url"] = "ok"
    except Exception as e: out["debugger_attach_chrome_url"] = str(e)
    # path (f): drive chrome://extensions through the PORT (not the extension)
    cid = next(x["id"] for x in cdp.http(sess["port"], "/json/list") if x["url"].startswith("chrome://extensions"))
    h = HumanCDP(sess["port"], cid)
    out["chrome_ext_page_devPrivate"] = h.ws.evaluate("typeof chrome.developerPrivate")
    out["devmode_toggle"] = h.ws.evaluate("chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode:true}).then(()=>chrome.developerPrivate.getProfileConfiguration()).then(c=>c.inDeveloperMode).catch(e=>'ERR '+e)")
    out["ext_list_via_page"] = h.ws.evaluate("chrome.developerPrivate.getExtensionsInfo().then(l=>l.map(e=>[e.id,e.location,e.state]))")
    h.close(); obs.close(); cdp.kill(sess); relay.close(); time.sleep(1)
    return out

R["default"] = measure("C-ib1", [])
R["silent_flag"] = measure("C-ib2", ["--silent-debugger-extension-api"])
json.dump(R, open(os.path.join(OUT, "infobar.json"), "w"), indent=1)
print(json.dumps(R, indent=1))
