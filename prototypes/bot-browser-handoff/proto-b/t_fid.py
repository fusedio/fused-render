import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env import *
import urllib.parse
R = f"{S}/results/B"
LOG = open(f"{R}/raw_fidelity.txt", "w")
def out(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); LOG.write(s + "\n"); LOG.flush()
def res(name, ok, detail=""): out(f"RESULT {name}: {'PASS' if ok else 'FAIL'} {detail}")
e = Env("BF")
def click_el(sel):
    r = e.ws.evaluate(f"(()=>{{const r=document.querySelector({json.dumps(sel)}).getBoundingClientRect();return [(r.left+r.width/2)/innerWidth,(r.top+r.height/2)/innerHeight]}})()")
    e.h_click_canvas(r[0], r[1]); time.sleep(.15)
def page(html): e.goto("data:text/html;charset=utf-8," + urllib.parse.quote(html))
try:
    e.goto("about:blank"); e.open_liveview(); e.take_over()
    # contenteditable
    page("<body><div id=ce contenteditable style='border:1px solid;min-height:100px;font:20px sans-serif'></div>")
    click_el("#ce"); s = """Hello, World! it's a "test" (1+1=2) {x} [y] <z> a.b;c-d/e 日本 é"""
    e.h_type(s); e.h_key("Enter"); e.h_type("line2"); e.h_key("Backspace"); e.h_key("Backspace")
    t = e.ws.evaluate("document.getElementById('ce').innerText")
    res("F1 contenteditable", t == s + "\nlin", repr(t))
    e.h_key("a", "KeyA", meta=True); e.h_key("Delete"); res("F1b contenteditable cmd-A + Delete", e.ws.evaluate("document.getElementById('ce').innerText.trim()") == "")
    e.h_shot(f"{R}/F1_contenteditable.png")
    # login-shaped form
    page("<body><form id=f onsubmit=\"event.preventDefault();document.title=JSON.stringify([u.value,p.value,document.activeElement.id])\"><input id=u name=u autocomplete=username><br><input id=p type=password name=p autocomplete=current-password><br><button id=b>Sign in</button></form>")
    click_el("#u"); e.h_type("alice+test@example.com"); e.h_key("Tab"); e.h_type("P@ss w0rd!#\\'\"é"); e.h_key("Enter"); time.sleep(.2)
    t = e.ws.evaluate("document.title"); want = json.dumps(["alice+test@example.com", "P@ss w0rd!#\\'\"é", "p"], separators=(",", ":"), ensure_ascii=False)
    res("F2 login form: type, Tab, password, Enter submits", t == want, t)
    e.ws.evaluate("document.title=''"); click_el("#b"); time.sleep(.2); res("F2b click submit button via forwarded mouse", e.ws.evaluate("document.title") != "")
    e.h_shot(f"{R}/F2_login.png")
    # search-like with autocomplete (local, deterministic): datalist + keyup-driven suggestion list
    page("<body><input id=q autocomplete=off style='font:20px sans-serif;width:400px'><ul id=s></ul><script>q.addEventListener('input',()=>{s.innerHTML=q.value?['a','b','c'].map(x=>'<li>'+q.value+' '+x+'</li>').join(''):''});q.addEventListener('keydown',ev=>{window.K=(window.K||[]).concat(ev.key+':'+ev.keyCode)})</script>")
    click_el("#q"); e.h_type("fused render"); time.sleep(.2)
    n = e.ws.evaluate("document.querySelectorAll('#s li').length"); res("F3a local autocomplete (input events fire per key)", n == 3, f"suggestions={n} keys={e.ws.evaluate('K.length')}")
    # real DuckDuckGo
    try:
        e.goto("https://duckduckgo.com/"); time.sleep(2.5)
        sel = "[name=q][role=combobox]"; ok = e.ws.evaluate(f"!!document.querySelector('{sel}')")
        out("ddg loaded:", ok, e.ws.evaluate("location.href"))
        if ok:
            click_el(sel); e.h_type("fused render"); time.sleep(1.5)
            v = e.ws.evaluate(f"document.querySelector('{sel}').value")
            sug = e.ws.evaluate("document.querySelectorAll('[role=option],[role=listbox] li,[class*=suggest] li').length")
            res("F3b DuckDuckGo: typed value + autocomplete dropdown", v == "fused render" and sug > 0, f"value={v!r} suggestion_nodes={sug}")
            e.h_shot(f"{R}/F3_ddg_live.png")
            e.h_key("ArrowDown"); e.h_key("ArrowDown"); e.h_key("Enter"); time.sleep(2.5)
            res("F3c DuckDuckGo: ArrowDown x2 + Enter navigates to results", "q=" in e.ws.evaluate("location.href") or "/?" in e.ws.evaluate("location.href"), e.ws.evaluate("location.href")[:100])
            e.h_shot(f"{R}/F3_ddg_results.png")
    except Exception as ex:
        res("F3b DuckDuckGo", False, f"{type(ex).__name__}: {ex}")
    # non-US char forms, via both paths on <input>
    page("<body><input id=q style='font:20px'>")
    click_el("#q"); e.h_key("é", "KeyE"); e.h_key("日", ""); e.h_key("本", ""); e.h_key("ñ", "KeyN"); e.h_key("€", "Digit2", alt=True)
    v = e.ws.evaluate("q.value"); res("F4 non-US chars (é 日 本 ñ alt-€) via human keydown", v == "é日本ñ€", repr(v))
    # keydown/keyup sequence seen by the page for punctuation (the original bug)
    page("<body><input id=q><script>window.L=[];for(const t of ['keydown','keyup'])q.addEventListener(t,ev=>L.push(t[3]+':'+ev.key+'/'+ev.code+'/'+ev.keyCode))</script>")
    click_el("#q"); e.h_type(".'"); L = e.ws.evaluate("L.join(' ')")
    out("page-visible events for '.' then \"'\":", L)
    res("F5 page sees correct keyCode for . and ' (190, 222; no Delete/ArrowRight)", "d:./Period/190" in L and "d:'/Quote/222" in L, L)
    e.h_shot(f"{R}/F5_final.png")
finally:
    e.close()
