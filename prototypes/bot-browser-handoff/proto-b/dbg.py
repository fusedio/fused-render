import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env import *
e = Env("BF")
try:
    e.goto("https://duckduckgo.com/"); time.sleep(4)
    print(e.ws.evaluate("location.href + ' | ' + document.title + ' | ' + document.body.innerText.slice(0,200).replace(/\\n/g,' / ')"))
    print(e.ws.evaluate("JSON.stringify([...document.querySelectorAll('input,textarea')].map(i=>[i.id,i.name,i.type,i.getAttribute('role')]))"))
    print(e.ws.evaluate("navigator.userAgent"))
finally: e.close()
