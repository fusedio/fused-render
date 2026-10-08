import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from playwright.sync_api import sync_playwright
t0 = time.time()
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(SCR + "/profiles/d-1", channel="chrome", headless=False,
                                               args=["--window-size=900,700", "--window-position=50,50"], viewport=None)
    print("launch s", round(time.time() - t0, 2))
    page = ctx.pages[0]
    page.goto(URL)
    page.focus("#t")
    t = time.time()
    page.keyboard.type(S)
    page.keyboard.press("Enter")
    v = val(page)
    print("T1 typed ms", round((time.time() - t) * 1000))
    print("exact:", v == S + "\n", repr(v))
    for _ in range(3):
        page.keyboard.press("ArrowLeft")
    page.keyboard.press("Backspace")
    print("after", repr(val(page)))
    page.keyboard.press("Meta+a")
    page.keyboard.type("q")
    print("selectall", repr(val(page)))
    print("webdriver", page.evaluate("navigator.webdriver"))
    page.bring_to_front()
    page.focus("#t")
    print("osa", osa_type("human.text's,here"))
    time.sleep(0.5)
    print("after human", repr(val(page)))
    page.screenshot(path=OUT + "/t2_view.png")
    subprocess.run(["screencapture", "-x", OUT + "/t2_screen.png"])
    ctx.close()
