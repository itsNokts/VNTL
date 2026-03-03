from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path

import anthropic

from config import Config
from context_manager import ContextManager

logger = logging.getLogger(__name__)

_LOG_PATH = Path.home() / ".config" / "vntl" / "llm_calls.log"


_SYSTEM_PROMPT = """\
You are a professional Japanese-to-English visual novel translator working in real-time. \
Translate each Japanese line into natural, publication-quality English that preserves \
every character's distinct voice, tone, and personality.

The conversation history contains prior JP\u2192EN pairs from this session. \
Use them as your translation memory: stay consistent with established character voices, \
name spellings, and any recurring terminology.

Guidelines:
- Match each character's register: formal, casual, rough, archaic, childlike, etc. \
  A gruff character should sound gruff; a formal character should sound formal.
- Preserve meaningful verbal tics or sentence-ending patterns that define a character \u2014 \
  convey their effect in English rather than transliterating them phonetically.
- Preserve Japanese honorifics (e.g. -san, -kun, -chan, -senpai) in the English output \
  as they convey important social nuance and character relationships.
- Convert Japanese ellipses (\u2026\u2026) to English ellipses (\u2026). Keep dramatic dashes (\u2014\u2014) \
  that signal interrupted or hesitant speech.
- For narration, use flowing literary prose. For dialogue, use natural spoken English.
- A [Scene:] header provides a visual description of the current situation as captured at \
  that moment \u2014 who is on screen, their expressions, and the setting. It remains relevant \
  for all following lines until the next [Scene:] header appears. Use it to inform the \
  speaker identity, tone, and context, but do not reference it explicitly in your output.
- If the Japanese line includes a speaker name prefix (e.g. \u300c\u6728\u6751\uff1a\u300d), romanize or translate \
  the name and preserve the prefix in your output (e.g. "Kimura:"). This lets the history \
  record who said what in a readable form. Do not add speaker labels that are not present \
  in the original.
- Output ONLY the English translation \u2014 no notes, no explanations, \
  no quotation marks wrapping the entire line. Translate words into natural English; \
  romanize only when no English equivalent exists (e.g. culturally specific terms \
  like kotatsu or tatami).\
"""

_SUMMARIZE_SYSTEM = (
    "You are a context compressor for a real-time Japanese visual novel translator. "
    "Your role is to condense old dialogue history into a concise summary that gives "
    "the translator everything it needs to maintain consistency going forward.\n\n"
    "A good summary for a translator is different from a plot summary: "
    "character voices matter as much as events. "
    "Capture how each character speaks \u2014 their register, pronouns, verbal tics, "
    "and speech patterns \u2014 not just what they said or did. "
    "Preserve established term choices and proper nouns exactly as previously translated. "
    "Be specific and concrete; avoid vague generalisations.\n\n"
    "Your summary must preserve:\n"
    "- All character names that have appeared, their relationships, and visual appearance "
    "(when described in scene context)\n"
    "- Each character's speech register, pronoun usage, and verbal tics "
    "(e.g. uses \u2018ore\u2019, speaks roughly, ends sentences with \u2018ne\u2019)\n"
    "- Key plot events and the current situation, including important non-verbal "
    "scene changes visible in scene descriptions\n"
    "- The locations and settings where scenes occurred\n"
    "- Any recurring proper nouns: places, items, terms, titles\n\n"
    "Aim for under 400 words. Output only the summary text, no preamble."
)

_DESCRIBE_SYSTEM = (
    "You are a visual scene analyst for a visual novel translator. "
    "You will be given a screenshot, story context, and the current dialogue line. "
    "Ignore all text rendered in the image (dialogue boxes, name plates, UI elements).\n\n"
    "Before drawing any conclusions, carefully examine the screenshot: "
    "note every character visible, their exact facial expressions and body postures, "
    "the background setting, and the overall mood or atmosphere. "
    "Use the story context to identify who is who.\n\n"
    "When [Previous scene:] appears in the dialogue history: "
    "compare your examination against it and describe ONLY what has changed — "
    "characters appearing or leaving, expression or pose changes, new setting, mood shifts. "
    "If the scene is genuinely unchanged, say so in one sentence. "
    "Do not restate anything that is the same.\n\n"
    "When no [Previous scene:] appears in the dialogue history: "
    "describe what you observed — who is visible and their emotional state, "
    "the setting and mood, and any character identity clues (2–3 sentences).\n\n"
    "Output only the final description. No preamble, no labels."
)


class Translator:
    """
    Translates Japanese VN dialogue using independently configurable providers
    for three roles: translator, descriptor (vision/scene), and summarizer.

    Supported providers: anthropic, openai, google, ollama.
    Google and Ollama both use the OpenAI SDK (OpenAI-compatible endpoints).
    Prompt caching is applied to the system prompt for the Anthropic translator path.
    """

    def __init__(self, cfg: Config, context: ContextManager) -> None:
        self._cfg = cfg
        self._context = context
        self._clients: dict[str, object] = {}
        self.last_compact_error: bool = False

        for provider in {cfg.translator_provider, cfg.descriptor_provider,
                         cfg.summarizer_provider}:
            self._clients[provider] = self._build_client(provider)


    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------

    def _build_client(self, provider: str) -> object:
        cfg = self._cfg
        if provider == "anthropic":
            return anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)
        import openai as _openai
        if provider == "openai":
            return _openai.AsyncOpenAI(api_key=cfg.openai_api_key)
        if provider == "google":
            return _openai.AsyncOpenAI(
                api_key=cfg.google_api_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            )
        if provider == "ollama":
            return _openai.AsyncOpenAI(
                base_url=cfg.ollama_base_url,
                api_key="ollama",
            )
        raise ValueError(f"Unknown provider: {provider!r}")

    def rebuild_clients(self) -> None:
        """Rebuild API clients from current config. Call after key/URL/provider changes."""
        self._clients = {}
        for provider in {self._cfg.translator_provider, self._cfg.descriptor_provider,
                         self._cfg.summarizer_provider}:
            self._clients[provider] = self._build_client(provider)

    # ------------------------------------------------------------------
    # Unified API call dispatcher
    # ------------------------------------------------------------------

    async def _call(
        self,
        provider: str,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int,
        use_cache: bool = False,
        think: bool = True,
    ) -> str:
        client = self._clients[provider]

        if provider == "anthropic":
            if use_cache:
                system_arg = [{"type": "text", "text": system,
                               "cache_control": {"type": "ephemeral"}}]
                # Cache the conversation history prefix by marking the last
                # assistant message. On subsequent calls the entire prefix up
                # to that point is served from cache at 10× lower cost.
                messages = list(messages)  # shallow copy — don't mutate caller's list
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i]["role"] == "assistant":
                        content = messages[i]["content"]
                        if isinstance(content, str):
                            messages[i] = {
                                "role": "assistant",
                                "content": [{"type": "text", "text": content,
                                             "cache_control": {"type": "ephemeral"}}],
                            }
                        elif isinstance(content, list) and content:
                            messages[i] = dict(messages[i])
                            last_block = dict(content[-1])
                            last_block["cache_control"] = {"type": "ephemeral"}
                            messages[i]["content"] = list(content[:-1]) + [last_block]
                        break
            else:
                system_arg = system
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_arg,
                messages=messages,
            )
            text = response.content[0].text.strip()
            if response.stop_reason != "end_turn":
                logger.warning("anthropic/%s stop_reason=%r", model, response.stop_reason)
            return text
        else:
            # OpenAI-compatible path (openai, google, ollama)
            full_messages = [{"role": "system", "content": system}] + messages
            token_kwarg = "max_completion_tokens" if provider == "openai" else "max_tokens"
            kwargs: dict = dict(model=model, messages=full_messages)
            kwargs[token_kwarg] = max_tokens
            if provider == "ollama" and not think:
                kwargs["extra_body"] = {"think": False}
            response = await client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            content = choice.message.content or ""
            if not content:
                logger.warning(
                    "%s/%s returned empty content (finish_reason=%r)",
                    provider, model, choice.finish_reason,
                )
            content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
            return content

    # ------------------------------------------------------------------
    # Image content format adapter
    # ------------------------------------------------------------------

    def _image_content(self, provider: str, b64: str) -> dict:
        if provider == "anthropic":
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64,
                },
            }
        else:
            return {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def translate(self, jp_text: str) -> str:
        """Translate a Japanese line and update the context."""
        if self._context.needs_summarization():
            await self._summarize_context()

        messages = self._context.get_messages(jp_text)
        provider = self._cfg.translator_provider
        model    = self._cfg.translator_model
        max_tok  = self._cfg.translator_max_tokens

        think    = self._cfg.translator_ollama_thinking
        system   = self._cfg.translator_system_prompt or _SYSTEM_PROMPT
        result = await self._call(
            provider, model, system, messages, max_tokens=max_tok,
            use_cache=(provider == "anthropic"), think=think,
        )

        self._write_translator_log(messages, result)
        if not result:
            logger.warning("Empty result for %r, retrying once…", jp_text)
            result = await self._call(
                provider, model, system, messages, max_tokens=max_tok,
                use_cache=(provider == "anthropic"), think=think,
            )
            self._write_translator_log(messages, result)

        if result:
            self._context.add_line(jp_text, result)
        else:
            logger.warning("Translator returned empty result for %r after retry — skipping context write", jp_text)
        logger.debug("Translated: %r -> %r", jp_text, result)
        return result

    async def describe_scene(self, screenshot_b64: str, jp_text: str) -> str:
        """
        Call the descriptor model with a screenshot to produce a text description
        of the new scene.  Works with any provider whose model supports vision.
        Uses the full story context so the description is informed by prior events.
        """
        provider = self._cfg.descriptor_provider
        model    = self._cfg.descriptor_model

        context_parts = []
        if self._context.summary:
            context_parts.append(f"[Story summary]\n{self._context.summary}")
        if self._context.history:
            hist_parts = []
            last_scene_idx = None
            for l in self._context.history:
                if l.scene_description is not None:
                    last_scene_idx = len(hist_parts)
                    hist_parts.append(f"[Scene: {l.scene_description}]")
                hist_parts.append(f"JP: {l.jp}\nEN: {l.en}")
            if last_scene_idx is not None:
                # Rename the most-recent [Scene:] tag to [Previous scene:] so the
                # model understands it describes the scene before the current screenshot.
                # No separate duplicate block is needed.
                hist_parts[last_scene_idx] = hist_parts[last_scene_idx].replace(
                    "[Scene:", "[Previous scene:", 1
                )
            context_parts.append("[Dialogue history]\n" + "\n".join(hist_parts))

        context_hint = "\n\n".join(context_parts) + "\n\n" if context_parts else ""

        messages = [{
            "role": "user",
            "content": [
                self._image_content(provider, screenshot_b64),
                {"type": "text", "text": (
                    f"{context_hint}"
                    f"Dialogue line currently on screen: {jp_text}"
                )},
            ],
        }]

        description = await self._call(
            provider, model, self._cfg.descriptor_system_prompt or _DESCRIBE_SYSTEM, messages,
            max_tokens=self._cfg.descriptor_max_tokens,
            think=self._cfg.descriptor_ollama_thinking,
        )
        return description

    async def _summarize_context(self) -> None:
        """Ask the summarizer model to compress the oldest context lines."""
        self.last_compact_error = False
        logger.info("Context threshold reached — compacting...")
        try:
            msgs     = self._context.build_summarization_messages()
            provider = self._cfg.summarizer_provider
            model    = self._cfg.summarizer_model

            new_summary = await self._call(
                provider, model, self._cfg.summarizer_system_prompt or _SUMMARIZE_SYSTEM, msgs,
                max_tokens=self._cfg.summarizer_max_tokens,
                think=self._cfg.summarizer_ollama_thinking,
            )
            self._context.apply_summarization(new_summary)
            logger.info("Context compacted successfully.")
        except Exception as exc:
            self.last_compact_error = True
            logger.error("Context compaction failed: %s", exc)

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _write_translator_log(self, messages: list[dict], output: str) -> None:
        """Overwrite llm_calls.log with the most recent translator call as a play script."""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"{ts}\n" + "─" * 72 + "\n"]

        i = 0
        while i < len(messages) - 1:
            u = messages[i]
            a = messages[i + 1]
            if u["role"] != "user" or a["role"] != "assistant":
                i += 1
                continue
            content = u.get("content", "")
            if content.startswith("[Context summary of earlier dialogue]"):
                summary = content.split("\n", 1)[1] if "\n" in content else content
                parts.append(f"\n[Summary]\n{summary}\n")
            else:
                parts.append(f"\n{content}\n{a.get('content', '')}\n")
            i += 2

        if messages and messages[-1]["role"] == "user":
            content = messages[-1].get("content", "")
            parts.append(f"\n{content}\n{output}\n")

        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("w", encoding="utf-8") as f:
                f.write("".join(parts))
        except OSError:
            logger.warning("Failed to write LLM call log.")
