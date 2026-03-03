from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DialogueLine:
    jp: str
    en: str
    scene_description: str | None = None  # Non-None = first line of a new scene


class ContextManager:
    """
    Maintains a rolling conversation history of translated VN dialogue.

    Works similarly to Claude Code's compact context:
    - Keeps all recent lines verbatim as multi-turn messages.
    - When estimated token usage crosses SUMMARIZE_AT_TOKENS, the oldest
      lines are summarized by Claude into a compact narrative summary.
    - The summary (plus the most recent MIN_RECENT_LINES lines) is then
      used as the context for subsequent translations.
    - This lets the translator preserve character voices, pronouns, and
      plot context across an entire VN session with no hard cutoff.
    """

    # Rough token estimates (good enough for triggering; not used for billing)
    _JP_CHARS_PER_TOKEN: float = 3.0
    _EN_CHARS_PER_TOKEN: float = 4.0

    def __init__(
        self,
        summarize_at_tokens: int = 20_000,
        min_recent_lines: int = 15,
    ) -> None:
        self._summarize_at_tokens = summarize_at_tokens
        self._min_recent_lines    = min_recent_lines
        self.history: list[DialogueLine] = []
        self.summary: str | None = None
        self._estimated_tokens: int = 0
        self.current_scene_description: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_line(self, jp: str, en: str) -> None:
        """Record a translated dialogue line and update token estimates.

        If current_scene_description is set, it is stored on this line (marking
        it as the first line of a new scene) and then cleared.
        """
        line = DialogueLine(jp=jp, en=en, scene_description=self.current_scene_description)
        self.current_scene_description = None  # consumed — subsequent lines in same scene get None
        self.history.append(line)
        self._estimated_tokens += self._estimate_line_tokens(line)

    def clear(self) -> None:
        """Discard all history and the summary, resetting to a blank session."""
        self.history = []
        self.summary = None
        self._estimated_tokens = 0
        self.current_scene_description = None

    def update_thresholds(self, summarize_at_tokens: int, min_recent_lines: int) -> None:
        """Update summarization thresholds. Takes effect immediately."""
        self._summarize_at_tokens = summarize_at_tokens
        self._min_recent_lines    = min_recent_lines

    def pop_last_line_if_matches(self, jp: str) -> bool:
        """Remove the last history entry if its JP text matches jp.

        Called before a retry so the old translation is replaced, not duplicated.
        If the popped line carried a scene description (it was the first line of a
        new scene), restore it to current_scene_description so the retry translation
        still sees the correct visual context.
        Returns True when the entry was removed.
        """
        if self.history and self.history[-1].jp == jp:
            removed = self.history.pop()
            self._estimated_tokens -= self._estimate_line_tokens(removed)
            if removed.scene_description is not None and self.current_scene_description is None:
                self.current_scene_description = removed.scene_description
            return True
        return False

    def needs_summarization(self) -> bool:
        return (
            self._estimated_tokens >= self._summarize_at_tokens
            and len(self.history) > self._min_recent_lines
        )

    def get_messages(self, current_jp: str) -> list[dict]:
        """
        Build the Claude API messages list for a translation request.

        Structure:
          [optional summary injection as user+assistant pair]
          [history lines as user (JP) / assistant (EN) pairs]
          [current JP line as final user message]
        """
        messages: list[dict] = []

        if self.summary:
            messages.append({
                "role": "user",
                "content": f"[Context summary of earlier dialogue]\n{self.summary}",
            })
            messages.append({
                "role": "assistant",
                "content": "Understood. I'll use this context for the following translations.",
            })

        for line in self.history:
            if line.scene_description is not None:
                user_content = f"[Scene: {line.scene_description}]\n{line.jp}"
            else:
                user_content = line.jp
            messages.append({"role": "user", "content": user_content})
            messages.append({"role": "assistant", "content": line.en})

        if self.current_scene_description:
            content = f"[Scene: {self.current_scene_description}]\n{current_jp}"
        else:
            content = current_jp
        messages.append({"role": "user", "content": content})
        return messages

    def apply_summarization(self, new_summary: str) -> None:
        """
        Replace the oldest history lines with a new combined summary.
        Called by Translator after receiving the summary from Claude.
        """
        lines_to_keep = self.history[-self._min_recent_lines:]
        lines_summarized = self.history[:-self._min_recent_lines]

        logger.info(
            "Compacting context: summarized %d lines, keeping %d verbatim.",
            len(lines_summarized),
            len(lines_to_keep),
        )

        self.summary = new_summary
        self.history = lines_to_keep

        # Recalculate token estimate from scratch
        self._estimated_tokens = self._estimate_summary_tokens(new_summary)
        for line in self.history:
            self._estimated_tokens += self._estimate_line_tokens(line)

    def build_summarization_messages(self) -> list[dict]:
        """
        Build a messages list asking the summarizer to compress the current context.
        Returns the messages to send (without the system prompt).
        """
        lines_to_summarize = self.history[:-self._min_recent_lines]
        parts = []
        for line in lines_to_summarize:
            if line.scene_description is not None:
                parts.append(f"[Scene: {line.scene_description}]")
            parts.append(f"[JP] {line.jp}\n[EN] {line.en}")
        history_text = "\n".join(parts)

        if self.summary:
            prompt = (
                f"Previous summary:\n\n{self.summary}\n\n"
                f"New dialogue to incorporate:\n\n{history_text}\n\n"
                f"Write an updated combined summary."
            )
        else:
            prompt = f"Dialogue to summarise:\n\n{history_text}"

        return [{"role": "user", "content": prompt}]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize history and summary to a plain dict (JSON-safe)."""
        return {
            "summary": self.summary,
            "history": [
                {"jp": line.jp, "en": line.en, "scene_description": line.scene_description}
                for line in self.history
            ],
        }

    def load_from_dict(self, data: dict) -> None:
        """Replace current state with data previously produced by to_dict()."""
        self.history = [
            DialogueLine(jp=d["jp"], en=d["en"], scene_description=d.get("scene_description"))
            for d in data.get("history", [])
        ]
        self.summary = data.get("summary")
        self._estimated_tokens = sum(self._estimate_line_tokens(l) for l in self.history)
        if self.summary:
            self._estimated_tokens += self._estimate_summary_tokens(self.summary)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _estimate_line_tokens(self, line: DialogueLine) -> int:
        jp_tokens = len(line.jp) / self._JP_CHARS_PER_TOKEN
        en_tokens = len(line.en) / self._EN_CHARS_PER_TOKEN
        scene_tokens = (
            int(len(line.scene_description) / self._EN_CHARS_PER_TOKEN) + 4
            if line.scene_description is not None else 0
        )
        return int(jp_tokens + en_tokens) + scene_tokens + 8  # +8 for message overhead

    def _estimate_summary_tokens(self, summary: str) -> int:
        return int(len(summary) / self._EN_CHARS_PER_TOKEN) + 16
