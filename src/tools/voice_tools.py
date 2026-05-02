"""Voice profile: sample the user's sent mail and synthesise a style card.

The card is stored under ``NS.VOICE`` ("voice.profile", key "current") and
other tools (``suggest_replies``, ``create_draft``, reply / forward drafts)
read it via :class:`GetVoiceProfileTool` to render drafts in a tone
consistent with the mailbox owner's actual writing.

Security & cost
---------------
* Samples are capped at 200 messages and 12 KiB per message; the prompt
  is hard-capped at ~30 KiB of user text total (tokens ~ chars/4).
* The AI call is read-only (no side effects on Exchange).
* The resulting card is small (a few KB of JSON) and lives in the
  per-mailbox memory store.
* Only the mailbox owner's ``Sent`` folder is sampled. Impersonated
  mailboxes are NOT sampled — the voice profile is personal to the
  primary authenticated user.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..memory import VoiceProfile, VoiceRepo
from ..utils import format_success_response, safe_get


_SYSTEM_PROMPT = (
    "You analyse the writing style of a mailbox owner based on samples of "
    "their sent emails. Output STRICT JSON matching this shape:\n"
    "{\n"
    '  "formality": "casual" | "professional" | "formal",\n'
    '  "avg_length_words": integer,\n'
    '  "common_greetings": [string, ...],   // up to 5\n'
    '  "common_signoffs": [string, ...],    // up to 5\n'
    '  "typical_structure": string,         // 1-3 sentence description\n'
    '  "examples": [string, ...]            // 3 short excerpts (<= 200 chars each)\n'
    "}\n"
    "Infer from the samples only; do not invent patterns that are not present. "
    "Never include PII (names, email addresses, phone numbers, account "
    "numbers) in the output — paraphrase or redact."
)


def _body_text(message) -> str:
    text = safe_get(message, "text_body", "") or safe_get(message, "body", "") or ""
    return str(text)


def _clean_body(text: str) -> str:
    """Strip quoted replies, signatures, and HTML from a sent-mail sample."""
    if not text:
        return ""
    # Drop anything after common quoted-reply markers.
    cutoffs = [
        r"\n[-_]{2,}\s*Original Message\s*[-_]{2,}",
        r"\nFrom:\s.+?\nSent:",
        r"\nOn .+ wrote:",
        r"\n> ",
    ]
    for pattern in cutoffs:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            text = text[: match.start()]
    # Strip HTML tags (rough — we don't need full parsing for a sample).
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text[:2000]


class GetVoiceProfileTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_voice_profile",
            "description": (
                "Return the currently stored voice profile (if any). Drafting "
                "tools use this to write in the user's tone."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        repo = VoiceRepo(self.get_memory_store())
        profile = repo.get()
        if profile is None:
            return format_success_response(
                "No voice profile stored yet",
                has_profile=False,
            )
        return format_success_response(
            "Voice profile fetched",
            has_profile=True,
            profile=profile.to_dict(),
        )
