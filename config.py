import json
import os
from dataclasses import dataclass, field, asdict

CONFIG_PATH = os.path.expanduser("~/.config/vntl/config.json")

ROLE_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "translator": {
        "anthropic": "claude-sonnet-4-6",
        "openai":    "gpt-5.2",
        "google":    "gemini-2.5-pro",
        "ollama":    "qwen3:14b",
    },
    "descriptor": {
        "anthropic": "claude-haiku-4-5",
        "openai":    "gpt-4o-mini",
        "google":    "gemini-2.5-flash",
        "ollama":    "qwen2.5vl:7b",
    },
    "summarizer": {
        "anthropic": "claude-haiku-4-5",
        "openai":    "gpt-5-mini",
        "google":    "gemini-2.5-flash",
        "ollama":    "qwen3:14b",
    },
}

ROLE_MODEL_OPTIONS: dict[str, dict[str, list[str]]] = {
    "translator": {
        "anthropic": ["claude-sonnet-4-6", "claude-sonnet-4-5", "claude-opus-4-6"],
        "openai":    ["gpt-5.2", "gpt-5-mini", "gpt-4o"],
        "google":    ["gemini-2.5-pro", "gemini-3.1-pro-preview", "gemini-2.5-flash"],
        "ollama":    ["qwen3:14b", "mistral-small3.2:24b", "gemma3:12b"],
    },
    "descriptor": {
        "anthropic": ["claude-haiku-4-5", "claude-sonnet-4-5", "claude-sonnet-4-6"],
        "openai":    ["gpt-4o-mini", "gpt-4o", "gpt-5.2"],
        "google":    ["gemini-2.5-flash", "gemini-3-flash-preview", "gemini-2.5-pro"],
        "ollama":    ["qwen2.5vl:7b", "gemma3:4b", "moondream"],
    },
    "summarizer": {
        "anthropic": ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-sonnet-4-5"],
        "openai":    ["gpt-5-mini", "gpt-5.2", "gpt-5-nano"],
        "google":    ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.1-pro-preview"],
        "ollama":    ["qwen3:14b", "gpt-oss:20b", "gemma3:12b"],
    },
}


@dataclass
class OverlayConfig:
    x: int = 100
    y: int = 100
    width: int = 700
    height: int = 150
    bg_color: list = field(default_factory=lambda: [20, 20, 25, 210])
    jp_color: list = field(default_factory=lambda: [200, 200, 200, 180])
    en_color: list = field(default_factory=lambda: [255, 255, 255, 230])
    jp_font_family: str = "Noto Sans CJK JP"
    jp_font_size:   int = 10
    en_font_family: str = "Noto Sans"
    en_font_size:   int = 13


@dataclass
class Config:
    # API keys — shared per provider
    anthropic_api_key: str = ""
    openai_api_key:    str = ""
    google_api_key:    str = ""
    ollama_base_url:   str = "http://localhost:11434/v1"

    # Per-role provider + model  ("anthropic" | "openai" | "google" | "ollama")
    translator_provider: str = "anthropic"
    translator_model:    str = "claude-sonnet-4-6"
    descriptor_provider: str = "anthropic"
    descriptor_model:    str = "claude-haiku-4-5"
    summarizer_provider: str = "anthropic"
    summarizer_model:    str = "claude-haiku-4-5"

    # Per-role Ollama thinking mode (only applies when provider == "ollama")
    translator_ollama_thinking: bool = False
    descriptor_ollama_thinking:  bool = False
    summarizer_ollama_thinking:  bool = False

    screenshot_enabled: bool = False
    clipboard_enabled:  bool = False
    context_summarize_tokens: int = 10_000
    context_min_recent_lines: int = 15

    # Per-role max output tokens
    translator_max_tokens: int = 2048
    descriptor_max_tokens: int = 2048
    summarizer_max_tokens: int = 2048
    # Screenshot scene-change sensitivity (% of pixels changed, stored as float)
    scene_change_threshold: float = 5.0

    # Editable system prompts — empty string means "use built-in default"
    translator_system_prompt: str = ""
    descriptor_system_prompt: str = ""
    summarizer_system_prompt: str = ""

    overlay: OverlayConfig = field(default_factory=OverlayConfig)


def load_config() -> Config:
    if not os.path.exists(CONFIG_PATH):
        return Config()
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        cfg = Config()

        # --- Migration: read legacy single-backend fields ---
        legacy_api_key      = data.get("api_key", "")
        legacy_model        = data.get("model", "claude-sonnet-4-6")
        legacy_backend      = data.get("backend", "claude")   # "claude" | "ollama"
        legacy_ollama_model = data.get("ollama_model", "qwen3:14b")

        _backend_map = {"claude": "anthropic", "ollama": "ollama"}
        legacy_provider = _backend_map.get(legacy_backend, "anthropic")
        legacy_role_model = legacy_ollama_model if legacy_backend == "ollama" else legacy_model

        # --- New fields (legacy values used only when new keys are absent) ---
        cfg.anthropic_api_key = data.get("anthropic_api_key", legacy_api_key)
        cfg.openai_api_key    = data.get("openai_api_key", "")
        cfg.google_api_key    = data.get("google_api_key", "")
        cfg.ollama_base_url   = data.get("ollama_base_url", "http://localhost:11434/v1")

        cfg.translator_provider = data.get("translator_provider", legacy_provider)
        cfg.translator_model    = data.get("translator_model", legacy_role_model)
        cfg.descriptor_provider = data.get("descriptor_provider", legacy_provider)
        cfg.descriptor_model    = data.get("descriptor_model", legacy_role_model)
        cfg.summarizer_provider = data.get("summarizer_provider", legacy_provider)
        cfg.summarizer_model    = data.get("summarizer_model", legacy_role_model)

        cfg.translator_ollama_thinking = data.get("translator_ollama_thinking", False)
        cfg.descriptor_ollama_thinking  = data.get("descriptor_ollama_thinking",  False)
        cfg.summarizer_ollama_thinking  = data.get("summarizer_ollama_thinking",  False)

        cfg.screenshot_enabled        = data.get("screenshot_enabled", False)
        cfg.clipboard_enabled         = data.get("clipboard_enabled", False)
        cfg.context_summarize_tokens  = data.get("context_summarize_tokens", 20_000)
        cfg.context_min_recent_lines  = data.get("context_min_recent_lines", 15)

        cfg.translator_max_tokens  = data.get("translator_max_tokens", 2048)
        cfg.descriptor_max_tokens  = data.get("descriptor_max_tokens", 2048)
        cfg.summarizer_max_tokens  = data.get("summarizer_max_tokens", 2048)
        cfg.scene_change_threshold = float(data.get("scene_change_threshold", 15.0))

        cfg.translator_system_prompt = data.get("translator_system_prompt", "")
        cfg.descriptor_system_prompt = data.get("descriptor_system_prompt", "")
        cfg.summarizer_system_prompt = data.get("summarizer_system_prompt", "")

        if "overlay" in data:
            ov = data["overlay"]
            cfg.overlay = OverlayConfig(
                x=ov.get("x", 100),
                y=ov.get("y", 100),
                width=ov.get("width", 700),
                height=ov.get("height", 150),
                bg_color=ov.get("bg_color", [20, 20, 25, 210]),
                jp_color=ov.get("jp_color", [200, 200, 200, 180]),
                en_color=ov.get("en_color", [255, 255, 255, 230]),
                jp_font_family=ov.get("jp_font_family", "Noto Sans CJK JP"),
                jp_font_size=ov.get("jp_font_size", 10),
                en_font_family=ov.get("en_font_family", "Noto Sans"),
                en_font_size=ov.get("en_font_size", 13),
            )
        return cfg
    except Exception:
        return Config()


def save_config(cfg: Config) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(asdict(cfg), f, indent=2)
