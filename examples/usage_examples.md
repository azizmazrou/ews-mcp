# Usage Examples

Common use cases and examples for EWS MCP Server.

## Email Examples

### Send a Simple Email

```
AI Assistant: Send an email to john@example.com with subject "Meeting Tomorrow" and body "Let's meet at 2pm"
```

The server will use `send_email` tool:
```json
{
  "to": ["john@example.com"],
  "subject": "Meeting Tomorrow",
  "body": "Let's meet at 2pm"
}
```

### Send Email with CC and Attachments

```
AI Assistant: Send an email to the team at team@example.com, CC manager@example.com, with subject "Q4 Report" and attach the report.pdf file
```

### Read Unread Emails

```
AI Assistant: Show me my unread emails
```

Uses `read_emails` with `unread_only: true`

### Search for Emails

```
AI Assistant: Find all emails from boss@example.com received last week about the project
```

Uses `search_emails` with filters:
```json
{
  "from_address": "boss@example.com",
  "subject_contains": "project",
  "start_date": "2024-12-28T00:00:00",
  "end_date": "2025-01-04T23:59:59"
}
```

## Calendar Examples

### Schedule a Meeting

```
AI Assistant: Schedule a team meeting for tomorrow at 10am for 1 hour in Conference Room A, invite alice@example.com and bob@example.com
```

Uses `create_appointment`:
```json
{
  "subject": "Team Meeting",
  "start_time": "2025-01-05T10:00:00",
  "end_time": "2025-01-05T11:00:00",
  "location": "Conference Room A",
  "attendees": ["alice@example.com", "bob@example.com"]
}
```

### Check Calendar

```
AI Assistant: What meetings do I have this week?
```

Uses `get_calendar` with date range for current week

### Respond to Meeting

```
AI Assistant: Accept the meeting invitation from manager@example.com
```

Uses `respond_to_meeting` with `response: "Accept"`

### Cancel Meeting

```
AI Assistant: Cancel my 3pm meeting today and notify attendees
```

Uses `delete_appointment` with `send_cancellation: true`

## Contact Examples

### Add a Contact

```
AI Assistant: Add John Doe as a contact, email john.doe@example.com, phone 555-0100, works at Acme Corp as Software Engineer
```

Uses `create_contact`:
```json
{
  "given_name": "John",
  "surname": "Doe",
  "email_address": "john.doe@example.com",
  "phone_number": "555-0100",
  "company": "Acme Corp",
  "job_title": "Software Engineer"
}
```

### Find Contact

```
AI Assistant: Find contact information for John
```

Uses `search_contacts` with `query: "John"`

### Update Contact

```
AI Assistant: Update John Doe's phone number to 555-0200
```

Uses `update_contact`

## Task Examples

### Create Task

```
AI Assistant: Create a task to complete the Q4 report by January 31st, mark it as high priority
```

Uses `create_task`:
```json
{
  "subject": "Complete Q4 report",
  "due_date": "2025-01-31T17:00:00",
  "importance": "High"
}
```

### List Tasks

```
AI Assistant: Show me my pending tasks
```

Uses `get_tasks` with `include_completed: false`

### Complete Task

```
AI Assistant: Mark the Q4 report task as complete
```

Uses `complete_task`

## Complex Workflows

### Morning Briefing

```
AI Assistant: Give me my morning briefing
```

The AI would use multiple tools:
1. `read_emails` - Get unread emails
2. `get_calendar` - Get today's meetings
3. `get_tasks` - Get pending tasks

### Meeting Preparation

```
AI Assistant: I have a meeting with the client tomorrow. Find all emails from client@example.com this month, check my calendar for conflicts, and create a task to prepare presentation
```

Uses multiple tools:
1. `search_emails` - Find client emails
2. `get_calendar` - Check for conflicts
3. `create_task` - Create preparation task

### Weekly Cleanup

```
AI Assistant: Move all read emails from this week to a "Archive" folder
```

Uses:
1. `search_emails` - Find read emails from this week
2. `move_email` - Move each to archive folder (loop)

### Team Coordination

```
AI Assistant: Send a meeting invite to the engineering team for Friday at 2pm to discuss the new project, then create a task to prepare the agenda by Thursday
```

Uses:
1. `create_appointment` - Schedule meeting
2. `create_task` - Create agenda task

## Advanced Examples

### Email Summary

```
AI Assistant: Summarize my important emails from today
```

Uses:
1. `search_emails` with `importance: "High"` and today's date
2. AI summarizes the results

### Calendar Optimization

```
AI Assistant: Find gaps in my calendar this week where I could schedule a 1-hour meeting
```

Uses:
1. `get_calendar` for the week
2. AI analyzes gaps

### Contact Export

```
AI Assistant: List all contacts from Acme Corp
```

Uses:
1. `get_contacts`
2. Filter by company (AI or server-side)

### Task Dashboard

```
AI Assistant: Show me a dashboard of my tasks: how many are overdue, how many are due this week, and what's my completion rate
```

Uses:
1. `get_tasks` with `include_completed: true`
2. AI analyzes and formats results

## Error Handling Examples

### Invalid Email Address

```
AI Assistant: Send email to invalid-email
```

Response:
```json
{
  "success": false,
  "error": "Invalid input: Invalid email address",
  "error_type": "ValidationError"
}
```

### Authentication Failure

```
Response:
{
  "success": false,
  "error": "Authentication setup failed: Invalid client secret",
  "error_type": "AuthenticationError"
}
```

### Rate Limit Exceeded

```
Response:
{
  "success": false,
  "error": "Rate limit exceeded: maximum 25 requests per minute",
  "error_type": "RateLimitError",
  "is_retryable": true
}
```

## Tips for Best Results

1. **Be Specific**: Provide all required information (email addresses, dates, etc.)
2. **Use Natural Language**: The AI understands context
3. **Chain Operations**: Combine multiple tools in one request
4. **Handle Errors**: The AI will explain errors and suggest fixes
5. **Date Formats**: Use natural language (tomorrow, next week) or ISO 8601

## Common Patterns

### Daily Routine
- Check unread emails
- Review calendar for the day
- List pending tasks

### Email Management
- Search and archive old emails
- Move newsletters to specific folders
- Flag important emails

### Meeting Management
- Schedule recurring meetings
- Accept/decline invitations
- Update meeting details

### Task Tracking
- Create tasks from emails
- Set reminders
- Track progress
