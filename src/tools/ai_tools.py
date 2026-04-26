"""AI-powered tools for EWS MCP Server."""

import re
from typing import Any, Dict, List, Optional
from .base import BaseTool
from ..exceptions import EmbeddingError, ToolExecutionError
from ..utils import (
    format_success_response, safe_get, find_message_for_account,
    project_fields, strip_body_by_default, LIST_DEFAULT_FIELDS,
    is_automated_sender, AUTOMATED_SUBJECT_PREFIXES,
)
from ..ai import get_ai_provider, get_embedding_provider, EmailClassificationService, EmbeddingService


_TARGET_MAILBOX_SCHEMA = {
    "target_mailbox": {
        "type": "string",
        "description": "Email address to operate on (requires EWS_IMPERSONATION_ENABLED=true)"
    }
}


def _id_from_doc(doc: Dict[str, Any]) -> str:
    """Best-effort stable string identifier for a semantic-search hit."""
    raw = doc.get("id") or doc.get("message_id") or ""
    if hasattr(raw, "id"):
        raw = raw.id
    return str(raw or "")


def _embedding_error_hint(exc_msg: str) -> str:
    """Return an actionable hint for an EmbeddingError message.

    Two distinct failure modes deserve distinct hints — a connection failure
    ("unreachable" / "All connection attempts failed" / connect refused /
    timeout) almost always means the AI_BASE_URL host isn't routable from
    inside the container (typical bridge-network-to-host-LAN-IP trap). A
    404/400/model-not-found means the model name is wrong. Conflating both
    under the same generic "check your model" hint sent operators on long
    debug detours (see fix/ai-docker-networking).
    """
    msg = (exc_msg or "").lower()
    is_unreachable = (
        "unreachable" in msg
        or "all connection attempts failed" in msg
        or ("connect" in msg and ("refused" in msg or "timeout" in msg or "timed out" in msg))
    )
    if is_unreachable:
        return (
            "AI_BASE_URL host is not reachable from this process. "
            "If running in Docker, the host's LAN IP is NOT routable from "
            "inside a bridge network. Two options: "
            "(1) RECOMMENDED — attach both ews-mcp and the Ollama "
            "container to a shared external network "
            "(`docker network create claude-shared`, then declare it as "
            "external in both compose files), and set "
            "AI_BASE_URL=http://ollama:11434/v1 (Docker DNS); "
            "(2) FALLBACK — add `extra_hosts: "
            "'host.docker.internal:host-gateway'` to the ews-mcp service "
            "and set AI_BASE_URL=http://host.docker.internal:11434/v1. "
            "If running with network_mode: host or bare-metal, use "
            "http://localhost:11434/v1."
        )
    return (
        "Verify AI_EMBEDDING_MODEL matches an installed model at "
        "AI_BASE_URL (e.g. 'text-embedding-3-small' for OpenAI, "
        "'nomic-embed-text' for Ollama)."
    )


class SemanticSearchEmailsTool(BaseTool):
    """Tool for semantic search across emails."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "semantic_search_emails",
            "description": "Search emails using semantic similarity (AI-powered natural language search)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query"
                    },
                    "folder": {
                        "type": "string",
                        "description": "Folder to search in",
                        "default": "inbox",
                        "enum": ["inbox", "sent", "drafts", "archive", "all"]
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 50
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Minimum similarity threshold (0.0-1.0)",
                        "default": 0.7,
                        "minimum": 0.0,
                        "maximum": 1.0
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Result projection. Default items include "
                            "message_id, id (deprecated alias), subject, from, "
                            "received_time, snippet, similarity_score. Pass "
                            "['body'] to opt into the full message body."
                        ),
                    },
                    "exclude_automated": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Filter out likely-automated senders (no-reply@, "
                            "notifications@, mailer-daemon, and "
                            "'Accepted:' / 'Canceled:' / 'Automatic reply:' "
                            "subjects) before similarity ranking. Set false to "
                            "include everything."
                        ),
                    },
                    **_TARGET_MAILBOX_SCHEMA
                },
                "required": ["query"]
            }
        }

    # Per-call cap on how many emails we embed on demand. Anything beyond
    # this is silently sampled (newest-first). Separate from the warmup
    # job which populates the cache at startup (Bug 4).
    _PER_CALL_EMBED_CAP: int = 50

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute semantic search."""
        query = kwargs.get("query")
        folder_name = kwargs.get("folder", "inbox").lower()
        max_results = kwargs.get("max_results", 10)
        threshold = kwargs.get("threshold", 0.7)
        target_mailbox = kwargs.get("target_mailbox")
        # Bug 6: result projection. Includes two cheap Bug-7/Bug-8 fields
        # (duplicate_count, is_automated) so the agent can reason about
        # "this was collapsed from N hits" / "this is an automated
        # sender" without a second round trip.
        fields = kwargs.get("fields") or [
            "message_id", "id", "subject", "from", "received_time",
            "snippet", "similarity_score",
            "duplicate_count", "is_automated",
        ]
        # Bug 8: default ON for semantic search (agent-facing workflow).
        exclude_automated = bool(kwargs.get("exclude_automated", True))

        if not query:
            raise ToolExecutionError("query is required")

        try:
            # Get embedding provider
            embedding_provider = get_embedding_provider(self.ews_client.config)
            if not embedding_provider:
                raise ToolExecutionError("Semantic search not enabled. Set enable_ai=true and enable_semantic_search=true")

            embedding_service = EmbeddingService(embedding_provider, cache_dir="data/embeddings")

            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folder_map = {
                "inbox": account.inbox,
                "sent": account.sent,
                "drafts": account.drafts,
                "archive": getattr(account, 'archive', account.inbox),
                "all": account.inbox,  # Placeholder; "all" maps to inbox today
            }

            folder = folder_map.get(folder_name, account.inbox)

            # Fetch recent emails. Pull more than the per-call cap so that
            # after exclude_automated filtering we still have useful
            # candidates, but cap embedding work below.
            raw_limit = min(200, self._PER_CALL_EMBED_CAP * 3)
            emails = list(folder.all().order_by('-datetime_received')[:raw_limit])

            # Prepare documents for search. Apply exclude_automated BEFORE
            # embedding so we don't pay the embedding cost for noise.
            documents: List[Dict[str, Any]] = []
            filtered_out = 0
            for email in emails:
                subject = safe_get(email, 'subject', '') or ''
                sender = safe_get(email.sender, 'email_address', '') if hasattr(email, 'sender') else ''
                if exclude_automated and is_automated_sender(sender, subject):
                    filtered_out += 1
                    continue
                text_body = safe_get(email, 'text_body', '') or ''
                text = f"{subject} {text_body[:500]}"
                documents.append({
                    "id": safe_get(email, 'id', ''),
                    "subject": subject,
                    "from": sender,
                    "datetime_received": safe_get(email, 'datetime_received'),
                    "text_body": text_body,
                    "text": text,
                })

            # Bug 4: per-call embed cap. Honour newest-first ordering.
            sampled_partial = False
            scanned_total = len(documents)
            if len(documents) > self._PER_CALL_EMBED_CAP:
                sampled_partial = True
                documents = documents[: self._PER_CALL_EMBED_CAP]
                self.logger.info(
                    "Partial embedding: embedding %d of %d items "
                    "(set a larger cap with future warmup work).",
                    self._PER_CALL_EMBED_CAP, scanned_total,
                )

            if not documents:
                # No mail to rank — don't embed anything. Be explicit so the
                # operator doesn't confuse this with an embedding failure.
                return format_success_response(
                    f"No messages found in folder {folder_name!r}",
                    query=query,
                    result_count=0,
                    results=[],
                    items=[],
                    filtered_out=filtered_out,
                    mailbox=mailbox,
                    folder=folder_name,
                )

            # Probe the embedding endpoint with the query FIRST. If the
            # provider is misconfigured (wrong model, wrong base_url,
            # missing model in Ollama, etc.) we want the error to surface
            # here rather than after scanning the inbox — and we want the
            # upstream error message to appear verbatim in the response.
            try:
                results = await embedding_service.search_similar(
                    query=query,
                    documents=documents,
                    text_key="text",
                    # Pull extra candidates so dedupe can still surface
                    # max_results unique hits after collapsing duplicates.
                    top_k=max_results * 3,
                    threshold=threshold,
                )
            except EmbeddingError as exc:
                raise ToolExecutionError(
                    f"Embedding provider error: {exc} | "
                    f"Hint: {_embedding_error_hint(str(exc))}"
                ) from exc

            # Bug 7: dedupe by message_id. Keep the highest-scoring hit
            # per id and track collapsed copies in duplicate_count.
            seen: Dict[str, Dict[str, Any]] = {}
            for doc, score in results:
                key = _id_from_doc(doc)
                if not key:
                    # No stable id — skip rather than duplicate arbitrarily.
                    continue
                score_rounded = round(score, 3)
                existing = seen.get(key)
                if existing is not None:
                    existing["duplicate_count"] = existing.get("duplicate_count", 0) + 1
                    # Only swap-in the new record if it scored higher;
                    # otherwise the better hit stays. Carry the
                    # duplicate_count across the swap.
                    if score_rounded > existing.get("similarity_score", 0):
                        dup_count = existing["duplicate_count"]
                        existing = None  # fall through to the builder below
                    else:
                        continue
                sender = doc.get("from") or ""
                subject = doc.get("subject") or ""
                from_is_automated = is_automated_sender(sender, subject)
                text_body = doc.get("text_body") or ""
                item = {
                    # Bug 5: canonical key + legacy alias (deprecation
                    # surfaced via meta in the response envelope).
                    "message_id": key,
                    "id": key,
                    "subject": subject,
                    "from": sender,
                    "received_time": str(doc.get("datetime_received") or ""),
                    "datetime_received": str(doc.get("datetime_received") or ""),
                    "similarity_score": score_rounded,
                    "snippet": (text_body or "")[:200],
                    "is_automated": from_is_automated,
                    "duplicate_count": dup_count if existing is None and key in seen else 0,
                }
                if "body" in fields:
                    item["body"] = text_body
                strip_body_by_default(item, keep_body="body" in fields)
                seen[key] = item

            formatted_results = list(seen.values())
            # Sort by score (desc) — dedupe scrambled order.
            formatted_results.sort(key=lambda i: i.get("similarity_score", 0), reverse=True)
            formatted_results = formatted_results[:max_results]

            # Apply projection last so per-field filtering happens once.
            projected = [project_fields(item, fields) for item in formatted_results]

            self.logger.info(
                "Semantic search: %d unique results (dedupe removed %d) "
                "for query %r",
                len(projected),
                sum(i.get("duplicate_count", 0) for i in formatted_results),
                query[:50],
            )

            response_meta = {
                "deprecations": [
                    "result.id is deprecated — use result.message_id instead. "
                    "Aliased for one release."
                ],
                "scanned": scanned_total,
                "filtered_automated": filtered_out if exclude_automated else 0,
                "sampled_partial": sampled_partial,
            }

            return format_success_response(
                f"Found {len(projected)} semantically similar emails",
                query=query,
                result_count=len(projected),
                results=projected,
                # Bug 5: canonical `items` + `count`, kept alongside `results`.
                items=projected,
                count=len(projected),
                total=scanned_total,
                meta=response_meta,
                mailbox=mailbox,
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to perform semantic search: {e}")
            raise ToolExecutionError(f"Failed to perform semantic search: {e}")


class ClassifyEmailTool(BaseTool):
    """Tool for classifying emails using AI."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "classify_email",
            "description": "Classify email priority, sentiment, and category using AI",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "include_spam_detection": {
                        "type": "boolean",
                        "description": "Include spam/phishing detection",
                        "default": False
                    },
                    **_TARGET_MAILBOX_SCHEMA
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute email classification."""
        message_id = kwargs.get("message_id")
        include_spam = kwargs.get("include_spam_detection", False)
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            # Get AI provider
            ai_provider = get_ai_provider(self.ews_client.config)
            if not ai_provider:
                raise ToolExecutionError("AI not enabled. Set enable_ai=true and enable_email_classification=true")

            classification_service = EmailClassificationService(ai_provider)

            # Resolve account (honours target_mailbox when impersonation is enabled).
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            message = find_message_for_account(account, message_id)

            # Extract email details
            subject = safe_get(message, 'subject', '')
            body = safe_get(message, 'text_body', '') or safe_get(message, 'body', '')[:2000]
            sender = safe_get(message.sender, 'email_address', '') if hasattr(message, 'sender') else 'unknown'

            # Classify
            classification = await classification_service.classify_full(
                subject=subject,
                body=body,
                sender=sender,
                include_spam=include_spam
            )

            self.logger.info(f"Classified email {message_id}: priority={classification['priority']['priority']}")

            return format_success_response(
                "Email classified successfully",
                message_id=message_id,
                subject=subject,
                classification=classification,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to classify email: {e}")
            raise ToolExecutionError(f"Failed to classify email: {e}")


class SummarizeEmailTool(BaseTool):
    """Tool for generating email summaries using AI."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "summarize_email",
            "description": "Generate a concise summary of an email using AI",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum summary length in characters",
                        "default": 200,
                        "minimum": 50,
                        "maximum": 500
                    },
                    **_TARGET_MAILBOX_SCHEMA
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute email summarization."""
        message_id = kwargs.get("message_id")
        max_length = kwargs.get("max_length", 200)
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            # Get AI provider
            ai_provider = get_ai_provider(self.ews_client.config)
            if not ai_provider:
                raise ToolExecutionError("AI not enabled. Set enable_ai=true and enable_email_summarization=true")

            classification_service = EmailClassificationService(ai_provider)

            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            message = find_message_for_account(account, message_id)

            # Extract email details
            subject = safe_get(message, 'subject', '')
            body = safe_get(message, 'text_body', '') or safe_get(message, 'body', '')

            # Generate summary
            summary = await classification_service.generate_summary(
                subject=subject,
                body=body,
                max_length=max_length
            )

            self.logger.info(f"Generated summary for email {message_id}")

            return format_success_response(
                "Email summarized successfully",
                message_id=message_id,
                subject=subject,
                summary=summary,
                summary_length=len(summary),
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to summarize email: {e}")
            raise ToolExecutionError(f"Failed to summarize email: {e}")


class SuggestRepliesTool(BaseTool):
    """Tool for generating smart reply suggestions using AI."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "suggest_replies",
            "description": "Generate smart reply suggestions for an email using AI",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "num_suggestions": {
                        "type": "integer",
                        "description": "Number of reply suggestions",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 5
                    },
                    **_TARGET_MAILBOX_SCHEMA
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute smart reply suggestion generation."""
        message_id = kwargs.get("message_id")
        num_suggestions = kwargs.get("num_suggestions", 3)
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            # Get AI provider
            ai_provider = get_ai_provider(self.ews_client.config)
            if not ai_provider:
                raise ToolExecutionError("AI not enabled. Set enable_ai=true and enable_smart_replies=true")

            classification_service = EmailClassificationService(ai_provider)

            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            message = find_message_for_account(account, message_id)

            # Extract email details
            subject = safe_get(message, 'subject', '')
            body = safe_get(message, 'text_body', '') or safe_get(message, 'body', '')
            sender = safe_get(message.sender, 'email_address', '') if hasattr(message, 'sender') else 'unknown'

            # Generate suggestions
            suggestions = await classification_service.suggest_replies(
                subject=subject,
                body=body,
                sender=sender,
                num_suggestions=num_suggestions
            )

            self.logger.info(f"Generated {len(suggestions)} reply suggestions for email {message_id}")

            return format_success_response(
                f"Generated {len(suggestions)} reply suggestions",
                message_id=message_id,
                subject=subject,
                suggestions=suggestions,
                suggestion_count=len(suggestions),
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to generate reply suggestions: {e}")
            raise ToolExecutionError(f"Failed to generate reply suggestions: {e}")
