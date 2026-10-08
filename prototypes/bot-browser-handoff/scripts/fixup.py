import sys
p = sys.argv[1]
S = "/private/tmp/claude-501/-Users-vasu-Documents-fused-dev-fused-render/acb7c266-9bd8-4dc6-a57d-56112293155a/scratchpad"
s = open(p).read()
start = s.index("## Q2 addendum: MEASURED")
end = s.index("## Q4.")
block = s[start:end]
s = s[:start] + s[end:]
block = block.replace("## Q2 addendum: MEASURED key behaviour (Chrome 155, macOS, script scratchpad/scripts/keys_test.py, input value starts \"abc\")",
                      "### MEASURED key behaviour (Chrome 155, macOS, input value starts \"abc\"; script " + S + "/scripts/keys_test.py)")
anchor = s.index("## Q3. Chrome platform constraints")
s = s[:anchor] + block.rstrip() + "\n\n" + s[anchor:]


def rep(a, b):
    global s
    assert a in s, a
    s = s.replace(a, b)


rep("scripts in scratchpad/scripts/)", "scripts in " + S + "/scripts/, run with " + S + "/venv/bin/python -I <script>)")
rep("Script: scratchpad/scripts/minimize_test.py.", "Script: " + S + "/scripts/minimize_test.py.")
rep("(scratchpad/scripts/infobar_test.py, Chrome 155 headed)", "(" + S + "/scripts/infobar_test.py, Chrome 155 headed)")
rep("- Nobody serious relaunches the browser for take-over.", "- None of the surveyed products relaunch the browser for take-over.")
rep("- Nobody auto-detects \"the human started typing\".", "- None of them auto-detect \"the human started typing\".")
rep("(remote debugging alone sets it in this build)", "(cause UNVERIFIED: could be the remote-debugging switch or the attached CDP session)")
rep("SIGTERM on mac writes \"SessionEnded\" via `chrome::SessionEnding()` (chrome_browser_main_posix.cc).",
    "SIGTERM on mac calls `chrome::SessionEnding()` (chrome_browser_main_posix.cc), which presumably records \"SessionEnded\" (kForcedShutdown); that link is inferred, not read.")
rep("Stagehand (library on top of Browserbase, inherits its live view)", "Stagehand (assumed to inherit Browserbase's live view; not fetched)")
rep("- Focus emulation also makes `document.hasFocus()` true",
    "- Scope of the measurement: one window per browser, created with default focus. NOT measured: windows created with `focus:false`, several minimized bot windows in one profile process at once (the real multi-bot case), long runs, or the Mac sleeping/locking.\n- Focus emulation also makes `document.hasFocus()` true")
rep("## Q2 addendum", "## Q2 addendum") if "## Q2 addendum" in s else None
open(p, "w").write(s)
print("ok")
