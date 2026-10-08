// JS port of keys.py (same table + logic). Works in browser (window.keyparams) and node (module.exports).
(function (root) {
  const T = {};
  ")!@#$%^&*(".split("").forEach((s, i) => (T["Digit" + i] = [48 + i, String(i), s]));
  for (let i = 0; i < 26; i++) T["Key" + String.fromCharCode(65 + i)] = [65 + i, String.fromCharCode(97 + i), String.fromCharCode(65 + i)];
  [["Minus",189,"-","_"],["Equal",187,"=","+"],["BracketLeft",219,"[","{"],["BracketRight",221,"]","}"],
   ["Backslash",220,"\\","|"],["Semicolon",186,";",":"],["Quote",222,"'",'"'],["Backquote",192,"`","~"],
   ["Comma",188,",","<"],["Period",190,".",">"],["Slash",191,"/","?"],["Space",32," "," "]].forEach(([c,k,a,b]) => (T[c] = [k,a,b]));
  const NAMED = {Enter:13,Tab:9,Backspace:8,Delete:46,Escape:27,ArrowLeft:37,ArrowUp:38,ArrowRight:39,ArrowDown:40,Home:36,End:35,PageUp:33,PageDown:34,Insert:45,Shift:16,Control:17,Alt:18,Meta:91,CapsLock:20};
  const CHAR2CODE = {};
  for (const [c, [, k, s]] of Object.entries(T)) { CHAR2CODE[k] = [c, false]; CHAR2CODE[s] = [c, true]; }
  CHAR2CODE[" "] = ["Space", false];
  const MAC_CMDS = {a:"selectAll", c:"copy", x:"cut", z:"undo", v:"paste"};
  const ARROW_PRIM = {ArrowLeft:"moveToBeginningOfLine", ArrowRight:"moveToEndOfLine", ArrowUp:"moveToBeginningOfDocument", ArrowDown:"moveToEndOfDocument"};
  const ARROW_WORD = {ArrowLeft:"moveWordLeft", ArrowRight:"moveWordRight", ArrowUp:"moveToBeginningOfParagraph", ArrowDown:"moveToEndOfParagraph"};

  function keyparams(key, code, o) {
    o = o || {}; code = code || "";
    const shift = !!o.shift, ctrl = !!o.ctrl, alt = !!o.alt, meta = !!o.meta, mac = o.mac !== false, keyCode = o.keyCode || null;
    const mods = (alt ? 1 : 0) | (ctrl ? 2 : 0) | (meta ? 4 : 0) | (shift ? 8 : 0);
    const prim = mac ? meta : ctrl, wordmod = mac ? alt : ctrl;
    const base = {key, code, modifiers: mods};
    const mk = (vk, text, commands) => {
      const d = Object.assign({}, base);
      if (vk) { d.windowsVirtualKeyCode = vk; /* never nativeVirtualKeyCode: hides tab on macOS */ }
      const dn = Object.assign({}, d, {type: text ? "keyDown" : "rawKeyDown"});
      if (text) { dn.text = text; dn.unmodifiedText = text; }
      if (commands && commands.length) dn.commands = commands;
      return {down: dn, up: Object.assign({}, d, {type: "keyUp"})};
    };
    const ms = shift ? "AndModifySelection" : "";
    if ([...key].length > 1) {
      const vk = keyCode || NAMED[key] || 0; let cmds = [];
      if (ARROW_PRIM[key] && prim) cmds = [ARROW_PRIM[key] + ms];
      else if (ARROW_WORD[key] && wordmod) cmds = [ARROW_WORD[key] + ms];
      else if (key === "Backspace" && prim) cmds = ["deleteToBeginningOfLine"];
      else if (key === "Backspace" && wordmod) cmds = ["deleteWordBackward"];
      else if (key === "Delete" && wordmod) cmds = ["deleteWordForward"];
      else if (key === "Home" || key === "End") cmds = ["moveTo" + (key === "Home" ? "Beginning" : "End") + "OfLine" + ms];
      return mk(vk, key === "Enter" ? "\r" : null, cmds);
    }
    const c = (code in T) ? code : ((CHAR2CODE[key] || [""])[0]);
    if ((ctrl || meta) && !(ctrl && alt)) {
      const vk = (c in T) ? T[c][0] : (keyCode || 0); let cmds = [];
      const lk = key.toLowerCase();
      if (prim && MAC_CMDS[lk] && !(alt && mac)) cmds = [lk === "z" && shift ? "redo" : MAC_CMDS[lk]];
      return mk(vk, null, cmds);
    }
    if ((c in T) && (key === T[c][1] || key === T[c][2])) return mk(T[c][0], key);
    return {insert: key};
  }
  const api = {keyparams, T, CHAR2CODE};
  if (typeof module !== "undefined") module.exports = api; else Object.assign(root, api);
})(typeof window !== "undefined" ? window : globalThis);
