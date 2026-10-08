import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from playwright.sync_api import sync_playwright
sess = json.load(open(OUT + "/m2_session.json"))
port = sess["port"]
with sync_playwright() as p:
    t = time.time()
    br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    ctx = br.contexts[0]
    print("T8 reattach ms", round((time.time() - t) * 1000), "pages", [(x.url[:25]) for x in ctx.pages])
    page = [x for x in ctx.pages if x.url.startswith("data:")][0]
    tid_now = [t for t in cdp.pages(port) if t["url"].startswith("data:")][0]["id"]
    print("same target id:", tid_now == sess["tid"])
    before = val(page)
    page.focus("#t"); page.keyboard.type(S[:10])
    print("T8 value before", repr(before), "after", repr(val(page)))
    print("chrome pid alive:", subprocess.run(["pgrep", "-f", f"remote-debugging-port={port}"], capture_output=True, text=True).stdout.split()[0] == str(sess["pid"]))
    # does closing the Browser handle kill chrome?
    br.close()
time.sleep(1)
try:
    print("after br.close(): chrome still up:", cdp.http(port, "/json/version")["Browser"])
except Exception as e:
    print("after br.close(): chrome DEAD", type(e).__name__)
