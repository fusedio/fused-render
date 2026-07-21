import sys
from pathlib import Path

from fused_render.winopen import _ICON_VARIANT_FOR_TOKEN, extensions

# FusedRenderPy: the experiment/python-supervisor build's own exe name and
# ProgID prefix, distinct from the shipping "FusedRender" product so this
# test install never collides with a real install's registry entries.
_EXE_NAME = "FusedRenderPy.exe"
_PROGID_PREFIX = "FusedRenderPy.Desktop"
_CONTEXT_MENU_KEY = "FusedRenderPyDesktop"


def main() -> None:
    output = Path(sys.argv[1])
    command = f'""{{app}}\\payload\\{_EXE_NAME}"" ""%1""'
    lines = [
        f'Root: HKCU; Subkey: "Software\\Classes\\Applications\\{_EXE_NAME}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "FusedRender (Python Supervisor)"; Flags: uninsdeletekey',
        f'Root: HKCU; Subkey: "Software\\Classes\\Applications\\{_EXE_NAME}\\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{{app}}\\payload\\assets\\icons\\fused-render.ico,0"; Flags: uninsdeletevalue uninsdeletekeyifempty',
        f'Root: HKCU; Subkey: "Software\\Classes\\Applications\\{_EXE_NAME}\\shell\\open\\command"; ValueType: string; ValueName: ""; ValueData: "{command}"; Flags: uninsdeletevalue uninsdeletekeyifempty',
        f'Root: HKCU; Subkey: "Software\\Classes\\*\\shell\\{_CONTEXT_MENU_KEY}"; ValueType: string; ValueName: ""; ValueData: "Open with FusedRender (Python Supervisor)"; Flags: uninsdeletekey',
        f'Root: HKCU; Subkey: "Software\\Classes\\*\\shell\\{_CONTEXT_MENU_KEY}"; ValueType: string; ValueName: "Icon"; ValueData: "{{app}}\\payload\\assets\\icons\\fused-render.ico"; Flags: uninsdeletevalue uninsdeletekeyifempty',
        f'Root: HKCU; Subkey: "Software\\Classes\\*\\shell\\{_CONTEXT_MENU_KEY}\\command"; ValueType: string; ValueName: ""; ValueData: "{command}"; Flags: uninsdeletevalue uninsdeletekeyifempty',
    ]
    for extension in extensions():
        token = extension[1:].lower()
        prog_id = f"{_PROGID_PREFIX}.{token}"
        icon = _ICON_VARIANT_FOR_TOKEN.get(token, "file")
        type_name = f"{token.upper()} File (FusedRender)"
        lines.extend(
            [
                f'Root: HKCU; Subkey: "Software\\Classes\\{prog_id}"; ValueType: string; ValueName: ""; ValueData: "{type_name}"; Flags: uninsdeletekey',
                f'Root: HKCU; Subkey: "Software\\Classes\\{prog_id}\\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{{app}}\\payload\\assets\\icons\\{icon}.ico,0"; Flags: uninsdeletevalue uninsdeletekeyifempty',
                f'Root: HKCU; Subkey: "Software\\Classes\\{prog_id}\\shell\\open\\command"; ValueType: string; ValueName: ""; ValueData: "{command}"; Flags: uninsdeletevalue uninsdeletekeyifempty',
                f'Root: HKCU; Subkey: "Software\\Classes\\{extension}\\OpenWithProgids"; ValueType: string; ValueName: "{prog_id}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty',
                f'Root: HKCU; Subkey: "Software\\Classes\\Applications\\{_EXE_NAME}\\SupportedTypes"; ValueType: string; ValueName: "{extension}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty',
            ]
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
