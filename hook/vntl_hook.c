/*
 * vntl_hook.c — VNTL text hooker DLL
 *
 * Hooks GDI TextOutW, ExtTextOutW, TextOutA, ExtTextOutA, the USER32
 * DrawText/DrawTextEx family, and GetGlyphOutlineW/A in all loaded modules
 * via IAT patching.  Captured text is sent to the VNTL named-pipe server as
 * framed messages:
 *
 *   [ UINT64 hook_id ][ DWORD charLen ][ wchar_t text[charLen] ]
 *
 * hook_id is the caller's return address (unique per call-site in the game),
 * cast to UINT64.  The Python side groups text by hook_id into "streams".
 *
 * GetGlyphOutlineW/A is called one character at a time, so we accumulate
 * characters per call-site in a small table and flush complete strings after
 * ACCUM_FLUSH_MS milliseconds of silence (handled by a background thread).
 *
 * Build with MinGW:
 *   x86:  i686-w64-mingw32-gcc   -m32 -shared -o vntl_hook_x86.dll vntl_hook.c -lgdi32 -lpsapi
 *   x64:  x86_64-w64-mingw32-gcc -m64 -shared -o vntl_hook_x64.dll vntl_hook.c -lgdi32 -lpsapi
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <string.h>

/* -------------------------------------------------------------------------
 * Globals — pipe + string-level hooks
 * ---------------------------------------------------------------------- */

static HANDLE g_pipe   = INVALID_HANDLE_VALUE;
static BOOL   g_hooked = FALSE;

#define MAX_TEXT 65536

/* Saved original function pointers — GDI32.DLL string hooks */
typedef BOOL (WINAPI *PfnTextOutW)   (HDC, int, int, LPCWSTR, int);
typedef BOOL (WINAPI *PfnExtTextOutW)(HDC, int, int, UINT, const RECT *,
                                       LPCWSTR, UINT, const INT *);
typedef BOOL (WINAPI *PfnTextOutA)   (HDC, int, int, LPCSTR, int);
typedef BOOL (WINAPI *PfnExtTextOutA)(HDC, int, int, UINT, const RECT *,
                                       LPCSTR, UINT, const INT *);

static PfnTextOutW    g_orig_TextOutW    = NULL;
static PfnExtTextOutW g_orig_ExtTextOutW = NULL;
static PfnTextOutA    g_orig_TextOutA    = NULL;
static PfnExtTextOutA g_orig_ExtTextOutA = NULL;

/* Saved original function pointers — USER32.DLL DrawText family */
typedef int (WINAPI *PfnDrawTextW)  (HDC, LPCWSTR, int, LPRECT, UINT);
typedef int (WINAPI *PfnDrawTextA)  (HDC, LPCSTR,  int, LPRECT, UINT);
typedef int (WINAPI *PfnDrawTextExW)(HDC, LPCWSTR, int, LPRECT, UINT,
                                      LPDRAWTEXTPARAMS);
typedef int (WINAPI *PfnDrawTextExA)(HDC, LPSTR,   int, LPRECT, UINT,
                                      LPDRAWTEXTPARAMS);

static PfnDrawTextW   g_orig_DrawTextW   = NULL;
static PfnDrawTextA   g_orig_DrawTextA   = NULL;
static PfnDrawTextExW g_orig_DrawTextExW = NULL;
static PfnDrawTextExA g_orig_DrawTextExA = NULL;

/* -------------------------------------------------------------------------
 * Globals — GetGlyphOutline accumulator
 *
 * Many VN engines call GetGlyphOutlineW in a tight loop (one call per
 * character) rather than using TextOutW.  We accumulate those individual
 * characters into a per-call-site buffer and flush complete strings to the
 * pipe after ACCUM_FLUSH_MS milliseconds of silence.
 * ---------------------------------------------------------------------- */

#define MAX_ACCUM_SLOTS 32
#define ACCUM_FLUSH_MS  200   /* ms of silence before flushing a stream */

typedef DWORD (WINAPI *PfnGetGlyphOutlineW)(HDC, UINT, UINT,
                                             LPGLYPHMETRICS, DWORD,
                                             LPVOID, const MAT2 *);
typedef DWORD (WINAPI *PfnGetGlyphOutlineA)(HDC, UINT, UINT,
                                             LPGLYPHMETRICS, DWORD,
                                             LPVOID, const MAT2 *);

static PfnGetGlyphOutlineW g_orig_GetGlyphOutlineW = NULL;
static PfnGetGlyphOutlineA g_orig_GetGlyphOutlineA = NULL;

typedef struct {
    UINT64  hook_id;
    wchar_t buf[MAX_TEXT];
    int     len;
    wchar_t last_sent[MAX_TEXT]; /* dedup: skip flush if text unchanged */
    DWORD   last_tick;           /* GetTickCount() of last appended char */
    BOOL    active;
} GlyphAccum;

static GlyphAccum       g_accums[MAX_ACCUM_SLOTS];
static CRITICAL_SECTION g_cs;
static HANDLE           g_flush_thread = NULL;
static volatile BOOL    g_running      = FALSE;

/* -------------------------------------------------------------------------
 * Pipe helpers
 * ---------------------------------------------------------------------- */

/*
 * pipe_send — send one text event with its caller hook_id.
 * Filters purely-whitespace strings.  Deduplication is handled in Python
 * (per-stream TextDebouncer), so we do not deduplicate here.
 */
static void pipe_send(UINT64 hook_id, LPCWSTR text, int count)
{
    if (g_pipe == INVALID_HANDLE_VALUE) return;
    if (!text) return;

    /* count = -1 means null-terminated (DrawText convention) */
    int len = (count < 0) ? (int)wcslen(text) : count;
    if (len == 0) return;
    if (len >= MAX_TEXT) len = MAX_TEXT - 1;

    /* Build a bounded, null-terminated copy */
    wchar_t buf[MAX_TEXT];
    wcsncpy(buf, text, len);
    buf[len] = L'\0';

    /* Skip purely whitespace strings */
    BOOL all_space = TRUE;
    for (int i = 0; i < len; i++) {
        if (buf[i] > L' ') { all_space = FALSE; break; }
    }
    if (all_space) return;

    /* Send: 8-byte hook_id, 4-byte char count, UTF-16 text body */
    DWORD written;
    DWORD charLen = (DWORD)len;
    WriteFile(g_pipe, &hook_id, sizeof(UINT64),          &written, NULL);
    WriteFile(g_pipe, &charLen, sizeof(DWORD),            &written, NULL);
    WriteFile(g_pipe, buf,      charLen * sizeof(wchar_t), &written, NULL);
}

/* -------------------------------------------------------------------------
 * Glyph accumulator helpers
 * ---------------------------------------------------------------------- */

/* Must be called with g_cs held. */
static void flush_accum_locked(GlyphAccum *acc)
{
    if (acc->len > 0 && wcscmp(acc->buf, acc->last_sent) != 0) {
        pipe_send(acc->hook_id, acc->buf, acc->len);
        wcsncpy(acc->last_sent, acc->buf, MAX_TEXT - 1);
        acc->last_sent[MAX_TEXT - 1] = L'\0';
    }
    acc->len    = 0;
    acc->buf[0] = L'\0';
}

/*
 * flush_thread_proc — wakes every 50 ms and flushes any accumulator that
 * has been silent for at least ACCUM_FLUSH_MS milliseconds.
 * This ensures accumulated text is delivered even when the game's render
 * loop stops calling GetGlyphOutlineW (e.g. the game is idle between clicks).
 *
 * NOTE: Do NOT call WaitForSingleObject on this thread from DllMain — that
 * would deadlock while holding the loader lock.  We simply signal g_running
 * = FALSE and release the handle; the thread exits on its own within 50 ms.
 */
static DWORD WINAPI flush_thread_proc(LPVOID unused)
{
    (void)unused;
    while (g_running) {
        Sleep(50);
        DWORD now = GetTickCount();
        EnterCriticalSection(&g_cs);
        for (int i = 0; i < MAX_ACCUM_SLOTS; i++) {
            GlyphAccum *a = &g_accums[i];
            if (a->active && a->len > 0 &&
                (now - a->last_tick) >= ACCUM_FLUSH_MS) {
                flush_accum_locked(a);
            }
        }
        LeaveCriticalSection(&g_cs);
    }
    return 0;
}

/*
 * accum_char — append one Unicode character to the per-call-site buffer.
 * If the slot has been silent for >= ACCUM_FLUSH_MS, the previous content
 * is flushed first (sends the previous dialogue line before starting a new one).
 */
static void accum_char(UINT64 hook_id, wchar_t wch)
{
    if (wch <= L' ') return; /* skip whitespace/control characters */

    DWORD now = GetTickCount();
    EnterCriticalSection(&g_cs);

    /* Find existing slot for this call-site */
    GlyphAccum *acc = NULL;
    for (int i = 0; i < MAX_ACCUM_SLOTS; i++) {
        if (g_accums[i].active && g_accums[i].hook_id == hook_id) {
            acc = &g_accums[i];
            break;
        }
    }
    /* Allocate a new slot if needed */
    if (!acc) {
        for (int i = 0; i < MAX_ACCUM_SLOTS; i++) {
            if (!g_accums[i].active) {
                memset(&g_accums[i], 0, sizeof(g_accums[i]));
                g_accums[i].hook_id = hook_id;
                g_accums[i].active  = TRUE;
                acc = &g_accums[i];
                break;
            }
        }
    }

    if (acc) {
        /* Flush old content if we've been silent long enough */
        if (acc->len > 0 && (now - acc->last_tick) >= ACCUM_FLUSH_MS)
            flush_accum_locked(acc);
        /* Append character */
        if (acc->len < MAX_TEXT - 1) {
            acc->buf[acc->len++] = wch;
            acc->buf[acc->len]   = L'\0';
        }
        acc->last_tick = now;
    }

    LeaveCriticalSection(&g_cs);
}

/* -------------------------------------------------------------------------
 * Hook implementations — Wide (W) variants
 * ---------------------------------------------------------------------- */

static BOOL WINAPI HookedTextOutW(HDC hdc, int x, int y,
                                   LPCWSTR lpString, int c)
{
    UINT64 id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
    pipe_send(id, lpString, c);
    return g_orig_TextOutW(hdc, x, y, lpString, c);
}

static BOOL WINAPI HookedExtTextOutW(HDC hdc, int x, int y, UINT options,
                                      const RECT *lprect, LPCWSTR lpString,
                                      UINT c, const INT *lpDx)
{
    UINT64 id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
    pipe_send(id, lpString, (int)c);
    return g_orig_ExtTextOutW(hdc, x, y, options, lprect, lpString, c, lpDx);
}

/* -------------------------------------------------------------------------
 * Hook implementations — ANSI (A) variants
 * ---------------------------------------------------------------------- */

static BOOL WINAPI HookedTextOutA(HDC hdc, int x, int y,
                                   LPCSTR lpString, int c)
{
    UINT64 id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
    wchar_t wide[MAX_TEXT];
    int n = MultiByteToWideChar(CP_ACP, 0, lpString, c, wide, MAX_TEXT - 1);
    if (n > 0) pipe_send(id, wide, n);
    return g_orig_TextOutA(hdc, x, y, lpString, c);
}

static BOOL WINAPI HookedExtTextOutA(HDC hdc, int x, int y, UINT options,
                                      const RECT *lprect, LPCSTR lpString,
                                      UINT c, const INT *lpDx)
{
    UINT64 id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
    wchar_t wide[MAX_TEXT];
    int n = MultiByteToWideChar(CP_ACP, 0, lpString, (int)c, wide, MAX_TEXT - 1);
    if (n > 0) pipe_send(id, wide, n);
    return g_orig_ExtTextOutA(hdc, x, y, options, lprect, lpString, c, lpDx);
}

/* -------------------------------------------------------------------------
 * Hook implementations — DrawText / DrawTextEx (user32.dll)
 * ---------------------------------------------------------------------- */

static int WINAPI HookedDrawTextW(HDC hdc, LPCWSTR s, int c,
                                   LPRECT r, UINT fmt)
{
    UINT64 id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
    pipe_send(id, s, c);
    return g_orig_DrawTextW(hdc, s, c, r, fmt);
}

static int WINAPI HookedDrawTextA(HDC hdc, LPCSTR s, int c,
                                   LPRECT r, UINT fmt)
{
    UINT64 id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
    wchar_t wide[MAX_TEXT];
    int n = MultiByteToWideChar(CP_ACP, 0, s, c, wide, MAX_TEXT - 1);
    if (n > 0) pipe_send(id, wide, n);
    return g_orig_DrawTextA(hdc, s, c, r, fmt);
}

static int WINAPI HookedDrawTextExW(HDC hdc, LPCWSTR s, int c,
                                     LPRECT r, UINT fmt, LPDRAWTEXTPARAMS p)
{
    UINT64 id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
    pipe_send(id, s, c);
    return g_orig_DrawTextExW(hdc, s, c, r, fmt, p);
}

static int WINAPI HookedDrawTextExA(HDC hdc, LPSTR s, int c,
                                     LPRECT r, UINT fmt, LPDRAWTEXTPARAMS p)
{
    UINT64 id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
    wchar_t wide[MAX_TEXT];
    int n = MultiByteToWideChar(CP_ACP, 0, s, c, wide, MAX_TEXT - 1);
    if (n > 0) pipe_send(id, wide, n);
    return g_orig_DrawTextExA(hdc, s, c, r, fmt, p);
}

/* -------------------------------------------------------------------------
 * Hook implementations — GetGlyphOutline (character-by-character accumulator)
 * ---------------------------------------------------------------------- */

static DWORD WINAPI HookedGetGlyphOutlineW(HDC hdc, UINT uChar, UINT fuFormat,
                                            LPGLYPHMETRICS lpgm, DWORD cjBuffer,
                                            LPVOID pvBuffer, const MAT2 *lpmat2)
{
    if (g_pipe != INVALID_HANDLE_VALUE) {
        UINT64 id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
        accum_char(id, (wchar_t)uChar);
    }
    return g_orig_GetGlyphOutlineW(hdc, uChar, fuFormat, lpgm, cjBuffer,
                                   pvBuffer, lpmat2);
}

static DWORD WINAPI HookedGetGlyphOutlineA(HDC hdc, UINT uChar, UINT fuFormat,
                                            LPGLYPHMETRICS lpgm, DWORD cjBuffer,
                                            LPVOID pvBuffer, const MAT2 *lpmat2)
{
    if (g_pipe != INVALID_HANDLE_VALUE) {
        UINT64  id = (UINT64)(ULONG_PTR)__builtin_return_address(0);
        wchar_t wide[3] = {0};
        char    ansi[3] = {0};
        /* Handle double-byte ANSI (e.g. Shift-JIS) */
        if (uChar > 0xFF) {
            ansi[0] = (char)((uChar >> 8) & 0xFF);
            ansi[1] = (char)(uChar & 0xFF);
            MultiByteToWideChar(CP_ACP, 0, ansi, 2, wide, 2);
        } else {
            ansi[0] = (char)(uChar & 0xFF);
            MultiByteToWideChar(CP_ACP, 0, ansi, 1, wide, 1);
        }
        if (wide[0]) accum_char(id, wide[0]);
    }
    return g_orig_GetGlyphOutlineA(hdc, uChar, fuFormat, lpgm, cjBuffer,
                                   pvBuffer, lpmat2);
}

/* -------------------------------------------------------------------------
 * IAT patching helpers
 * ---------------------------------------------------------------------- */

/*
 * patch_iat_in_module — scan one module's IAT for a specific import and
 * replace its function pointer.  Returns the original pointer, or NULL if
 * the import was not found in this module.
 */
static PROC patch_iat_in_module(HMODULE hmod,
                                 const char *dll_name,
                                 const char *func_name,
                                 PROC       new_func)
{
    if (!hmod) return NULL;

    BYTE *base = (BYTE *)hmod;

    /* Validate DOS header */
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return NULL;
    if (dos->e_lfanew <= 0 || dos->e_lfanew > 0x40000000) return NULL;

    /* Validate NT headers */
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return NULL;

    DWORD imp_rva =
        nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT]
        .VirtualAddress;
    if (!imp_rva) return NULL;

    DWORD imp_size =
        nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].Size;
    if (!imp_size) return NULL;

    IMAGE_IMPORT_DESCRIPTOR *desc =
        (IMAGE_IMPORT_DESCRIPTOR *)(base + imp_rva);

    /* Walk import descriptors */
    IMAGE_IMPORT_DESCRIPTOR *desc_end =
        (IMAGE_IMPORT_DESCRIPTOR *)(base + imp_rva + imp_size);

    for (; desc < desc_end && desc->Name; desc++) {
        char *name = (char *)(base + desc->Name);
        if (_stricmp(name, dll_name) != 0) continue;
        if (!desc->FirstThunk || !desc->OriginalFirstThunk) continue;

        IMAGE_THUNK_DATA *iat =
            (IMAGE_THUNK_DATA *)(base + desc->FirstThunk);
        IMAGE_THUNK_DATA *orig =
            (IMAGE_THUNK_DATA *)(base + desc->OriginalFirstThunk);

        for (; iat->u1.Function; iat++, orig++) {
            if (IMAGE_SNAP_BY_ORDINAL(orig->u1.Ordinal)) continue;
            if (!orig->u1.AddressOfData) continue;

            IMAGE_IMPORT_BY_NAME *ibn =
                (IMAGE_IMPORT_BY_NAME *)(base + orig->u1.AddressOfData);
            if (strcmp((char *)ibn->Name, func_name) != 0) continue;

            /* Found the entry — unprotect, swap, re-protect */
            PROC old = (PROC)(ULONG_PTR)iat->u1.Function;
            DWORD old_prot;
            VirtualProtect(&iat->u1.Function, sizeof(PROC),
                           PAGE_READWRITE, &old_prot);
            iat->u1.Function = (ULONG_PTR)new_func;
            VirtualProtect(&iat->u1.Function, sizeof(PROC),
                           old_prot, &old_prot);
            return old;
        }
    }
    return NULL;
}

/*
 * patch_all_modules — walk every loaded module and patch the named import.
 * Returns the first original pointer found (they should all be the same).
 */
static PROC patch_all_modules(const char *dll_name,
                               const char *func_name,
                               PROC        new_func)
{
    PROC first_orig = NULL;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE,
                                           GetCurrentProcessId());
    if (snap == INVALID_HANDLE_VALUE) return NULL;

    MODULEENTRY32 me;
    me.dwSize = sizeof(me);
    if (Module32First(snap, &me)) {
        do {
            PROC orig = patch_iat_in_module((HMODULE)me.modBaseAddr,
                                             dll_name, func_name, new_func);
            if (orig && !first_orig) first_orig = orig;
        } while (Module32Next(snap, &me));
    }
    CloseHandle(snap);
    return first_orig;
}

/*
 * restore_all_modules — undo our patches across every loaded module.
 */
static void restore_all_modules(const char *dll_name,
                                  const char *func_name,
                                  PROC        orig_func)
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE,
                                           GetCurrentProcessId());
    if (snap == INVALID_HANDLE_VALUE) return;

    MODULEENTRY32 me;
    me.dwSize = sizeof(me);
    if (Module32First(snap, &me)) {
        do {
            patch_iat_in_module((HMODULE)me.modBaseAddr,
                                 dll_name, func_name, orig_func);
        } while (Module32Next(snap, &me));
    }
    CloseHandle(snap);
}

/* -------------------------------------------------------------------------
 * DllMain
 * ---------------------------------------------------------------------- */

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD reason, LPVOID reserved)
{
    (void)hinstDLL;
    (void)reserved;

    switch (reason) {

    case DLL_PROCESS_ATTACH:
        DisableThreadLibraryCalls(hinstDLL);

        /* Connect to VNTL's named pipe (retry up to 3 times) */
        for (int i = 0; i < 3; i++) {
            g_pipe = CreateFileW(
                L"\\\\.\\pipe\\vntl_hook",
                GENERIC_WRITE,
                0, NULL,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                NULL);
            if (g_pipe != INVALID_HANDLE_VALUE) break;
            Sleep(500);
        }
        if (g_pipe == INVALID_HANDLE_VALUE) return FALSE;

        /* Initialise glyph accumulator */
        memset(g_accums, 0, sizeof(g_accums));
        InitializeCriticalSection(&g_cs);
        g_running      = TRUE;
        g_flush_thread = CreateThread(NULL, 0, flush_thread_proc, NULL, 0, NULL);

        /* Patch Wide variants */
        g_orig_TextOutW = (PfnTextOutW)
            patch_all_modules("GDI32.DLL", "TextOutW",
                              (PROC)HookedTextOutW);
        g_orig_ExtTextOutW = (PfnExtTextOutW)
            patch_all_modules("GDI32.DLL", "ExtTextOutW",
                              (PROC)HookedExtTextOutW);

        /* Patch ANSI variants */
        g_orig_TextOutA = (PfnTextOutA)
            patch_all_modules("GDI32.DLL", "TextOutA",
                              (PROC)HookedTextOutA);
        g_orig_ExtTextOutA = (PfnExtTextOutA)
            patch_all_modules("GDI32.DLL", "ExtTextOutA",
                              (PROC)HookedExtTextOutA);

        /* Patch DrawText / DrawTextEx (user32.dll) */
        g_orig_DrawTextW   = (PfnDrawTextW)
            patch_all_modules("USER32.DLL", "DrawTextW",   (PROC)HookedDrawTextW);
        g_orig_DrawTextA   = (PfnDrawTextA)
            patch_all_modules("USER32.DLL", "DrawTextA",   (PROC)HookedDrawTextA);
        g_orig_DrawTextExW = (PfnDrawTextExW)
            patch_all_modules("USER32.DLL", "DrawTextExW", (PROC)HookedDrawTextExW);
        g_orig_DrawTextExA = (PfnDrawTextExA)
            patch_all_modules("USER32.DLL", "DrawTextExA", (PROC)HookedDrawTextExA);

        /* Patch GetGlyphOutline (character-by-character engines) */
        g_orig_GetGlyphOutlineW = (PfnGetGlyphOutlineW)
            patch_all_modules("GDI32.DLL", "GetGlyphOutlineW",
                              (PROC)HookedGetGlyphOutlineW);
        g_orig_GetGlyphOutlineA = (PfnGetGlyphOutlineA)
            patch_all_modules("GDI32.DLL", "GetGlyphOutlineA",
                              (PROC)HookedGetGlyphOutlineA);

        /* Fall back to direct GetProcAddress if IAT had no imports to patch */
        {
            HMODULE gdi = GetModuleHandleW(L"GDI32.DLL");
            if (!g_orig_TextOutW) {
                g_orig_TextOutW    = (PfnTextOutW)   GetProcAddress(gdi, "TextOutW");
                g_orig_ExtTextOutW = (PfnExtTextOutW) GetProcAddress(gdi, "ExtTextOutW");
            }
            if (!g_orig_TextOutA) {
                g_orig_TextOutA    = (PfnTextOutA)   GetProcAddress(gdi, "TextOutA");
                g_orig_ExtTextOutA = (PfnExtTextOutA) GetProcAddress(gdi, "ExtTextOutA");
            }
            if (!g_orig_GetGlyphOutlineW) {
                g_orig_GetGlyphOutlineW = (PfnGetGlyphOutlineW)
                    GetProcAddress(gdi, "GetGlyphOutlineW");
                g_orig_GetGlyphOutlineA = (PfnGetGlyphOutlineA)
                    GetProcAddress(gdi, "GetGlyphOutlineA");
            }
        }

        g_hooked = TRUE;
        break;

    case DLL_PROCESS_DETACH:
        /* Stop flush thread (signal only — do not WaitForSingleObject inside
         * DllMain as that would deadlock while holding the loader lock). */
        g_running = FALSE;
        if (g_flush_thread) {
            CloseHandle(g_flush_thread);
            g_flush_thread = NULL;
        }

        if (g_hooked) {
            if (g_orig_GetGlyphOutlineW)
                restore_all_modules("GDI32.DLL", "GetGlyphOutlineW",
                                     (PROC)g_orig_GetGlyphOutlineW);
            if (g_orig_GetGlyphOutlineA)
                restore_all_modules("GDI32.DLL", "GetGlyphOutlineA",
                                     (PROC)g_orig_GetGlyphOutlineA);
            if (g_orig_TextOutW)
                restore_all_modules("GDI32.DLL", "TextOutW",
                                     (PROC)g_orig_TextOutW);
            if (g_orig_ExtTextOutW)
                restore_all_modules("GDI32.DLL", "ExtTextOutW",
                                     (PROC)g_orig_ExtTextOutW);
            if (g_orig_TextOutA)
                restore_all_modules("GDI32.DLL", "TextOutA",
                                     (PROC)g_orig_TextOutA);
            if (g_orig_ExtTextOutA)
                restore_all_modules("GDI32.DLL", "ExtTextOutA",
                                     (PROC)g_orig_ExtTextOutA);
            if (g_orig_DrawTextW)
                restore_all_modules("USER32.DLL", "DrawTextW",
                                     (PROC)g_orig_DrawTextW);
            if (g_orig_DrawTextA)
                restore_all_modules("USER32.DLL", "DrawTextA",
                                     (PROC)g_orig_DrawTextA);
            if (g_orig_DrawTextExW)
                restore_all_modules("USER32.DLL", "DrawTextExW",
                                     (PROC)g_orig_DrawTextExW);
            if (g_orig_DrawTextExA)
                restore_all_modules("USER32.DLL", "DrawTextExA",
                                     (PROC)g_orig_DrawTextExA);
            g_hooked = FALSE;
        }

        DeleteCriticalSection(&g_cs);

        if (g_pipe != INVALID_HANDLE_VALUE) {
            CloseHandle(g_pipe);
            g_pipe = INVALID_HANDLE_VALUE;
        }
        break;
    }
    return TRUE;
}
