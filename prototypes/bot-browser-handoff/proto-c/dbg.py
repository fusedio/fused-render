import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from relay import Relay
kill_c(); time.sleep(1)
relay = Relay()
sess, ext_id, ok, dt = launch_bridge("C-dbg", relay=relay)
w = relay.call("createWindow", url="about:blank", focused=False, width=600, height=400)
A = w["tabId"]; relay.call("attach", tabId=A)
for url in ["data:text/html,<textarea id=t></textarea>", "http://example.com/", "about:blank"]:
    try: r = relay.cdp(A, "Page.navigate", url=url)
    except Exception as e: r = str(e)
    time.sleep(1.5)
    print(url, "->", r, "| href:", relay.evaluate(A, "location.href"), "| tab:", relay.call("getTab", tabId=A).get("url"))
print("raw eval:", relay.cdp(A, "Runtime.evaluate", expression="1+1", returnByValue=True))
print([e for e in relay.events if e.get("type") != "event"][:5])
cdp.kill(sess); relay.close()
