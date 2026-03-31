# VNTL — Visual Novel Translator

Real-time Japanese → English translator for Windows visual novels. Hooks into the game's text rendering, extracts Japanese lines as they appear, and displays AI translations in a frameless always-on-top overlay.

## How it works

1. A GDI hook DLL is injected into the VN process and intercepts text draw calls (`TextOutW`, `ExtTextOutW`, `GetGlyphOutlineW`, etc.) — no OCR or clipboard polling needed; text is captured at the source before it is drawn.
2. Captured text is sent to the VNTL backend via a named pipe (`\\.\pipe\vntl_hook`).
3. A configurable debouncer (default 150 ms) absorbs letter-by-letter reveal animations; the line is only considered complete once the text stops changing.
4. A short batch window (default 200 ms) coalesces multiple lines that arrive in quick succession into a single translation call.
5. The completed line — along with the running dialogue history and, optionally, a scene description — is sent to your configured AI provider.
6. The translation appears in the overlay.

## Requirements

- Windows 10/11
- Python 3.11+
- An API key for at least one supported provider (or a running Ollama instance)

## Setup

```bash
uv sync
uv run python main.py
```

[uv](https://docs.astral.sh/uv/) handles the virtual environment and dependencies automatically. If you don't have uv, install it first:

```bash
# Windows
winget install astral-sh.uv

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

API keys can be entered in the settings dialog (right-click the overlay → Settings) or supplied via environment variables:

```
ANTHROPIC_API_KEY
OPENAI_API_KEY
GOOGLE_API_KEY
```

## Using VNTL

1. Launch your visual novel, then launch VNTL.
2. Right-click the overlay → **Attach to process…** and select the VN.
3. Advance one line in the VN — VNTL will discover the text streams available.
4. Right-click the overlay → **Select text streams…** and check the stream(s) that carry the main story text.
5. Click Save — the current line is translated immediately.

The overlay is draggable and resizable. Right-click for all options (settings, context management, scene description toggle, etc.).

## Text streams

When the hook DLL intercepts a text draw call it records the call-site address inside the VN executable — this address is the **stream ID**. Every distinct piece of rendering code in the game produces its own stream.

A typical VN will expose several streams: story dialogue, speaker names, menu options, system messages, HUD elements, furigana, and so on. The stream picker dialog shows a recent text sample for each stream so you can identify which ones carry the story text. Only checked streams are forwarded to the translator; unchecked streams are silently discarded.

You generally want to enable the one or two streams that contain full dialogue lines and leave everything else unchecked.

## Dialogue context and memory

VNTL maintains a rolling JP→EN dialogue history that is sent alongside each new line. This lets the translator keep character voices, name spellings, honorifics, and terminology consistent across an entire session — rather than translating each line in isolation.

When the history grows large (configurable threshold, default ~10 000 tokens), a **summarizer** model compresses the older portion into a concise reference document. The most recent lines (default 30) are always kept verbatim so the translator has immediate context. The summary is injected at the start of the conversation as a ground-truth reference.

Dialogue contexts are saved automatically and can be managed from the right-click menu (**Save context**, **Load context**, **Clear context**). Loading a saved context lets you resume a session mid-playthrough without losing established terminology.

The **Σ button** on the overlay opens the context viewer — a two-tab window showing exactly what the translator has in memory:

- **Summary** — the compressed reference document produced by the summarizer, covering character profiles, story progress, and established translation choices.
- **History** — the verbatim JP→EN pairs currently in context, with scene descriptions shown where they were captured. Text in both tabs is selectable and copyable.

This is useful for refreshing memory when resuming a session or checking whether the context has drifted. The viewer updates automatically after compaction, load, and clear.

## Scene description

When enabled (Settings → Screenshot), VNTL periodically captures a 64×64 grayscale thumbnail of the screen and computes the percentage of pixels that changed since the last frame. When the change exceeds the configured threshold, a full JPEG screenshot is sent to the **descriptor** model.

The descriptor produces a short description of what is visible — characters on screen, their expressions, and the setting. This description is prepended to the next translation prompt as `[Scene: …]`, giving the translator visual grounding for lines where the subject or emotional tone is implied by the image rather than stated in the text.

A **scene change cooldown** (default 1.0 s) prevents the descriptor from re-triggering during transition animations or fade effects.

## Clipboard fallback

The built-in GDI hook works well for most VNs but is not universal — some engines render text in ways that bypass the hooked calls entirely. For those cases, VNTL can monitor the system clipboard instead.

Popular third-party text hookers such as [Textractor](https://github.com/Artikash/Textractor) support a much wider range of VN engines and have options to copy hooked text directly to the clipboard. By enabling clipboard mode in VNTL (right-click → **Enable clipboard**) and pointing another hooker at the same game, you get broad engine compatibility while keeping VNTL's translation and context features.

Clipboard mode is also useful for manually pasting any Japanese text you want translated on the fly.

## Supported AI providers

Each of the three roles — **translator**, **descriptor** (scene images), and **summarizer** (context compaction) — can be configured independently with its own provider and model.

| Provider  | Notes |
|-----------|-------|
| **Anthropic** | Default. Uses prompt caching on the system prompt and last assistant turn to reduce latency and cost on repeated calls. |
| **OpenAI** | Standard chat completions. |
| **Google** | Gemini models via the OpenAI-compatible endpoint. |
| **Ollama** | Local models. Any Ollama model with vision support can be used as descriptor. |

Default models (used when no model is explicitly set):

| Role        | Anthropic              | OpenAI        | Google              | Ollama        |
|-------------|------------------------|---------------|---------------------|---------------|
| Translator  | claude-sonnet-4-6      | gpt-5.2       | gemini-2.5-pro      | qwen3:14b     |
| Descriptor  | claude-haiku-4-5       | gpt-4o-mini   | gemini-2.5-flash    | qwen2.5vl:7b  |
| Summarizer  | claude-haiku-4-5       | gpt-5-mini    | gemini-2.5-flash    | qwen3:14b     |

## Timing settings

All timing values are configurable in Settings → Timing:

| Setting | Default | Description |
|---------|---------|-------------|
| Text debounce | 150 ms | How long to wait after the last GDI call before treating a line as complete. Increase if lines get cut short; decrease for faster response. |
| Line batch window | 200 ms | After a line is stable, VNTL waits this long and drains any additional lines from the queue before sending — coalescing multi-part lines into one call. Set to 0 to disable. |
| Scene change cooldown | 1.0 s | Minimum gap between scene-description triggers. Prevents the descriptor from firing repeatedly during fade/transition animations. |
| Summary target size | 5 000 tokens | Target length for the compressed context summary produced by the summarizer. |

## Building a distributable

```bat
build.bat
```

Output is placed in `dist\vntl\`. Run `dist\vntl\vntl.exe` — the entire `dist\vntl\` folder must be kept together. Requires PyInstaller (installed automatically by the script).

## Building the hook DLLs

Pre-built DLLs (`hook/vntl_hook_x64.dll`, `hook/vntl_hook_x86.dll`, `hook/vntl_inject32.exe`) are committed to the repository — you only need to rebuild if you modify the C source.

VNTL automatically detects whether the target process is 32-bit (WOW64) or 64-bit and injects the appropriate DLL. The 32-bit path also uses `vntl_inject32.exe` as an intermediary because a 64-bit process cannot directly create a remote thread in a 32-bit process.

Requires a MinGW cross-compiler on a Linux host:

```bash
# Debian/Ubuntu
sudo apt install gcc-mingw-w64

# Arch/CachyOS
pacman -S mingw-w64-gcc

cd hook
make        # builds all targets (x64 DLL, x86 DLL, inject32.exe)
make clean
```

## Configuration

Config is stored at `~/.config/vntl/config.json` and written automatically on exit. Saved dialogue contexts live in `~/.config/vntl/contexts/`. AI call logs (model, tokens, latency) are written to `~/.config/vntl/llm_calls.log`.

## Architecture (for contributors)

```
VN process (GDI calls)
  → vntl_hook_x64/x86.dll     intercepts TextOutW, ExtTextOutW, GetGlyphOutlineW
  → named pipe \\.\pipe\vntl_hook   wire: [uint64 hook_id][uint32 charLen][wchar_t[]]
  → HookerService (hooker.py)  TextDebouncer per stream, grouping by hook_id
  → asyncio.Queue (text_queue)
  → input_loop (main.py)       clipboard fallback when not attached
  → Translator.translate()     (translator.py) — builds messages, calls LLM
  → OverlayWindow.update_text() (overlay.py)
```

Key files:

| File | Responsibility |
|------|---------------|
| `main.py` | Entry point, qasync event loop, `input_loop` |
| `config.py` | `Config` dataclass, JSON persistence, migration |
| `translator.py` | All LLM calls, prompt caching, call logging |
| `context_manager.py` | History, token estimation, summarization trigger |
| `hooker.py` | Named pipe server, DLL injection, debouncer, stream management |
| `overlay.py` | PyQt6 overlay UI, stream picker, settings launch |
| `screenshot_service.py` | Thumbnail diffing, scene change detection |
| `settings.py` | Settings dialog |
| `hook/vntl_hook.c` | GDI hook DLL source |
| `hook/vntl_inject32.c` | 32-bit injection helper source |
