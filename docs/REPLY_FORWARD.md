# Reply & Forward Email Tools

This document describes the `reply_email` and `forward_email` tools for the EWS MCP Server.

## Overview

These tools enable AI assistants to reply to and forward emails while preserving:
- Full email body content (including conversation threads)
- Inline images (signatures with logos, embedded images)
- Proper header formatting (Outlook-style)
- Conversation threading metadata

---

## reply_email Tool

### Description

Reply to an existing email while preserving the conversation thread. Uses Exchange's built-in reply mechanism to maintain In-Reply-To headers, conversation ID, and thread relationship.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message_id` | string | Yes | The Exchange message ID of the email to reply to |
| `body` | string | Yes | The reply body (HTML supported) |
| `reply_all` | boolean | No | If true, reply to all recipients; if false, reply only to sender (default: false) |
| `attachments` | array | No | File paths to attach to the reply |
| `target_mailbox` | string | No | Email address to reply from (requires impersonation/delegate access) |

### Response

```json
{
  "status": "success",
  "message": "Reply sent successfully",
  "original_subject": "Original Email Subject",
  "reply_to": ["sender@example.com"],
  "reply_all": false,
  "attachments_count": 0,
  "inline_attachments_preserved": 2,
  "mailbox": "user@example.com"
}
```

### Example Usage

```python
# Simple reply
reply_email(
    message_id="AAMkADc3MWUy...",
    body="Thank you for your email. I'll review and get back to you."
)

# Reply all with HTML
reply_email(
    message_id="AAMkADc3MWUy...",
    body="<p>Sounds good! I'm <b>available</b> on Tuesday.</p>",
    reply_all=True
)

# Reply with attachment
reply_email(
    message_id="AAMkADc3MWUy...",
    body="Please find the updated document attached.",
    attachments=["/path/to/document.pdf"]
)

# Reply on behalf of another user (impersonation)
reply_email(
    message_id="AAMkADc3MWUy...",
    body="Thank you for contacting our team.",
    target_mailbox="shared@company.com"
)
```

---

## forward_email Tool

### Description

Forward an existing email to new recipients while preserving the original content, formatting, and attachments.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message_id` | string | Yes | The Exchange message ID of the email to forward |
| `to` | array | Yes | List of recipient email addresses |
| `body` | string | No | Optional message to add before the forwarded content |
| `cc` | array | No | CC recipients |
| `bcc` | array | No | BCC recipients |
| `attachments` | array | No | Additional file paths to attach |
| `target_mailbox` | string | No | Email address to forward from (requires impersonation/delegate access) |

### Response

```json
{
  "status": "success",
  "message": "Email forwarded successfully",
  "original_subject": "Original Email Subject",
  "forwarded_to": ["recipient@example.com"],
  "cc": null,
  "bcc": null,
  "attachments_included": 3,
  "inline_attachments_preserved": 2,
  "additional_attachments": 1,
  "mailbox": "user@example.com"
}
```

### Example Usage

```python
# Simple forward
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["colleague@company.com"]
)

# Forward with a message
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["manager@company.com"],
    body="FYI - Please see the email below regarding the project update."
)

# Forward to multiple recipients with CC
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["team-lead@company.com"],
    cc=["team@company.com"],
    body="<p>Please review the attached proposal.</p>"
)

# Forward with additional attachment
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["client@external.com"],
    body="Here's the information you requested, along with our latest brochure.",
    attachments=["/path/to/brochure.pdf"]
)

# Forward on behalf of another user (impersonation)
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["recipient@company.com"],
    target_mailbox="shared@company.com"
)
```

---

## Header Formatting

Both tools format headers in Outlook-style:

### HTML Format

```html
<hr style="border: none; border-top: 1px solid #ccc; margin: 20px 0;">
<div style="color: #1f497d; font-family: Calibri, Arial, sans-serif;">
<b>From:</b> John Doe <john.doe@company.com><br>
<b>Sent:</b> Sunday, December 07, 2025 10:30:45 AM<br>
<b>To:</b> Jane Smith <jane.smith@company.com>; Bob Wilson <bob@company.com><br>
<b>Cc:</b> Team Lead <lead@company.com><br>
<b>Subject:</b> Project Update
</div>
<br>
[Original email body here]
```

### Plain Text Format

```
────────────────────────────────────────
From: John Doe <john.doe@company.com>
Sent: Sunday, December 07, 2025 10:30:45 AM
To: Jane Smith <jane.smith@company.com>; Bob Wilson <bob@company.com>
Cc: Team Lead <lead@company.com>
Subject: Project Update

[Original email body here]
```

---

## Inline Image Preservation

Email signatures and embedded images are preserved correctly:

### How It Works

1. Original email contains inline images referenced by `cid:` (Content-ID):
   ```html
   <img src="cid:image001.png@01DC676B.768520B0">
   ```

2. When forwarding/replying, the tools copy attachments with:
   - `content_id` - The unique identifier for cid: references
   - `is_inline` - Flag marking the attachment as embedded

3. The recipient sees the images inline, not as broken icons

### Supported Elements

- Company logos in signatures
- Social media icons
- Certification badges
- Personal photos
- Any embedded image

---

## Conversation Threading

Both tools preserve conversation metadata:

| Metadata | Description |
|----------|-------------|
| `In-Reply-To` | References the original message ID |
| `References` | Chain of message IDs in the conversation |
| `Conversation-ID` | Exchange's conversation tracking ID |
| `Thread-Index` | Thread position indicator |

This ensures emails appear correctly grouped in:
- Outlook conversation view
- Gmail threading
- Apple Mail threads
- Most email clients

---

## RTL (Right-to-Left) Support

Full support for Arabic, Hebrew, and other RTL languages:

- Original RTL formatting is preserved
- Headers maintain proper direction
- Unicode characters display correctly

---

## Error Handling

### Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `message_id is required` | Missing message ID | Provide a valid message ID |
| `body is required` | Empty reply body | Provide reply content |
| `to recipients are required` | No forward recipients | Specify at least one recipient |
| `Message not found` | Invalid message ID | Verify the message exists |
| `Attachment file not found` | Invalid file path | Check the attachment path |
| `Permission denied` | File read error | Check file permissions |
| `Impersonation not enabled` | Missing config | Enable impersonation in .env |

---

## Best Practices

### 1. Use HTML for Rich Content
```python
reply_email(
    message_id="...",
    body="<p>I <b>agree</b> with the proposal.</p><ul><li>Point 1</li><li>Point 2</li></ul>"
)
```

### 2. Check Message ID Before Reply/Forward
```python
# First get the message details
details = get_email_details(message_id="...")

# Then reply
reply_email(message_id=details["message_id"], body="...")
```

### 3. Use Reply All Carefully
```python
# Only use reply_all when all original recipients need the response
reply_email(
    message_id="...",
    body="...",
    reply_all=True  # Everyone on the original email will receive this
)
```

### 4. Add Context When Forwarding
```python
forward_email(
    message_id="...",
    to=["manager@company.com"],
    body="For your review - this relates to the budget discussion from last week."
)
```

---

## Technical Details

### Body Extraction

The tools correctly extract HTML content from Exchange's `HTMLBody` object:

```python
# Exchange returns: message.body (HTMLBody object)
# Actual content is: message.body.body (string)

def extract_body_html(message):
    body = message.body
    if hasattr(body, 'body') and body.body:
        return body.body  # The actual HTML string
    return str(body) if body else ""
```

### Attachment Copying

Attachments are copied with all properties preserved:

```python
new_attachment = FileAttachment(
    name=original.name,
    content=original.content,
    content_type=original.content_type,
    content_id=original.content_id,    # For cid: references
    is_inline=original.is_inline       # Embedded vs attached
)
```

---

## See Also

- [Impersonation Guide](IMPERSONATION.md) - Access other mailboxes
- [Email Tools](API.md#email-tools) - All email operations
- [Troubleshooting](TROUBLESHOOTING.md) - Common issues
