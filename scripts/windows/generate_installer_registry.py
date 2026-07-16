import sys
from pathlib import Path

from fused_render.winopen import _ICON_VARIANT_FOR_TOKEN, extensions


def main() -> None:
    output = Path(sys.argv[1])
    command = '""{app}\\payload\\FusedRender.exe"" ""%1""'
    lines = [
        'Root: HKCU; Subkey: "Software\\Classes\\Applications\\FusedRender.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "FusedRender"; Flags: uninsdeletekey',
        'Root: HKCU; Subkey: "Software\\Classes\\Applications\\FusedRender.exe\\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\\payload\\assets\\icons\\fused-render.ico,0"; Flags: uninsdeletevalue uninsdeletekeyifempty',
        f'Root: HKCU; Subkey: "Software\\Classes\\Applications\\FusedRender.exe\\shell\\open\\command"; ValueType: string; ValueName: ""; ValueData: "{command}"; Flags: uninsdeletevalue uninsdeletekeyifempty',
        'Root: HKCU; Subkey: "Software\\Classes\\*\\shell\\FusedRenderDesktop"; ValueType: string; ValueName: ""; ValueData: "Open with FusedRender"; Flags: uninsdeletekey',
        'Root: HKCU; Subkey: "Software\\Classes\\*\\shell\\FusedRenderDesktop"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\\payload\\assets\\icons\\fused-render.ico"; Flags: uninsdeletevalue uninsdeletekeyifempty',
        f'Root: HKCU; Subkey: "Software\\Classes\\*\\shell\\FusedRenderDesktop\\command"; ValueType: string; ValueName: ""; ValueData: "{command}"; Flags: uninsdeletevalue uninsdeletekeyifempty',
    ]
    for extension in extensions():
        token = extension[1:].lower()
        prog_id = f"FusedRender.Desktop.{token}"
        icon = _ICON_VARIANT_FOR_TOKEN.get(token, "file")
        type_name = f"{token.upper()} File (FusedRender)"
        lines.extend(
            [
                f'Root: HKCU; Subkey: "Software\\Classes\\{prog_id}"; ValueType: string; ValueName: ""; ValueData: "{type_name}"; Flags: uninsdeletekey',
                f'Root: HKCU; Subkey: "Software\\Classes\\{prog_id}\\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{{app}}\\payload\\assets\\icons\\{icon}.ico,0"; Flags: uninsdeletevalue uninsdeletekeyifempty',
                f'Root: HKCU; Subkey: "Software\\Classes\\{prog_id}\\shell\\open\\command"; ValueType: string; ValueName: ""; ValueData: "{command}"; Flags: uninsdeletevalue uninsdeletekeyifempty',
                f'Root: HKCU; Subkey: "Software\\Classes\\{extension}\\OpenWithProgids"; ValueType: string; ValueName: "{prog_id}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty',
                f'Root: HKCU; Subkey: "Software\\Classes\\Applications\\FusedRender.exe\\SupportedTypes"; ValueType: string; ValueName: "{extension}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty',
            ]
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
