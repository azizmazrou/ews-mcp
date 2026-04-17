"""AI-powered tools for EWS MCP Server."""

from typing import Any, Dict
from .base import BaseTool
from ..exceptions import EmbeddingError, ToolExecutionError
from ..utils import format_success_response, safe_get, find_message_for_account
from ..ai import get_ai_provider, get_embedding_provider, EmailClassificationService, EmbeddingService


_TARGET_MAILBOX_SCHEMA = {
    "target_mailbox": {
        "type": "string",
        "description": "Email address to operate on (requires EWS_IMPERSONATION_ENABLED=true)"
    }
}


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
                    **_TARGET_MAILBOX_SCHEMA
                },
                "required": ["query"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute semantic search."""
        query = kwargs.get("query")
        folder_name = kwargs.get("folder", "inbox").lower()
        max_results = kwargs.get("max_results", 10)
        threshold = kwargs.get("threshold", 0.7)
        target_mailbox = kwargs.get("target_mailbox")

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

            # Fetch recent emails
            emails = list(folder.all().order_by('-datetime_received')[:100])

            # Prepare documents for search
            documents = []
            for email in emails:
                text = f"{safe_get(email, 'subject', '')} {safe_get(email, 'text_body', '')[:500]}"
                documents.append({
                    "id": safe_get(email, 'id', ''),
                    "subject": safe_get(email, 'subject', ''),
                    "from": safe_get(email.sender, 'email_address', '') if hasattr(email, 'sender') else '',
                    "datetime_received": safe_get(email, 'datetime_received'),
                    "text": text
                })

            if not documents:
                # No mail to rank — don't embed anything. Be explicit so the
                # operator doesn't confuse this with an embedding failure.
                return format_success_response(
                    f"No messages found in folder {folder_name!r}",
                    query=query,
                    result_count=0,
                    results=[],
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
                    top_k=max_results,
                    threshold=threshold,
                )
            except EmbeddingError as exc:
                hint = (
                    "Verify AI_EMBEDDING_MODEL matches an installed model at "
                    "AI_BASE_URL (e.g. 'text-embedding-3-small' for OpenAI, "
                    "'nomic-embed-text' for Ollama)."
                )
                raise ToolExecutionError(
                    f"Embedding provider error: {exc} | Hint: {hint}"
                ) from exc

            # Format results
            formatted_results = []
            for doc, score in results:
                formatted_results.append({
                    "id": doc["id"],
                    "subject": doc["subject"],
                    "from": doc["from"],
                    "datetime_received": str(doc["datetime_received"]),
                    "similarity_score": round(score, 3)
                })

            self.logger.info(f"Semantic search found {len(formatted_results)} results for query: {query[:50]}")

            return format_success_response(
                f"Found {len(formatted_results)} semantically similar emails",
                query=query,
                result_count=len(formatted_results),
                results=formatted_results,
                mailbox=mailbox
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
