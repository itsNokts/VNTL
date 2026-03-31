from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path

import anthropic

from config import Config
from context_manager import ContextManager

logger = logging.getLogger(__name__)

_LOG_DIR          = Path.home() / ".config" / "vntl"
_TRANSLATE_LOG    = _LOG_DIR / "translate.log"
_DESCRIBE_LOG     = _LOG_DIR / "describe.log"
_SUMMARIZE_LOG    = _LOG_DIR / "summarize.log"
_MAX_LOG_ENTRIES  = 5


_SYSTEM_PROMPT = """\
You are an expert Japanese-to-English visual novel translator. Produce fluent, \
voice-preserving English for each line you are given.

The conversation you receive will include:
1. A SUMMARY injected as an earlier exchange \u2014 a reference document with established \
character voices, terminology, and story context. Treat it as ground truth.
2. HISTORY \u2014 recent JP\u2192EN pairs as conversation turns. These are your translation memory: \
match established character voices, name spellings, and terminology exactly.
3. A line marked [TRANSLATE THIS:] \u2014 this is your sole input. Translate it directly \
and completely. Do not predict, continue, or infer beyond what is written there.

## Voice and Register
- Each character has a distinct speech register (formal, casual, rough, archaic, childlike, \
etc.). Maintain it consistently based on the summary and history.
- Preserve the effect of verbal tics and sentence-ending patterns in natural English rather \
than transliterating them. For example, a character who ends lines with \u3063\u3059 should come \
across as clipped and eager, not literally say "-ssu."
- Preserve Japanese honorifics (-san, -kun, -chan, -senpai, etc.) as they encode social \
relationships.
- For narration, use flowing literary prose. For dialogue, use natural spoken English.

## Subject Inference
Japanese frequently omits subjects. When the subject is unstated, use the scene description, \
conversation flow, and character relationships to infer it. Commit to the most natural \
reading \u2014 do not hedge or leave it ambiguous.

## Scene Context
A [Scene:] header may appear in the history. It describes who appears to be visible on screen, \
their expressions, and the setting at that point. It remains valid until the next [Scene:] \
header. Use it as a hint for speaker identity, tone, and emotional state, but if it conflicts \
with what the dialogue itself implies (e.g. a character is named in the scene but the speech \
pattern clearly belongs to someone else), trust the dialogue and history over the scene \
description. Never reference the scene description explicitly in your output.

## Speaker Names
- If the JP line includes a speaker prefix (e.g. \u300c\u6728\u6751\uff1a\u300d), romanize or translate the name \
and keep the prefix (e.g. "Kimura:"). Do not add speaker labels that are not in the source.
- Some VN engines emit the speaker name as a standalone line before dialogue. If the input \
is a bare name with no sentence structure, output only its romanization.

## Formatting
- Convert Japanese ellipses (\u2026\u2026) to English ellipses (\u2026).
- Preserve dramatic dashes (\u2014\u2014) for interrupted or trailing speech.

## Accuracy Rules
- Translate what is there. Do not add, embellish, or infer content beyond what the Japanese \
line says. If a line is terse, the English should be terse.
- Your translation must derive solely from the marked Japanese text \u2014 not from predicting \
what would logically follow in the story.
- If a line is genuinely ambiguous even with context, prefer the simplest interpretation \
that fits the scene.
- Romanize Japanese words only when no natural English equivalent exists \
(e.g. kotatsu, tatami).

Output ONLY the English translation. No notes, no explanations, no wrapper quotes.\
"""

_SUMMARIZE_SYSTEM = """\
You are maintaining a running translation reference document for a Japanese visual novel \
translator. This document is the translator's sole source of long-term context \u2014 every \
future line will be translated using it, so it must contain everything needed to stay \
perfectly consistent.

The user message contains:
1. A PREVIOUS SUMMARY (if one exists) \u2014 treat it as the best understanding so far, not \
as infallible truth. It was written by an LLM working with incomplete information and may \
contain errors.
2. A HISTORY \u2014 recent lines (JP\u2192EN pairs) with occasional [Scene:] headers describing \
the visual state.

Your job is to ACCUMULATE and CORRECT:
- Add new information from the history.
- Update what has changed.
- If new dialogue reveals that something in the previous summary was wrong or based on a \
misunderstanding (e.g. a "sister" turns out to be a cousin, a character's motive is \
recontextualized), correct it. Do not preserve information you now have reason to believe \
is wrong.
- When something is implied but not confirmed (e.g. a character seems to be hiding \
something, a relationship is ambiguous), mark it as uncertain \
(e.g. "possibly rivals \u2014 hinted but not confirmed").
- Only mark something as uncertain if there is genuine ambiguity. If the text has stated \
something clearly and directly, record it as fact.
- NEVER compress or remove details that are still believed to be accurate, especially \
character voice details and terminology.

## Characters
For every character who has appeared:
- Name (all aliases, nicknames, and honorifics others use for them)
- Role and relationships to other characters (family, friends, rivals, romantic interest, \
hierarchy) \u2014 note confidence level if based on implication rather than explicit statement
- Physical appearance (if known)
- Personality traits that affect how they speak
- Speech register: formal / casual / rough / archaic / childish / etc.
- First-person pronoun (ore / boku / watashi / atashi / etc.) and signature verbal tics or \
sentence-final particles (be specific: "uses \u3063\u3059, drops \u3060 in casual speech" not "speaks casually")
- Established English renderings of their name, catchphrases, or unique expressions

Keep character entries focused on STATIC PROPERTIES: identity, appearance, speech patterns, \
relationship status, and translation conventions. Do NOT record story events here, even if \
they are character-defining moments — those belong in Story So Far or Current Situation. \
The Characters section should grow only when new characters are introduced, not as the \
story progresses.

When a relationship status changes (rivals → allies, strangers → friends), update the \
relationship descriptor in-place. Do not append narrative about what caused the change. \
Example: change "rivals" to "reluctant allies (as of the shrine confrontation)" — not a \
paragraph about the confrontation itself. If a relationship is recontextualized by new \
information, correct the earlier understanding rather than keeping both versions.

## Current Situation
- Where the characters are right now and what is immediately happening
- The active goal or conflict driving the current scene
- The most recent visual scene description (from the last [Scene:] header)
- Unresolved threads, promises, or mysteries

## Story So Far
- Key past events that inform present motivations or ongoing tension
- Important locations that have been visited
- Significant revelations about the world, characters, or backstory
- Events that no longer affect the current situation may be compressed to a single \
sentence, but never deleted entirely
- If a past event was recorded under a misunderstanding that has since been clarified, \
update it to reflect the corrected understanding

## Terminology & Translation Conventions

Track every translation choice that must stay consistent across lines:
- Proper nouns: character names, place names, organization names, titles, items
- Name order: whether the English output uses given-family or family-given
- Recurring expressions: Japanese phrases that appear repeatedly and their established \
English rendering
- Special handling: puns, wordplay, untranslated titles, or any case where the English \
deliberately departs from a literal translation

For each entry, record the Japanese term, the current English rendering, and enough \
context to justify the choice \
(e.g. "\u5b66\u5712\u9577 \u2014 Headmaster; head of Seiran Academy").

This section is a living document, not a locked-in glossary. Early translations are made \
with limited context and may turn out to be wrong. If new dialogue makes clear that a \
rendering is inaccurate or misleading, update the entry to the better translation.

Never remove terminology entries \u2014 only update them.

## Length Management
The input you are receiving is approximately {current_context_tokens} tokens. \
Target approximately {max_summary_tokens} tokens for your output. If space is tight, apply compression in \
this priority order (most compressible first):
1. Story So Far \u2014 old resolved events can be condensed
2. Current Situation \u2014 prior scene descriptions can be shortened once superseded
3. NEVER compress Characters or Terminology \u2014 these directly affect translation quality. \
Terminology entries may be updated or corrected but never removed. Character entries must \
remain lean (static properties only); if you find event descriptions inside a character \
entry, move them to Story So Far and trim the character entry back to its static attributes.

Guidelines:
- Be specific, not vague. Concrete Japanese-language details (pronouns, particles, tics) \
are more useful to the translator than subjective descriptions.
- Filler chitchat with no lasting impact may be omitted from Story So Far, but anything \
that reveals character, shifts a relationship, or introduces terminology must be kept.
- If the history contains no meaningful new information (e.g. only small talk that adds \
nothing), return the previous summary unchanged with only Current Situation updated.

Output only the document. No preamble, no postscript.\
"""

_DESCRIBE_SYSTEM = """\
You are a visual scene analyst supporting a Japanese visual novel translator. Your \
descriptions help the translator identify speakers, infer omitted subjects, and gauge \
emotional tone.

You will receive:
1. A screenshot from the visual novel.
2. A SUMMARY \u2014 established character information and story context.
3. A HISTORY \u2014 recent dialogue lines (JP\u2192EN pairs), with the most recent scene marked as \
[Previous scene:] and earlier scenes marked as [Scene:].
4. The current dialogue line on screen \u2014 use it as context for the mood and situation.

Ignore all text rendered in the screenshot (dialogue boxes, name plates, UI elements, \
menus). Analyze only the visual scene itself.

## Analysis Process
First, examine the screenshot carefully:
- How many characters are visible?
- For each character: if you can confidently identify them from the summary and history \
(matching hair color, clothing, or context clues), name them. If identification is \
uncertain, describe their appearance and note the closest match (e.g. "girl with long \
black hair \u2014 possibly Yuki based on the school uniform, but not certain"). Never guess a \
name without visual evidence supporting it.
- What is their facial expression, posture, and apparent emotion?
- What is the setting/background? What is the overall mood or lighting?

Then, check the history for a [Previous scene:] description.

## If a [Previous scene:] exists:
Compare against it point by point:
- Characters: has anyone appeared, left, or changed position?
- Expressions/poses: has any character's emotion or posture shifted?
- Setting: has the background or location changed?
- Lighting/mood: has the atmosphere shifted?

Report ONLY the points where something changed. If none of the above have changed, \
respond with exactly: No visual change.

A rephrased description of the same scene is NOT a change. If the same character is in \
the same place, with the same expression, against the same background \u2014 that is no \
change, even if you would word it differently.

## If no [Previous scene:] exists:
Describe the full scene: who is visible and their emotional state, the setting, the mood, \
and any character identity clues. Keep it to 2-3 sentences.

## Visual Novel Conventions
Characters disappearing from the screen does not mean they left the scene. VN engines \
frequently show only the active speaker or a subset of characters present. Use the \
dialogue history to determine who is still part of the scene.

- If a character was visible previously and is no longer rendered but the dialogue implies \
they are still present, note that they are no longer \u2018visible\u2019 rather than that they \
\u2018left\u2019 (e.g. "Yuki is no longer on screen" not "Yuki has left").
- Only state that a character has left if the dialogue explicitly indicates a departure.

## Priorities
- Character identity and count matter most \u2014 the translator needs to know who is on \
screen to resolve pronouns.
- Emotional state matters second \u2014 it informs tone and word choice.
- Background and setting matter third \u2014 useful only when they've changed or are first \
appearing.
- Do not describe decorative details (furniture arrangement, background objects) unless \
they are narratively significant.

Output only the description. No preamble, no labels, no commentary.\
"""


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
            if choice.message is None:
                raise RuntimeError(
                    f"Response blocked or empty (finish_reason={choice.finish_reason!r})"
                )
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
        """Translate a Japanese line and update the context.

        Summarization is the caller's responsibility — call _summarize_context()
        beforehand if context.needs_summarization() is True.
        """
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

        self._log_call("TRANSLATE", provider, model, system, messages, result)
        if not result:
            logger.warning("Empty result for %r, retrying once…", jp_text)
            result = await self._call(
                provider, model, system, messages, max_tokens=max_tok,
                use_cache=(provider == "anthropic"), think=think,
            )
            self._log_call("TRANSLATE", provider, model, system, messages, result)

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

        desc_system = self._cfg.descriptor_system_prompt or _DESCRIBE_SYSTEM
        description = await self._call(
            provider, model, desc_system, messages,
            max_tokens=self._cfg.descriptor_max_tokens,
            think=self._cfg.descriptor_ollama_thinking,
        )
        self._log_call("DESCRIBE", provider, model, desc_system, messages, description)
        return description

    async def _summarize_context(self) -> None:
        """Ask the summarizer model to compress the oldest context lines."""
        self.last_compact_error = False
        logger.info("Context threshold reached — compacting...")
        try:
            msgs     = self._context.build_summarization_messages()
            provider = self._cfg.summarizer_provider
            model    = self._cfg.summarizer_model

            system = (self._cfg.summarizer_system_prompt or _SUMMARIZE_SYSTEM).replace(
                "{max_summary_tokens}", str(self._cfg.summary_max_tokens)
            ).replace(
                "{current_context_tokens}", str(self._context.estimated_tokens)
            )
            new_summary = await self._call(
                provider, model, system, msgs,
                max_tokens=self._cfg.summarizer_max_tokens,
                think=self._cfg.summarizer_ollama_thinking,
            )
            self._context.apply_summarization(new_summary)
            self._log_call("SUMMARIZE", provider, model, system, msgs, new_summary)
            logger.info("Context compacted successfully.")
        except Exception as exc:
            self.last_compact_error = True
            logger.error("Context compaction failed: %s", exc)

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log_call(
        self,
        call_type: str,
        provider: str,
        model: str,
        system: str,
        messages: list[dict],
        output: str,
    ) -> None:
        """Append one LLM call entry to the appropriate role log file."""
        path = {"TRANSLATE": _TRANSLATE_LOG, "DESCRIBE": _DESCRIBE_LOG,
                "SUMMARIZE": _SUMMARIZE_LOG}.get(call_type, _TRANSLATE_LOG)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        HDR = "──────"
        SEP = "═" * 72

        parts: list[str] = []
        parts.append(f"\n{SEP}\n")
        parts.append(f"  {call_type}  ·  {ts}  ·  {provider}/{model}\n")
        parts.append(f"{SEP}\n")

        parts.append(f"\nSYSTEM\n{HDR}\n{system}\n")

        if call_type == "TRANSLATE":
            context_lines: list[str] = []
            i = 0
            while i < len(messages) - 1:
                u = messages[i]
                a = messages[i + 1]
                if u["role"] != "user" or a["role"] != "assistant":
                    i += 1
                    continue
                content = u.get("content", "")
                if content.startswith("[Context summary of earlier dialogue]"):
                    summary_body = content.split("\n", 1)[1] if "\n" in content else content
                    context_lines.append(f"[Summary]\n{summary_body}")
                else:
                    context_lines.append(f"{content}\n{a.get('content', '')}")
                i += 2
            if context_lines:
                parts.append(f"\nCONTEXT\n{HDR}\n" + "\n\n".join(context_lines) + "\n")
            if messages and messages[-1]["role"] == "user":
                parts.append(f"\nINPUT\n{HDR}\n{messages[-1].get('content', '')}\n")

        elif call_type == "DESCRIBE":
            input_parts: list[str] = []
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") in ("image", "image_url"):
                                input_parts.append("[JPEG image]")
                            elif block.get("type") == "text":
                                input_parts.append(block.get("text", ""))
                else:
                    input_parts.append(str(content))
            parts.append(f"\nINPUT\n{HDR}\n" + "\n\n".join(input_parts) + "\n")

        else:  # SUMMARIZE
            if messages:
                parts.append(f"\nINPUT\n{HDR}\n{messages[0].get('content', '')}\n")

        parts.append(f"\nOUTPUT\n{HDR}\n{output}\n")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_entry = "".join(parts)
            # Rolling window: keep only the last _MAX_LOG_ENTRIES entries.
            # Entries are delimited by the ═-line that starts each block.
            existing: list[str] = []
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                # Split on the separator; first element is empty (file starts with \n═══)
                sep = "\n" + "═" * 72
                chunks = raw.split(sep)
                existing = [sep + c for c in chunks if c.strip()]
            kept = existing[-(  _MAX_LOG_ENTRIES - 1):] if existing else []
            with path.open("w", encoding="utf-8") as f:
                f.write("".join(kept) + new_entry)
        except OSError:
            logger.warning("Failed to write LLM call log.")
