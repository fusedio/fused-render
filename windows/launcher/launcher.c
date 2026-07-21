/* FusedRender launcher stub — the only native (non-Python) piece of the
 * full-Python supervisor experiment (docs/PYTHON_SUPERVISOR_SPEC.md).
 *
 * This is deliberately NOT the supervisor. It exists only because Explorer /
 * Start-Menu / "Open with" need a real windowless .exe to point at, and the
 * private CPython we bundle can't itself be that target (pythonw.exe would
 * show as generic "Python" in Open-With, and per-invocation argv handling
 * needs to live somewhere stable across supervisor rewrites). It execs
 * `pythonw.exe -I -m fused_render.win_supervisor <args>` and gets out of the
 * way — all argument parsing, single-instance logic, job-object ownership,
 * and tray/IPC live in fused_render/win_supervisor/*.py.
 *
 * Built by scripts/build_windows_installer.ps1 (cl.exe if present, else a
 * pinned zig cc) at release time only — day-to-day supervisor iteration runs
 * `python -m fused_render.win_supervisor` directly and never touches this.
 */
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                     PWSTR pCmdLine, int nCmdShow) {
    wchar_t self_path[MAX_PATH];
    DWORD len = GetModuleFileNameW(NULL, self_path, MAX_PATH);
    if (len == 0 || len == MAX_PATH) {
        MessageBoxW(NULL, L"FusedRender could not locate its own install directory.",
                    L"FusedRender", MB_OK | MB_ICONERROR);
        return 1;
    }

    /* Strip the file name, leaving the install dir (payload\). */
    wchar_t install_dir[MAX_PATH];
    wcscpy_s(install_dir, MAX_PATH, self_path);
    wchar_t *last_slash = wcsrchr(install_dir, L'\\');
    if (last_slash != NULL) {
        *last_slash = L'\0';
    }

    wchar_t pythonw_path[MAX_PATH];
    swprintf_s(pythonw_path, MAX_PATH, L"%s\\python\\pythonw.exe", install_dir);

    /* Build: "<pythonw>" -I -m fused_render.win_supervisor <passthrough argv> */
    wchar_t command_line[32768];
    int written = swprintf_s(command_line, 32768,
                              L"\"%s\" -I -m fused_render.win_supervisor", pythonw_path);
    if (pCmdLine != NULL && pCmdLine[0] != L'\0') {
        swprintf_s(command_line + written, 32768 - written, L" %s", pCmdLine);
    }

    BOOL shutdown_for_upgrade = (pCmdLine != NULL &&
                                  wcsstr(pCmdLine, L"--shutdown-for-upgrade") != NULL);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    BOOL ok = CreateProcessW(
        pythonw_path, command_line, NULL, NULL, FALSE,
        CREATE_NO_WINDOW, NULL, NULL, &si, &pi);

    if (!ok) {
        wchar_t message[MAX_PATH + 256];
        swprintf_s(message, MAX_PATH + 256,
                   L"FusedRender could not start (missing private Python runtime?):\n\n%s",
                   pythonw_path);
        MessageBoxW(NULL, message, L"FusedRender", MB_OK | MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);

    int exit_code = 0;
    if (shutdown_for_upgrade) {
        /* The installer's upgrade step execs us with --shutdown-for-upgrade
         * and waits synchronously for our exit code — propagate the
         * supervisor's own exit code so a failed shutdown blocks the
         * upgrade instead of silently proceeding. */
        WaitForSingleObject(pi.hProcess, INFINITE);
        DWORD code = 0;
        GetExitCodeProcess(pi.hProcess, &code);
        exit_code = (int)code;
    }
    CloseHandle(pi.hProcess);
    return exit_code;
}
