"""Correct CDP key forwarder (table ported from Puppeteer USKeyboardLayout).
keyparams(...) -> {"down": params, "up": params}  or  {"insert": text}
mac = the HUMAN's keyboard is mac (cmd is the shortcut modifier); the TARGET Chrome is on macOS either way,
so editing shortcuts must be sent as explicit `commands` (Blink does not derive them from raw keys over CDP).
"""
T = {}  # code -> (keyCode, key, shifted)
for i, s in enumerate(")!@#$%^&*("): T[f"Digit{i}"] = (48 + i, str(i), s)
for i in range(26): T[f"Key{chr(65+i)}"] = (65 + i, chr(97 + i), chr(65 + i))
for code, kc, k, s in [("Minus",189,"-","_"),("Equal",187,"=","+"),("BracketLeft",219,"[","{"),("BracketRight",221,"]","}"),
        ("Backslash",220,"\\","|"),("Semicolon",186,";",":"),("Quote",222,"'",'"'),("Backquote",192,"`","~"),
        ("Comma",188,",","<"),("Period",190,".",">"),("Slash",191,"/","?"),("Space",32," "," ")]:
    T[code] = (kc, k, s)
NAMED = {"Enter":13,"Tab":9,"Backspace":8,"Delete":46,"Escape":27,"ArrowLeft":37,"ArrowUp":38,"ArrowRight":39,"ArrowDown":40,
         "Home":36,"End":35,"PageUp":33,"PageDown":34,"Insert":45,"Shift":16,"Control":17,"Alt":18,"Meta":91,"CapsLock":20}
CHAR2CODE = {}
for c, (kc, k, s) in T.items():
    CHAR2CODE[k] = (c, False); CHAR2CODE[s] = (c, True)
CHAR2CODE[" "] = ("Space", False)
MAC_CMDS = {"a":"selectAll","c":"copy","x":"cut","z":"undo","v":"paste"}
ARROW_PRIM = {"ArrowLeft":"moveToBeginningOfLine","ArrowRight":"moveToEndOfLine","ArrowUp":"moveToBeginningOfDocument","ArrowDown":"moveToEndOfDocument"}
ARROW_WORD = {"ArrowLeft":"moveWordLeft","ArrowRight":"moveWordRight","ArrowUp":"moveToBeginningOfParagraph","ArrowDown":"moveToEndOfParagraph"}

def keyparams(key, code="", shift=False, ctrl=False, alt=False, meta=False, keyCode=None, mac=True):
    mods = (1 if alt else 0) | (2 if ctrl else 0) | (4 if meta else 0) | (8 if shift else 0)
    prim = meta if mac else ctrl
    wordmod = alt if mac else ctrl
    base = {"key": key, "code": code, "modifiers": mods}
    def mk(vk, text=None, commands=None):
        d = dict(base)
        if vk: d["windowsVirtualKeyCode"] = vk  # NEVER set nativeVirtualKeyCode: on macOS native 16 (Shift's win vk) HIDES the tab + kills screencast
        dn = dict(d, type="keyDown" if text else "rawKeyDown")
        if text: dn["text"] = text; dn["unmodifiedText"] = text
        if commands: dn["commands"] = commands
        return {"down": dn, "up": dict(d, type="keyUp")}
    ms = "AndModifySelection" if shift else ""
    if len(key) > 1:
        vk = keyCode or NAMED.get(key) or 0
        cmds = []
        if key in ARROW_PRIM and prim: cmds = [ARROW_PRIM[key] + ms]
        elif key in ARROW_WORD and wordmod: cmds = [ARROW_WORD[key] + ms]
        elif key == "Backspace" and prim: cmds = ["deleteToBeginningOfLine"]
        elif key == "Backspace" and wordmod: cmds = ["deleteWordBackward"]
        elif key == "Delete" and wordmod: cmds = ["deleteWordForward"]
        elif key in ("Home", "End"): cmds = ["moveTo%sOfLine" % ("Beginning" if key == "Home" else "End") + ms]
        return mk(vk, "\r" if key == "Enter" else None, cmds)
    c = code if code in T else CHAR2CODE.get(key, ("", 0))[0]
    if (ctrl or meta) and not (ctrl and alt):  # shortcut chord: no text
        vk = T[c][0] if c in T else (keyCode or 0)
        cmds = []
        if prim and key.lower() in MAC_CMDS and not (alt and mac):
            cmds = ["redo"] if (key.lower() == "z" and shift) else [MAC_CMDS[key.lower()]]
        return mk(vk, None, cmds)
    if c in T and key in (T[c][1], T[c][2]):
        return mk(T[c][0], key)
    return {"insert": key}  # é, 日, emoji, non-US-layout char
