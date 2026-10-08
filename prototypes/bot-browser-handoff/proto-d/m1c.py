import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    for ign in (None, ["--enable-automation"]):
        ctx = p.chromium.launch_persistent_context(SCR + "/profiles/d-3", channel="chrome", headless=False, viewport=None,
            ignore_default_args=ign, args=["--window-size=800,600"])
        out = subprocess.run(["pgrep", "-fl", "profiles/d-3"], capture_output=True, text=True).stdout.splitlines()
        main = [l for l in out if "--type=" not in l][0]
        print(ign, "has --enable-automation:", "--enable-automation" in main, "| pipe:", "--remote-debugging-pipe" in main)
        ctx.close()
