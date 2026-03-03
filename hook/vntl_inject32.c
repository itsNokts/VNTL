/*
 * vntl_inject32.c — 32-bit DLL injection helper for VNTL
 *
 * Usage: vntl_inject32.exe <pid> <dll_path_utf8>
 * Exit:  0 = success, 1 = failure
 *
 * This executable must be compiled as 32-bit so that GetProcAddress resolves
 * LoadLibraryW to the correct 32-bit address for injection into WOW64 targets.
 * A 64-bit VNTL process spawns this helper when the target VN is 32-bit.
 */

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char *argv[])
{
    if (argc < 3) {
        fprintf(stderr, "Usage: vntl_inject32.exe <pid> <dll_path_utf8>\n");
        return 1;
    }

    DWORD pid = (DWORD)atoi(argv[1]);
    if (pid == 0) {
        fprintf(stderr, "Invalid PID: %s\n", argv[1]);
        return 1;
    }

    /* Convert UTF-8 DLL path to wide string */
    int wide_len = MultiByteToWideChar(CP_UTF8, 0, argv[2], -1, NULL, 0);
    if (wide_len == 0) {
        fprintf(stderr, "MultiByteToWideChar (size) failed: %lu\n", GetLastError());
        return 1;
    }
    wchar_t *dll_path = (wchar_t *)malloc(wide_len * sizeof(wchar_t));
    if (!dll_path) {
        fprintf(stderr, "malloc failed\n");
        return 1;
    }
    if (MultiByteToWideChar(CP_UTF8, 0, argv[2], -1, dll_path, wide_len) == 0) {
        fprintf(stderr, "MultiByteToWideChar failed: %lu\n", GetLastError());
        free(dll_path);
        return 1;
    }

    /* Open target process */
    HANDLE hProc = OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid);
    if (!hProc) {
        fprintf(stderr, "OpenProcess(%lu) failed: %lu\n", pid, GetLastError());
        free(dll_path);
        return 1;
    }

    int exit_code = 1;
    SIZE_T path_bytes = (SIZE_T)(wide_len * sizeof(wchar_t));

    /* Allocate memory in target process for DLL path */
    LPVOID remote_buf = VirtualAllocEx(
        hProc, NULL, path_bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
    );
    if (!remote_buf) {
        fprintf(stderr, "VirtualAllocEx failed: %lu\n", GetLastError());
        goto cleanup_proc;
    }

    /* Write DLL path into target process */
    SIZE_T written = 0;
    if (!WriteProcessMemory(hProc, remote_buf, dll_path, path_bytes, &written)) {
        fprintf(stderr, "WriteProcessMemory failed: %lu\n", GetLastError());
        goto cleanup_buf;
    }

    /* Resolve LoadLibraryW — must be from kernel32 in this (32-bit) process
       so the address is valid in the 32-bit target's address space. */
    HMODULE hKernel32 = GetModuleHandleW(L"kernel32.dll");
    if (!hKernel32) {
        fprintf(stderr, "GetModuleHandleW(kernel32) failed: %lu\n", GetLastError());
        goto cleanup_buf;
    }
    LPTHREAD_START_ROUTINE load_lib =
        (LPTHREAD_START_ROUTINE)GetProcAddress(hKernel32, "LoadLibraryW");
    if (!load_lib) {
        fprintf(stderr, "GetProcAddress(LoadLibraryW) failed: %lu\n", GetLastError());
        goto cleanup_buf;
    }

    /* Spawn remote thread that calls LoadLibraryW(dll_path) */
    HANDLE hThread = CreateRemoteThread(
        hProc, NULL, 0, load_lib, remote_buf, 0, NULL
    );
    if (!hThread) {
        fprintf(stderr, "CreateRemoteThread failed: %lu\n", GetLastError());
        goto cleanup_buf;
    }

    WaitForSingleObject(hThread, 5000);
    CloseHandle(hThread);
    exit_code = 0;

cleanup_buf:
    VirtualFreeEx(hProc, remote_buf, 0, MEM_RELEASE);
cleanup_proc:
    CloseHandle(hProc);
    free(dll_path);
    return exit_code;
}
