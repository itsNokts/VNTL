from __future__ import annotations

import asyncio
import logging
import sys
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMessageBox
import qasync

from config import load_config, save_config
from context_manager import ContextManager
from translator import Translator
from overlay import OverlayWindow
from hooker import HookerService
from screenshot_service import ScreenshotService


def _setup_logging() -> None:
    log_dir = Path.home() / ".config" / "vntl"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_h = RotatingFileHandler(
        log_dir / "vntl.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    file_h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_h)
    # Stream handler only when running from source (not frozen with console=False)
    if not getattr(sys, "frozen", False):
        stream_h = logging.StreamHandler()
        stream_h.setFormatter(fmt)
        root.addHandler(stream_h)


_setup_logging()
logger = logging.getLogger("vntl")


def fatal(msg: str) -> None:
    QMessageBox.critical(None, "VNTL — Fatal Error", msg)
    sys.exit(1)


async def input_loop(
    translator: Translator,
    overlay: OverlayWindow,
    hooker: HookerService,
    screenshot_service: ScreenshotService,
    cfg,
    context: ContextManager,
) -> None:
    """
    Main input loop: prefer text from the DLL hooker when attached,
    fall back to clipboard polling when not.
    """
    async def _do_translate(text: str, should_pop: bool = False) -> None:
        popped = context.pop_last_line_if_matches(text) if should_pop else False
        was_compacting = context.needs_summarization()
        if was_compacting:
            overlay.show_loading("Summarizing...")
            await translator._summarize_context()
        overlay.show_loading()
        try:
            en = await translator.translate(text)
            overlay.update_text(text, en, replace_last=popped)
            if popped:
                overlay.update_context(context.summary, context.history)
            overlay.show_retry_button(
                lambda t=text: asyncio.create_task(_do_translate(t, should_pop=True))
            )
            if was_compacting:
                if translator.last_compact_error:
                    overlay.show_compact_error()
                else:
                    overlay.show_compact_indicator()
        except Exception as exc:
            cls = type(exc).__name__
            emsg = str(exc).lower()
            if "quota" in emsg or "insufficient_quota" in emsg:
                friendly = "API quota exceeded — check your billing"
            elif "ratelimit" in cls.lower() or "429" in emsg:
                friendly = "Rate limit hit — slow down or upgrade plan"
            elif "auth" in cls.lower() or "401" in emsg or "invalid_api_key" in emsg:
                friendly = "API authentication failed — check your API key"
            else:
                friendly = f"Translation error: {cls}"
            logger.error("translate() raised %s: %s", cls, exc)
            overlay.show_error_text(text, f"[{friendly}]")
            overlay.show_retry_button(
                lambda t=text: asyncio.create_task(_do_translate(t, should_pop=True)),
                is_error=True,
            )

    clipboard = QApplication.clipboard()
    last_clipboard = ""
    needs_describe_retry = False
    logger.info("Input loop started.")
    while True:
        text: str | None = None

        if hooker.is_attached:
            try:
                text = hooker.text_queue.get_nowait()
                logger.debug("Hooker text: %r", text)
                # Wait briefly so burst lines finish their debouncers, then drain
                if cfg.line_batch_window_ms > 0:
                    await asyncio.sleep(cfg.line_batch_window_ms / 1000.0)
                items = [text]
                while True:
                    try:
                        items.append(hooker.text_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                text = "\n".join(items)
            except asyncio.QueueEmpty:
                pass

        if text is None and cfg.clipboard_enabled:
            cb = clipboard.text().strip()
            if cb and cb != last_clipboard:
                last_clipboard = cb
                text = cb
                logger.debug("Clipboard: %r", text)

        if text:
            text = text.strip()
        if text:
            if cfg.screenshot_enabled and screenshot_service.is_attached:
                sc = screenshot_service.capture(force=needs_describe_retry)
                overlay.set_scene_diff(screenshot_service.last_pct, screenshot_service.last_triggered)
                if sc is not None:
                    overlay.show_loading("Describing scene...")
                    try:
                        context.current_scene_description = await translator.describe_scene(sc, text)
                        needs_describe_retry = False
                    except Exception as exc:
                        logger.warning("describe_scene failed: %s", exc)
                        overlay.set_scene_describe_error()
                        needs_describe_retry = True
            await _do_translate(text)
            if cfg.screenshot_enabled and screenshot_service.is_attached:
                screenshot_service.reset_cooldown()

        await asyncio.sleep(0.1)


async def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Load config
    cfg = load_config()

    # Pull API keys from environment if not already in config
    if not cfg.anthropic_api_key:
        cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not cfg.openai_api_key:
        cfg.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    if not cfg.google_api_key:
        cfg.google_api_key = os.environ.get("GOOGLE_API_KEY", "")

    # Init context + translator
    context = ContextManager(
        summarize_at_tokens=cfg.context_summarize_tokens,
        min_recent_lines=cfg.context_min_recent_lines,
    )

    def save_context(path: str) -> None:
        import json
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(context.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info("Context saved to %s", path)

    def load_context(path: str) -> None:
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        context.load_from_dict(data)
        logger.info(
            "Context loaded from %s (%d lines, summary=%s)",
            path, len(context.history), "yes" if context.summary else "no",
        )

    def clear_context() -> None:
        context.clear()
        logger.info("Context cleared.")

    def compact_context() -> None:
        if len(context.history) <= context._min_recent_lines:
            logger.info("Not enough history to compact (need >%d lines).", context._min_recent_lines)
            return

        async def _do_compact() -> None:
            overlay.show_loading("Summarizing...")
            await translator._summarize_context()
            if translator.last_compact_error:
                overlay.show_compact_error()
            else:
                overlay.show_compact_indicator()
                overlay.update_context(context.summary, context.history)

        asyncio.create_task(_do_compact())
        logger.info("Context compaction scheduled.")

    translator = Translator(cfg, context)

    def on_settings_save(cfg_arg) -> None:
        save_config(cfg_arg)
        translator.rebuild_clients()
        context.update_thresholds(
            cfg_arg.context_summarize_tokens,
            cfg_arg.context_min_recent_lines,
        )
        overlay.set_screenshot_enabled(cfg_arg.screenshot_enabled)

    logger.info(
        "Providers — translator: %s/%s | descriptor: %s/%s | summarizer: %s/%s",
        cfg.translator_provider, cfg.translator_model,
        cfg.descriptor_provider, cfg.descriptor_model,
        cfg.summarizer_provider, cfg.summarizer_model,
    )

    # Init hooker and create the named pipe (non-blocking)
    hooker = HookerService()
    hooker.set_debounce(cfg.debounce_ms)
    hooker.start_server()

    screenshot_service = ScreenshotService()
    screenshot_service.threshold = cfg.scene_change_threshold
    screenshot_service.cooldown  = cfg.scene_change_cooldown

    # Show overlay
    overlay = OverlayWindow(
        cfg, on_settings_save,
        save_context, load_context,
        clear_context, compact_context,
        hooker,
        screenshot_service,
        get_context_fn=lambda: (context.summary, context.history),
    )
    overlay.show()

    logger.info("VNTL running.")

    # Save overlay geometry on exit
    try:
        await input_loop(translator, overlay, hooker, screenshot_service, cfg, context)
    finally:
        hooker.detach()
        overlay.save_geometry_to_config()
        save_config(cfg)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        try:
            loop.run_until_complete(main())
        except RuntimeError as exc:
            if "Event loop stopped before Future completed" not in str(exc):
                raise
