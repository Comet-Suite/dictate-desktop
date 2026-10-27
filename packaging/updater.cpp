// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Comet-Suite
// Tiny lock-free installer handoff; this executable never loads Python or Qt.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>
#include <shellapi.h>
#include <string>
#include <vector>
#include <cwchar>
#include <cerrno>

#pragma comment(lib, "bcrypt.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "user32.lib")

struct Handle {
    HANDLE value;
    explicit Handle(HANDLE h = INVALID_HANDLE_VALUE) : value(h) {}
    ~Handle() { if (value != INVALID_HANDLE_VALUE && value != nullptr) CloseHandle(value); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
};

static std::wstring quote(const std::wstring& input) {
    std::wstring result = L"\"";
    size_t backslashes = 0;
    for (wchar_t ch : input) {
        if (ch == L'\\') { ++backslashes; continue; }
        if (ch == L'\"') result.append(backslashes * 2 + 1, L'\\');
        else result.append(backslashes, L'\\');
        backslashes = 0;
        result += ch;
    }
    result.append(backslashes * 2, L'\\');
    return result + L"\"";
}

static bool sha256(HANDLE file, std::wstring& hex) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    DWORD object_size = 0, returned = 0;
    bool ok = false;
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0) return false;
    if (BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&object_size),
                          sizeof(object_size), &returned, 0) >= 0) {
        std::vector<UCHAR> object(object_size), block(1024 * 1024), digest(32);
        if (BCryptCreateHash(algorithm, &hash, object.data(), object_size, nullptr, 0, 0) >= 0) {
            DWORD length = 0;
            ok = true;
            while (true) {
                if (!ReadFile(file, block.data(), static_cast<DWORD>(block.size()), &length, nullptr)) { ok = false; break; }
                if (!length) break;
                if (BCryptHashData(hash, block.data(), length, 0) < 0) { ok = false; break; }
            }
            if (ok) ok = BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) >= 0;
            if (ok) {
                const wchar_t* digits = L"0123456789abcdef";
                for (UCHAR byte : digest) { hex += digits[byte >> 4]; hex += digits[byte & 15]; }
            }
            BCryptDestroyHash(hash);
        }
    }
    BCryptCloseAlgorithmProvider(algorithm, 0);
    return ok;
}

static bool start(const std::wstring& application, const std::wstring& arguments,
                  const std::wstring& directory, PROCESS_INFORMATION& process) {
    std::wstring command = quote(application) + (arguments.empty() ? L"" : L" " + arguments);
    STARTUPINFOW startup = {};
    startup.cb = sizeof(startup);
    return CreateProcessW(application.c_str(), &command[0], nullptr, nullptr, FALSE,
                          CREATE_UNICODE_ENVIRONMENT, nullptr, directory.c_str(), &startup, &process) != FALSE;
}

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    int count = 0;
    wchar_t** argv = CommandLineToArgvW(GetCommandLineW(), &count);
    if (!argv) return 2;
    const bool quiet = count == 6 && std::wstring(argv[5]) == L"--quiet";
    auto error = [quiet](int code, const wchar_t* text) {
        if (!quiet) MessageBoxW(nullptr, text, L"Dictate update", MB_OK | MB_ICONERROR | MB_SETFOREGROUND);
        return code;
    };
    if ((count != 5 && !quiet) || count < 5) {
        LocalFree(argv);
        return error(2, L"The updater received invalid arguments. Your installation was not changed.");
    }
    wchar_t* end = nullptr;
    errno = 0;
    unsigned long parent_pid = std::wcstoul(argv[1], &end, 10);
    std::wstring setup(argv[2]), directory(argv[3]), expected(argv[4]);
    const bool valid_pid = !errno && end && *end == L'\0' && parent_pid != 0 && parent_pid != GetCurrentProcessId();
    LocalFree(argv);
    if (!valid_pid || expected.size() != 64 || expected.find_first_not_of(L"0123456789abcdef") != std::wstring::npos)
        return error(2, L"The updater's process or checksum argument is invalid.");
    if (directory.empty()) return error(3, L"The installation directory is empty.");
    std::wstring folded = directory;
    CharLowerBuffW(&folded[0], static_cast<DWORD>(folded.size()));
    if (folded.find(L"\\windowsapps\\") != std::wstring::npos || folded.ends_with(L"\\windowsapps"))
        return error(3, L"Microsoft Store installations must be updated through their store.");
    const std::wstring executable = directory + L"\\Dictate.exe";
    if (GetFileAttributesW(executable.c_str()) == INVALID_FILE_ATTRIBUTES)
        return error(3, L"The existing Dictate installation was not found. Run the verified installer manually.");
    // Deny write/delete sharing while hashing and executing. The verified bytes
    // cannot be swapped between verification and CreateProcess by another writer.
    Handle file(CreateFileW(setup.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                            OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, nullptr));
    LARGE_INTEGER size = {};
    std::wstring actual;
    if (file.value == INVALID_HANDLE_VALUE || !GetFileSizeEx(file.value, &size) || size.QuadPart <= 0
            || size.QuadPart > 512LL * 1024 * 1024 || !sha256(file.value, actual) || actual != expected)
        return error(4, L"Installer SHA-256 verification failed. No installation was performed.");
    // A live parent is waited on, never killed. In particular this does not kill
    // another Dictate instance or interrupt a recording started during handoff.
    HANDLE parent = OpenProcess(SYNCHRONIZE, FALSE, static_cast<DWORD>(parent_pid));
    if (parent) {
        Handle waiting(parent);
        if (WaitForSingleObject(parent, 120000) != WAIT_OBJECT_0)
            return error(5, L"Dictate did not close in time. The update was cancelled; please retry.");
    } else if (GetLastError() != ERROR_INVALID_PARAMETER) {
        return error(5, L"The updater could not wait for Dictate safely. The update was cancelled.");
    }
    wchar_t helper_path[32768] = {};
    DWORD helper_length = GetModuleFileNameW(nullptr, helper_path, 32768);
    std::wstring helper_directory(helper_path, helper_length);
    helper_directory = helper_directory.substr(0, helper_directory.find_last_of(L"\\/"));
    PROCESS_INFORMATION install = {};
    const std::wstring arguments = L"/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL /NOICONS "
        L"/NOCLOSEAPPLICATIONS /NORESTARTAPPLICATIONS " + quote(L"/DIR=" + directory) + L" " +
        quote(L"/LOG=" + helper_directory + L"\\installer.log");
    if (!start(setup, arguments, directory, install))
        return error(6, L"The verified installer could not start. The existing installation is unchanged.");
    Handle install_process(install.hProcess), install_thread(install.hThread);
    if (WaitForSingleObject(install.hProcess, 600000) != WAIT_OBJECT_0)
        return error(7, L"The installer is still running. Please finish it before restarting Dictate.");
    DWORD exit_code = 1;
    if (!GetExitCodeProcess(install.hProcess, &exit_code) || exit_code != 0)
        return error(8, L"The installer reported a failure. Run the verified installer manually and choose the existing folder.");
    PROCESS_INFORMATION restart = {};
    if (!start(executable, L"", directory, restart))
        return error(9, L"Dictate was updated, but could not restart. Start it from the Start menu.");
    Handle restarted_process(restart.hProcess), restarted_thread(restart.hThread);
    WaitForInputIdle(restart.hProcess, 30000);
    return 0;
}
