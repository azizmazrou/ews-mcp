# Impersonation / Delegation Support

EWS MCP Server supports operating on behalf of other users through Exchange impersonation or delegation. This allows a service account to access multiple mailboxes without requiring separate credentials for each user.

## Overview

| Feature | Impersonation | Delegation |
|---------|---------------|------------|
| Use Case | Service accounts, automation | User-to-user delegation |
| Permission Source | Exchange Admin (PowerShell) | Individual user (Outlook) |
| Scope | Organization-wide (can be scoped) | Per-mailbox |
| `EWS_IMPERSONATION_TYPE` | `impersonation` | `delegate` |

## Requirements

### For Impersonation (Service Account)

The service account needs the `ApplicationImpersonation` management role in Exchange. This must be granted by an Exchange administrator.

**Exchange Online (Microsoft 365):**
```powershell
# Connect to Exchange Online
Connect-ExchangeOnline -UserPrincipalName admin@domain.com

# Grant impersonation rights to the service account
New-ManagementRoleAssignment -Name "EWS-Impersonation" `
    -Role "ApplicationImpersonation" `
    -User "service-account@domain.com"

# Optional: Scope to specific users only
New-ManagementScope -Name "EWS-Impersonation-Scope" `
    -RecipientRestrictionFilter "Department -eq 'Sales'"

New-ManagementRoleAssignment -Name "EWS-Impersonation-Scoped" `
    -Role "ApplicationImpersonation" `
    -User "service-account@domain.com" `
    -CustomRecipientWriteScope "EWS-Impersonation-Scope"
```

**Exchange On-Premises:**
```powershell
# Open Exchange Management Shell

# Grant impersonation rights to the service account
New-ManagementRoleAssignment -Name "EWS-Impersonation" `
    -Role "ApplicationImpersonation" `
    -User "DOMAIN\service-account"
```

### For Delegation

The target user must grant delegate access to the service account. This can be done via:
- Outlook: File > Account Settings > Delegate Access
- Exchange Admin Center: Recipients > Mailboxes > Mailbox delegation

## Configuration

Add the following to your `.env` file:

```env
# Enable impersonation support
EWS_IMPERSONATION_ENABLED=true

# Access type: 'impersonation' or 'delegate'
EWS_IMPERSONATION_TYPE=impersonation
```

## Usage

All tools accept an optional `target_mailbox` parameter to operate on behalf of another user:

### Email Operations

```python
# Read emails from another user's inbox
read_emails(
    folder="inbox",
    max_results=10,
    target_mailbox="user@domain.com"
)

# Send email on behalf of another user
send_email(
    to=["recipient@domain.com"],
    subject="Hello from shared mailbox",
    body="<p>This email is sent on behalf of the user.</p>",
    target_mailbox="shared-mailbox@domain.com"
)

# Reply to an email in another user's mailbox
reply_email(
    message_id="AAMkABC123...",
    body="<p>Reply on behalf of user.</p>",
    target_mailbox="user@domain.com"
)

# Forward email from another user's mailbox
forward_email(
    message_id="AAMkABC123...",
    to=["recipient@domain.com"],
    body="<p>Please review.</p>",
    target_mailbox="user@domain.com"
)
```

### Calendar Operations

```python
# Check another user's calendar
get_calendar(
    days_ahead=7,
    target_mailbox="user@domain.com"
)

# Create appointment in another user's calendar
create_appointment(
    subject="Meeting",
    start_time="2025-12-15T10:00:00",
    end_time="2025-12-15T11:00:00",
    target_mailbox="user@domain.com"
)

# Check availability for multiple users
check_availability(
    attendees=["user1@domain.com", "user2@domain.com"],
    start_time="2025-12-15T09:00:00",
    end_time="2025-12-15T17:00:00",
    target_mailbox="organizer@domain.com"
)
```

### Contact Operations

```python
# Search contacts in another user's mailbox
search_contacts(
    query="John",
    target_mailbox="user@domain.com"
)

# Create contact in another user's address book
create_contact(
    given_name="John",
    surname="Doe",
    email_address="john.doe@external.com",
    target_mailbox="user@domain.com"
)
```

### Task Operations

```python
# Get tasks from another user's task list
get_tasks(
    include_completed=False,
    target_mailbox="user@domain.com"
)

# Create task in another user's task list
create_task(
    subject="Complete report",
    due_date="2025-12-20",
    target_mailbox="user@domain.com"
)
```

### Folder Operations

```python
# List folders in another user's mailbox
list_folders(
    parent_folder="root",
    target_mailbox="user@domain.com"
)

# Create folder in another user's mailbox
create_folder(
    name="Project X",
    parent_folder="inbox",
    target_mailbox="user@domain.com"
)
```

### Out-of-Office Settings

```python
# Get OOF settings for another user
get_oof_settings(
    target_mailbox="user@domain.com"
)

# Set OOF for another user
set_oof_settings(
    state="Scheduled",
    external_reply="I am out of office.",
    internal_reply="I am out of office until Monday.",
    start_time="2025-12-20T00:00:00",
    end_time="2025-12-27T23:59:59",
    target_mailbox="user@domain.com"
)
```

## Response Format

When operating on another mailbox, responses include the `mailbox` field indicating which mailbox was accessed:

```json
{
  "success": true,
  "message": "Retrieved 10 emails",
  "mailbox": "user@domain.com",
  "emails": [...]
}
```

When no `target_mailbox` is specified (or it matches the primary account), the `mailbox` field shows the primary account email.

## Error Handling

### Common Errors

**Impersonation Not Enabled:**
```
ConnectionError: Impersonation not enabled. Set EWS_IMPERSONATION_ENABLED=true to access mailbox: user@domain.com
```
**Solution:** Enable impersonation in your `.env` file.

**Access Denied:**
```
ConnectionError: Failed to access mailbox user@domain.com: The account does not have permission to impersonate the requested user.
```
**Solution:** Ensure the service account has the `ApplicationImpersonation` role or delegate access.

**Mailbox Not Found:**
```
ConnectionError: Failed to access mailbox nonexistent@domain.com: The SMTP address has no mailbox associated with it.
```
**Solution:** Verify the email address is correct and the mailbox exists.

## Security Considerations

1. **Principle of Least Privilege:** Use management scopes to limit which mailboxes can be impersonated.

2. **Audit Logging:** All impersonation operations are logged with both the service account and target mailbox for compliance.

3. **Credential Security:** Store service account credentials securely using environment variables or a secrets manager.

4. **Monitor Usage:** Regularly review audit logs for unauthorized impersonation attempts.

## Performance Considerations

1. **Account Caching:** Impersonated accounts are cached to avoid repeated authentication overhead.

2. **Connection Pooling:** Impersonated accounts share the same connection pool configuration as the primary account.

3. **Cache Clearing:** The impersonation cache is automatically cleared when the server shuts down. For long-running servers, consider periodic cache clearing.

## Troubleshooting

### Verify Impersonation Rights

```powershell
# Exchange Online
Get-ManagementRoleAssignment -Role "ApplicationImpersonation" |
    Where-Object {$_.RoleAssignee -eq "service-account@domain.com"}

# Exchange On-Premises
Get-ManagementRoleAssignment -Role "ApplicationImpersonation" -GetEffectiveUsers |
    Where-Object {$_.EffectiveUserName -eq "DOMAIN\service-account"}
```

### Test Impersonation Manually

```python
from exchangelib import Account, Credentials, Configuration, IMPERSONATION

credentials = Credentials(username='service-account@domain.com', password='password')
config = Configuration(server='mail.domain.com', credentials=credentials)

# Test impersonation
account = Account(
    primary_smtp_address='target-user@domain.com',
    config=config,
    autodiscover=False,
    access_type=IMPERSONATION
)

# Try a simple operation
print(account.inbox.total_count)
```

### Enable Debug Logging

Set `LOG_LEVEL=DEBUG` in your `.env` file to see detailed impersonation logs:

```
INFO:EWSClient:Creating impersonated account for user@domain.com
INFO:EWSClient:Successfully connected to mailbox: user@domain.com
DEBUG:EWSClient:Using cached account for user@domain.com
```
