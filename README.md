# VNTL — Visual Novel Translator

Real-time Japanese → English translator for Windows visual novels. Hooks into the game's text rendering, extracts Japanese lines as they appear, and displays AI translations in a frameless always-on-top overlay.

## How it works

1. A GDI hook DLL is injected into the VN process and intercepts text draw calls (`TextOutW`, `ExtTextOutW`, `GetGlyphOutlineW`, etc.)
2. Captured text is sent to the VNTL backend via a named pipe
3. A 150 ms debouncer absorbs letter-by-letter reveal animations before the line is considered complete
4. The completed line (plus rolling dialogue context and optional scene description) is sent to your chosen AI provider
5. The translation appears in the overlay

## Requirements

- Windows 10/11
- Python 3.11+
- An API key for at least one supported provider (or a running Ollama instance)

## Setup

```bash
pip install -r requirements.txt
python main.py
```

API keys can be entered in the settings dialog (right-click the overlay → Settings) or supplied via environment variables:

```
ANTHROPIC_API_KEY
OPENAI_API_KEY
GOOGLE_API_KEY
```

## Using VNTL

1. Launch your visual novel, then launch VNTL
2. Right-click the overlay → **Attach to process…** and select the VN
3. Advance one line in the VN — VNTL will discover the text streams available
4. Right-click the overlay → **Select text streams…** and check the stream(s) that carry the main story text (samples are shown to help identify them)
5. Click Save — the current line is translated immediately

The overlay is draggable and resizable. Right-click for all options (settings, context management, scene description toggle, etc.).

## Supported AI providers

Each of the three roles — **translator**, **descriptor** (scene images), and **summarizer** (context compaction) — can be configured independently.

| Provider  | Notes |
|-----------|-------|
| **Anthropic** | Default. Uses prompt caching to reduce latency and cost. |
| **OpenAI** | Standard chat completions. |
| **Google** | Gemini models via the OpenAI-compatible endpoint. |
| **Ollama** | Local models. Any Ollama model with vision support can be used as descriptor. |

Default models (used when no model is explicitly set):

| Role        | Anthropic              | OpenAI        | Google              | Ollama        |
|-------------|------------------------|---------------|---------------------|---------------|
| Translator  | claude-sonnet-4-6      | gpt-5.2       | gemini-2.5-pro      | qwen3:14b     |
| Descriptor  | claude-haiku-4-5       | gpt-4o-mini   | gemini-2.5-flash    | qwen2.5vl:7b  |
| Summarizer  | claude-haiku-4-5       | gpt-5-mini    | gemini-2.5-flash    | qwen3:14b     |

## Building a distributable

```bat
build.bat
```

Output is placed in `dist\vntl\`. Requires PyInstaller (installed automatically by the script).

## Building the hook DLLs

Pre-built DLLs (`hook/vntl_hook_x64.dll`, `hook/vntl_hook_x86.dll`, `hook/vntl_inject32.exe`) are committed to the repository — you only need to rebuild if you modify the C source.

Requires a MinGW cross-compiler on a Linux host:

```bash
# Debian/Ubuntu
sudo apt install gcc-mingw-w64

# Arch/CachyOS
pacman -S mingw-w64-gcc

cd hook
make        # builds all targets
make clean
```

## Configuration

Config is stored at `~/.config/vntl/config.json` and written automatically on exit. Saved dialogue contexts live in `~/.config/vntl/contexts/`. AI call logs are written to `~/.config/vntl/llm_calls.log`.
