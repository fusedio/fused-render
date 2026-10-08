import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from playwright.sync_api import sync_playwright
mode = sys.argv[1]
if mode == "child":
    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(SCR + "/profiles/d-3", channel="chrome", headless=False, viewport=None,
                                               args=["--window-size=800,600"])
    print("webdriver default:", ctx.pages[0].evaluate("navigator.webdriver"), flush=True)
    ctx.close()
    ctx = p.chromium.launch_persistent_context(SCR + "/profiles/d-3", channel="chrome", headless=False, viewport=None,
                                               ignore_default_args=["--enable-automation"], args=["--window-size=800,600"])
    print("webdriver w/ ignore_default_args:", ctx.pages[0].evaluate("navigator.webdriver"), flush=True)
    print("CHILDPID", os.getpid(), flush=True)
    time.sleep(300)
else:
    import signal
    c = subprocess.Popen([sys.executable, "-u", __file__, "child"], stdout=subprocess.PIPE, text=True)
    for line in c.stdout:
        print(line.strip())
        if line.startswith("CHILDPID"):
            break
    def n():
        return len(subprocess.run(["pgrep", "-f", "profiles/d-3"], capture_output=True, text=True).stdout.split())
    print("chrome procs while python alive:", n())
    c.kill(); time.sleep(3)
    print("chrome procs after SIGKILL of python:", n())
    subprocess.run(["pkill", "-f", "profiles/d-3"])
