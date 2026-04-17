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


class BuildVoiceProfileTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "build_voice_profile",
            "description": (
                "Sample the mailbox owner's Sent folder and produce a short "
                "style card (formality, greetings, sign-offs, typical "
                "structure, 3 short examples). Stored for later use by draft "
                "tools. Requires ENABLE_AI=true."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sample_count": {
                        "type": "integer",
                        "minimum": 20,
                        "maximum": 200,
                        "default": 100,
                        "description": "Number of recent Sent messages to sample",
                    },
                    "min_words": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 200,
                        "default": 20,
                        "description": "Skip messages shorter than this many words",
                    },
                },
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        from ..ai import get_ai_provider
        from ..ai.base import Message as AIMessage

        sample_count = int(kwargs.get("sample_count", 100))
        min_words = int(kwargs.get("min_words", 20))

        provider = get_ai_provider(self.ews_client.config)
        if provider is None:
            raise ToolExecutionError(
                "AI provider not configured. Set ENABLE_AI=true to build a voice profile."
            )

        account = self.ews_client.account
        sent = getattr(account, "sent", None)
        if sent is None:
            raise ToolExecutionError("Sent folder is not available on this account")

        # Pull up to sample_count recent messages from Sent, filter for
        # sane-length text content, cap the prompt budget at ~30 KiB.
        samples: List[str] = []
        try:
            for item in sent.all().order_by("-datetime_sent")[: sample_count * 2]:
                cleaned = _clean_body(_body_text(item))
                if not cleaned:
                    continue
                if len(cleaned.split()) < min_words:
                    continue
                samples.append(cleaned[:1500])
                if len(samples) >= sample_count:
                    break
        except Exception as exc:
            raise ToolExecutionError(f"Failed to read Sent folder: {exc}")

        if len(samples) < 5:
            raise ToolExecutionError(
                f"Not enough sent-mail samples to build a profile "
                f"(found {len(samples)}, need at least 5)"
            )

        # Build prompt with a hard size cap.
        prompt_budget = 30_000
        joined = ""
        used = 0
        for idx, sample in enumerate(samples, start=1):
            chunk = f"\n--- SAMPLE {idx} ---\n{sample}\n"
            if used + len(chunk) > prompt_budget:
                break
            joined += chunk
            used += len(chunk)

        user_prompt = (
            f"Here are {samples.__len__()} samples from the user's Sent folder. "
            f"Produce the style card as JSON.\n{joined}"
        )

        response = await provider.complete(
            messages=[
                AIMessage(role="system", content=_SYSTEM_PROMPT),
                AIMessage(role="user", content=user_prompt),
            ],
            max_tokens=1024,
            temperature=0.2,
        )
        raw = getattr(response, "content", "") or ""
        parsed = self._parse_json_payload(raw)

        try:
            profile = VoiceProfile(
                sampled_at=__import__("time").time(),
                sample_count=len(samples),
                formality=str(parsed.get("formality", "professional")),
                avg_length_words=int(parsed.get("avg_length_words", 0)) or 0,
                common_greetings=[str(x) for x in (parsed.get("common_greetings") or [])][:5],
                common_signoffs=[str(x) for x in (parsed.get("common_signoffs") or [])][:5],
                typical_structure=str(parsed.get("typical_structure", "")),
                examples=[str(x)[:200] for x in (parsed.get("examples") or [])][:5],
            )
        except (ValueError, TypeError) as exc:
            raise ToolExecutionError(f"AI returned an invalid voice profile: {exc}")

        repo = VoiceRepo(self.get_memory_store())
        repo.save(profile)

        return format_success_response(
            "Voice profile built",
            profile=profile.to_dict(),
        )

    @staticmethod
    def _parse_json_payload(raw: str) -> Dict[str, Any]:
        if not raw:
            return {}
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
        if fence:
            raw = fence.group(1)
        brace = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        candidate = brace.group(0) if brace else raw
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


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
