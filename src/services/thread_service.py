"""ThreadService - Email thread preservation for EWS MCP v3.0."""

import logging
from typing import Optional, List
from datetime import datetime

from ..core.thread import ConversationThread
from ..core.email_message import EmailMessage, EmailRecipient
from ..utils import safe_get


class ThreadService:
    """
    Service for managing email conversation threads.

    Handles:
    - Thread retrieval and reconstruction
    - Reply formatting with thread preservation
    - HTML formatting with quoted text
    """

    def __init__(self, ews_client):
        """
        Initialize ThreadService.

        Args:
            ews_client: EWSClient instance
        """
        self.ews_client = ews_client
        self.logger = logging.getLogger(__name__)

    async def get_thread(
        self,
        message_id: str,
        max_messages: int = 50
    ) -> Optional[ConversationThread]:
        """
        Get complete conversation thread for a message.

        Args:
            message_id: Message ID to get thread for
            max_messages: Maximum messages to retrieve

        Returns:
            ConversationThread object or None
        """
        self.logger.info(f"Getting thread for message: {message_id}")

        try:
            # Get the original message
            # Search in multiple folders
            message = None
            for folder_name in ['inbox', 'sent', 'drafts']:
                folder = getattr(self.ews_client.account, folder_name)
                try:
                    message = folder.get(id=message_id)
                    if message:
                        break
                except:
                    continue

            if not message:
                self.logger.warning(f"Message not found: {message_id}")
                return None

            # Get conversation ID
            conversation_id = safe_get(message, 'conversation_id')
            if hasattr(conversation_id, 'id'):
                conversation_id = conversation_id.id

            if not conversation_id:
                # Single message thread
                email_message = EmailMessage.from_ews_message(message)
                thread = ConversationThread(
                    conversation_id=message_id,
                    subject=email_message.subject,
                    messages=[email_message],
                    message_count=1
                )
                return thread

            # Search for all messages in conversation
            thread_messages = []

            # Search inbox
            inbox_items = self.ews_client.account.inbox.filter(
                conversation_id=conversation_id
            )[:max_messages]

            for item in inbox_items:
                thread_messages.append(EmailMessage.from_ews_message(item))

            # Search sent
            sent_items = self.ews_client.account.sent.filter(
                conversation_id=conversation_id
            )[:max_messages]

            for item in sent_items:
                thread_messages.append(EmailMessage.from_ews_message(item))

            # Deduplicate by message_id
            seen_ids = set()
            unique_messages = []
            for msg in thread_messages:
                if msg.message_id not in seen_ids:
                    unique_messages.append(msg)
                    seen_ids.add(msg.message_id)

            # Create thread
            if unique_messages:
                thread = ConversationThread.from_messages(
                    conversation_id=str(conversation_id),
                    messages=unique_messages
                )
                return thread

            return None

        except Exception as e:
            self.logger.error(f"Failed to get thread: {e}")
            return None

    def format_reply_body_html(
        self,
        reply_body: str,
        original_message: EmailMessage,
        include_history: bool = True
    ) -> str:
        """
        Format reply body with HTML and quoted original message.

        Args:
            reply_body: New reply text
            original_message: Original message being replied to
            include_history: Include quoted original message

        Returns:
            Formatted HTML body
        """
        html = f"""<html>
<head>
<style>
body {{ font-family: Arial, sans-serif; font-size: 14px; }}
.reply {{ margin-bottom: 20px; }}
.quote {{ border-left: 3px solid #ccc; padding-left: 10px; color: #666; margin-top: 20px; }}
.quote-header {{ margin-bottom: 10px; font-size: 12px; }}
</style>
</head>
<body>
<div class="reply">
{reply_body}
</div>
"""

        if include_history:
            sender_name = original_message.sender.name or original_message.sender.email
            date_sent = original_message.datetime_sent.strftime('%B %d, %Y at %I:%M %p') if original_message.datetime_sent else 'Unknown date'

            html += f"""
<hr>
<div class="quote">
<div class="quote-header">
<strong>From:</strong> {sender_name}<br>
<strong>Sent:</strong> {date_sent}<br>
<strong>To:</strong> {', '.join([r.name or r.email for r in original_message.to_recipients])}<br>
<strong>Subject:</strong> {original_message.subject}
</div>
<div>
{original_message.body}
</div>
</div>
"""

        html += """
</body>
</html>
"""

        return html
