"""Email classification service using AI."""

import logging
from typing import Dict, Any, List, Optional
from .base import AIProvider, Message


class EmailClassificationService:
    """Service for classifying emails using AI."""

    def __init__(self, provider: AIProvider, temperature: float = 0.3):
        """Initialize classification service.

        Args:
            provider: AI provider to use
            temperature: Temperature for generation (lower = more deterministic)
        """
        self.provider = provider
        self.temperature = temperature
        self.logger = logging.getLogger(__name__)

    async def classify_priority(self, subject: str, body: str, sender: str) -> Dict[str, Any]:
        """Classify email priority.

        Args:
            subject: Email subject
            body: Email body
            sender: Sender email address

        Returns:
            Classification result with priority and confidence
        """
        system_prompt = """You are an email priority classifier. Analyze the email and classify its priority level.

Priority levels:
- urgent: Requires immediate action (deadlines, emergencies, time-sensitive)
- high: Important but not urgent (important projects, requests from management)
- normal: Standard emails (regular communication, updates)
- low: Non-essential (newsletters, FYI, informational)

Respond with JSON:
{
  "priority": "urgent|high|normal|low",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}"""

        user_prompt = f"""From: {sender}
Subject: {subject}

Body:
{body[:1000]}"""  # Limit body to 1000 chars

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt)
        ]

        result = await self.provider.complete_with_json(
            messages,
            temperature=self.temperature,
            max_tokens=200
        )

        return result

    async def classify_sentiment(self, subject: str, body: str) -> Dict[str, Any]:
        """Classify email sentiment.

        Args:
            subject: Email subject
            body: Email body

        Returns:
            Sentiment classification
        """
        system_prompt = """You are an email sentiment analyzer. Analyze the tone and sentiment of the email.

Sentiment levels:
- positive: Friendly, appreciative, enthusiastic
- neutral: Professional, informational, matter-of-fact
- negative: Frustrated, angry, disappointed, critical
- mixed: Contains both positive and negative elements

Respond with JSON:
{
  "sentiment": "positive|neutral|negative|mixed",
  "confidence": 0.0-1.0,
  "tone": "descriptive tone (e.g., professional, casual, urgent, friendly)",
  "emotions": ["list", "of", "detected", "emotions"]
}"""

        user_prompt = f"""Subject: {subject}

Body:
{body[:1500]}"""

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt)
        ]

        result = await self.provider.complete_with_json(
            messages,
            temperature=self.temperature,
            max_tokens=300
        )

        return result

    async def classify_category(self, subject: str, body: str, sender: str) -> Dict[str, Any]:
        """Classify email category.

        Args:
            subject: Email subject
            body: Email body
            sender: Sender email address

        Returns:
            Category classification
        """
        system_prompt = """You are an email categorizer. Classify the email into one or more categories.

Categories:
- meeting: Meeting invitations, scheduling
- invoice: Invoices, receipts, payments
- newsletter: Newsletters, marketing emails, announcements
- project: Project updates, deliverables, milestones
- support: Support requests, customer service
- personal: Personal communication
- notification: System notifications, alerts, confirmations
- action_required: Requires user action or response
- fyi: For information only, no action needed

Respond with JSON:
{
  "primary_category": "category name",
  "additional_categories": ["other", "relevant", "categories"],
  "confidence": 0.0-1.0,
  "action_required": true/false,
  "suggested_labels": ["label1", "label2"]
}"""

        user_prompt = f"""From: {sender}
Subject: {subject}

Body:
{body[:1500]}"""

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt)
        ]

        result = await self.provider.complete_with_json(
            messages,
            temperature=self.temperature,
            max_tokens=300
        )

        return result

    async def detect_spam(self, subject: str, body: str, sender: str) -> Dict[str, Any]:
        """Detect if email is spam or phishing.

        Args:
            subject: Email subject
            body: Email body
            sender: Sender email address

        Returns:
            Spam detection result
        """
        system_prompt = """You are an email spam and phishing detector. Analyze the email for signs of spam or phishing.

Look for:
- Suspicious sender addresses
- Urgent language demanding action
- Requests for personal/financial information
- Too-good-to-be-true offers
- Poor grammar and spelling
- Mismatched URLs
- Generic greetings

Respond with JSON:
{
  "is_spam": true/false,
  "is_phishing": true/false,
  "confidence": 0.0-1.0,
  "risk_level": "low|medium|high",
  "red_flags": ["list", "of", "suspicious", "elements"],
  "recommendation": "brief recommendation"
}"""

        user_prompt = f"""From: {sender}
Subject: {subject}

Body:
{body[:1500]}"""

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt)
        ]

        result = await self.provider.complete_with_json(
            messages,
            temperature=self.temperature,
            max_tokens=400
        )

        return result

    async def classify_full(
        self,
        subject: str,
        body: str,
        sender: str,
        include_spam: bool = False
    ) -> Dict[str, Any]:
        """Perform full email classification.

        Args:
            subject: Email subject
            body: Email body
            sender: Sender email address
            include_spam: Whether to include spam detection

        Returns:
            Complete classification result
        """
        # Run all classifications in parallel (if async runtime supports it)
        import asyncio

        tasks = [
            self.classify_priority(subject, body, sender),
            self.classify_sentiment(subject, body),
            self.classify_category(subject, body, sender)
        ]

        if include_spam:
            tasks.append(self.detect_spam(subject, body, sender))

        results = await asyncio.gather(*tasks)

        classification = {
            "priority": results[0],
            "sentiment": results[1],
            "category": results[2]
        }

        if include_spam:
            classification["spam"] = results[3]

        return classification

    async def generate_summary(self, subject: str, body: str, max_length: int = 200) -> str:
        """Generate a concise summary of the email.

        Args:
            subject: Email subject
            body: Email body
            max_length: Maximum summary length in characters

        Returns:
            Email summary
        """
        system_prompt = f"""You are an email summarizer. Create a concise summary of the email in {max_length} characters or less.

Focus on:
- Main topic or request
- Key action items
- Important dates or deadlines
- Critical information

Be clear, concise, and actionable."""

        user_prompt = f"""Subject: {subject}

Body:
{body[:2000]}"""

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt)
        ]

        result = await self.provider.complete(
            messages,
            temperature=self.temperature,
            max_tokens=100
        )

        return result.content.strip()

    async def suggest_replies(
        self,
        subject: str,
        body: str,
        sender: str,
        num_suggestions: int = 3
    ) -> List[Dict[str, str]]:
        """Generate smart reply suggestions.

        Args:
            subject: Email subject
            body: Email body
            sender: Sender email address
            num_suggestions: Number of reply suggestions

        Returns:
            List of reply suggestions
        """
        system_prompt = f"""You are an email reply assistant. Generate {num_suggestions} brief, professional reply suggestions.

Each reply should:
- Be appropriate for the email context
- Vary in tone (quick acknowledge, detailed response, polite decline, etc.)
- Be 1-3 sentences
- Be professional and clear

Respond with JSON:
{{
  "suggestions": [
    {{"tone": "tone description", "reply": "reply text"}},
    ...
  ]
}}"""

        user_prompt = f"""From: {sender}
Subject: {subject}

Body:
{body[:1500]}"""

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt)
        ]

        result = await self.provider.complete_with_json(
            messages,
            temperature=0.8,  # Higher temperature for variety
            max_tokens=500
        )

        return result.get("suggestions", [])
