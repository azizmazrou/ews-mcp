# EWS MCP Server - Logging System Documentation

## Overview

The EWS MCP Server implements a comprehensive, multi-layered logging system designed for:

- **Debugging and Troubleshooting**: Detailed activity logs with full context
- **Performance Monitoring**: Track response times and identify bottlenecks
- **Test Result Tracking**: Validate test executions and regressions
- **AI-Parseable Context**: Enable Claude and other AIs to understand system behavior
- **Compliance Auditing**: Maintain audit trails for security and compliance
- **User Pattern Learning**: Understand usage patterns for optimization

## Log Files

All logs are stored in `/app/logs` directory in JSON Lines format for easy parsing.

### 1. Activity Log (`ews_mcp_activity.log`)

**Purpose**: Records all activities and operations performed by the server.

**Format**: JSON Lines
```json
{
  "timestamp": "2025-11-06T14:30:45.123456",
  "level": "INFO",
  "module": "email_tools",
  "session_id": "sess_a1b2c3d4",
  "action": "SEND_EMAIL_SUCCESS",
  "data": {
    "to": ["user@example.com"],
    "subject": "Meeting Notes",
    "has_attachments": false
  },
  "result": {
    "status": "success",
    "duration_ms": 245,
    "message_id": "abc123"
  },
  "context": {
    "tool": "send_email"
  }
}
```

**Use Cases**:
- Track all operations chronologically
- Debug workflow issues
- Understand user behavior patterns
- Generate activity reports

### 2. Performance Log (`ews_mcp_performance.log`)

**Purpose**: Track performance metrics for optimization.

**Format**: JSON Lines
```json
{
  "timestamp": "2025-11-06T14:30:45.123456",
  "metric": "api_call",
  "session_id": "sess_a1b2c3d4",
  "tool": "send_email",
  "duration_ms": 245,
  "status": "success"
}
```

**Metrics Tracked**:
- `api_call`: Tool execution duration
- Response times by tool
- Success/failure rates
- Performance degradation detection

**Use Cases**:
- Identify slow operations (> 2000ms)
- Calculate P50, P95, P99 latencies
- Detect performance regressions
- Optimize frequently-used tools

### 3. Error Log (`ews_mcp_errors.log`)

**Purpose**: Dedicated error tracking for quick issue identification.

**Format**: JSON Lines (same as activity log, but only ERROR/CRITICAL levels)

**Use Cases**:
- Quick error identification
- Error pattern recognition
- Root cause analysis
- Alert generation

### 4. Audit Log (`ews_mcp_audit.log`)

**Purpose**: Compliance and security auditing.

**Format**: JSON Lines
```json
{
  "timestamp": "2025-11-06T14:30:45.123456",
  "session_id": "sess_a1b2c3d4",
  "user": "user@company.com",
  "action": "send_email",
  "resource": "send_email_operation",
  "result": "success",
  "details": {
    "duration_ms": 245
  },
  "ip_address": "internal",
  "user_agent": "MCP_Client"
}
```

**Use Cases**:
- Security compliance
- User action tracking
- Access control verification
- Forensic analysis

### 5. Test Results Log (`ews_mcp_test_results.log`)

**Purpose**: Track test executions and validate functionality.

**Format**: JSON Lines
```json
{
  "timestamp": "2025-11-06T14:30:45.123456",
  "test_suite": "email_operations",
  "test_case": "TC001_send_simple_email",
  "status": "PASSED",
  "duration_ms": 182,
  "assertions": {
    "message_id_exists": true,
    "status_success": true,
    "duration_acceptable": true
  }
}
```

**Use Cases**:
- Validate test coverage
- Track test success rates
- Detect regressions
- Performance testing

### 6. Conversation Context (`analysis/conversation_context.json`)

**Purpose**: AI-parseable context for understanding user interactions.

**Format**: JSON (continuously updated)
```json
{
  "session_id": "sess_a1b2c3d4",
  "started_at": "2025-11-06T14:00:00",
  "last_activity": "2025-11-06T14:30:45",
  "interactions": [
    {
      "timestamp": "2025-11-06T14:30:45",
      "user_input": "Send email to John about meeting",
      "agent_action": "send_email",
      "parameters": {
        "to": ["john@example.com"],
        "subject": "Meeting"
      },
      "result": {"success": true},
      "duration_ms": 245
    }
  ],
  "current_context": {},
  "people_mentioned": {},
  "user_patterns": {}
}
```

**Use Cases**:
- Enable Claude to understand conversation history
- Track user patterns and preferences
- Provide context for troubleshooting
- Support conversational AI features

## Log Analyzer

Use the `LogAnalyzer` class to programmatically analyze logs:

```python
from src.log_analyzer import LogAnalyzer

analyzer = LogAnalyzer()

# Get error summary
errors = analyzer.get_error_summary(hours=24)
print(f"Total errors: {errors['total_errors']}")

# Get performance metrics
perf = analyzer.get_performance_metrics(hours=24)
for tool, metrics in perf['tools'].items():
    print(f"{tool}: {metrics['avg_duration_ms']:.0f}ms avg")

# Generate human-readable report
report = analyzer.generate_summary_report()
print(report)

# Find slow operations
slow_ops = analyzer.find_slow_operations(threshold_ms=2000)
for op in slow_ops:
    print(f"Slow: {op['tool']} took {op['duration_ms']}ms")

# Find recurring errors
recurring = analyzer.find_recurring_errors(min_count=3)
for error_type, data in recurring['recurring_errors'].items():
    print(f"{error_type}: {data['count']} occurrences")
```

## Log Viewer CLI

Use the interactive log viewer for quick analysis:

```bash
# View recent activity logs
python scripts/view_logs.py view --file activity --lines 100

# View recent errors
python scripts/view_logs.py view --file errors --lines 50

# Search for keyword
python scripts/view_logs.py search --file activity --keyword "send_email"

# Show error summary
python scripts/view_logs.py errors --hours 24

# Show performance summary
python scripts/view_logs.py performance --hours 24
```

## Log Rotation

Logs are automatically rotated to prevent disk space issues:

```python
from src.log_rotation import rotate_logs, get_disk_usage

# Rotate logs (archives files older than today)
result = rotate_logs(keep_days=30)
print(f"Archived {result['archived']} files, deleted {result['deleted']} old archives")

# Check disk usage
usage = get_disk_usage()
print(f"Total log size: {usage['total_size_mb']}MB")
```

**Rotation Policy**:
- Logs older than 1 day are archived
- Archives are compressed with gzip
- Archives older than 30 days are deleted
- Archived logs: `/app/logs/daily/YYYY-MM-DD_filename.log.gz`

## Integration with Tools

All tools automatically log their operations through the `BaseTool` class:

```python
# Automatic logging happens in BaseTool.safe_execute()
class SendEmailTool(BaseTool):
    async def execute(self, **kwargs):
        # Your tool logic here
        return result

# Logs generated automatically:
# 1. Activity log: SEND_EMAIL_ATTEMPT
# 2. Activity log: SEND_EMAIL_SUCCESS (or ERROR)
# 3. Performance log: api_call metric
# 4. Audit log: user action
```

## Docker Configuration

Mount the logs directory as a volume to access logs from the host:

```yaml
# docker-compose.yml
services:
  ews-mcp-server:
    image: ghcr.io/azizmazrou/ews-mcp:latest
    volumes:
      - ./logs:/app/logs
```

Or with docker run:

```bash
docker run -v $(pwd)/logs:/app/logs ghcr.io/azizmazrou/ews-mcp:latest
```

## Querying Logs for Debugging

### Find all errors in the last hour

```bash
python scripts/view_logs.py errors --hours 1
```

### Find slow email operations

```python
from src.log_analyzer import LogAnalyzer

analyzer = LogAnalyzer()
slow_emails = [
    op for op in analyzer.find_slow_operations(threshold_ms=2000)
    if 'email' in op.get('tool', '')
]
```

### Track specific user's actions

```bash
python scripts/view_logs.py search --file audit --keyword "user@example.com"
```

### Generate daily report

```python
from src.log_analyzer import LogAnalyzer

analyzer = LogAnalyzer()
report = analyzer.generate_summary_report()

# Save to file
with open('/app/logs/daily_report.txt', 'w') as f:
    f.write(report)
```

## Log Levels

- **INFO**: Normal operations, successful executions
- **WARNING**: Potential issues, degraded performance
- **ERROR**: Operation failures, exceptions
- **CRITICAL**: System-level failures, security issues

## Privacy and Security

**Sensitive Data Handling**:
- Passwords, tokens, secrets are automatically redacted as `***REDACTED***`
- Email body content is truncated in logs
- Full sanitization in `LogManager._sanitize_data()`

**Audit Trail**:
- All user actions are logged
- Timestamps are in ISO 8601 format with timezone
- Session IDs track related operations
- IP addresses and user agents captured (where available)

## Best Practices

1. **Regular Monitoring**: Check error summary daily
2. **Performance Tracking**: Monitor P95 latencies weekly
3. **Disk Usage**: Run log rotation weekly or configure cron job
4. **Backup**: Archive important logs before rotation
5. **Analysis**: Use LogAnalyzer for trend detection
6. **Alerts**: Set up alerts for error spikes or performance degradation

## Troubleshooting

### Logs not being created

Check permissions on `/app/logs` directory:
```bash
ls -la /app/logs
# Should be writable by the app user
```

### Large log files

Run log rotation:
```bash
python -m src.log_rotation
```

### Missing log entries

Verify logging is initialized:
```python
from src.logging_system import get_logger
logger = get_logger()
# Should not raise errors
```

### Parse errors in log viewer

Some log entries may be malformed. The viewer skips invalid JSON automatically.

## Future Enhancements

- **Real-time log streaming**: WebSocket endpoint for live logs
- **Log aggregation**: Send logs to external systems (ELK, Splunk)
- **Alerting**: Automated alerts for error thresholds
- **Metrics dashboard**: Web UI for log visualization
- **Log retention policies**: Configurable retention per log type

## Examples

### Example 1: Find why emails are failing

```python
from src.log_analyzer import LogAnalyzer

analyzer = LogAnalyzer()

# Get error summary
errors = analyzer.get_error_summary(hours=24)

# Filter for email-related errors
email_errors = {
    error_type: data
    for error_type, data in errors['error_types'].items()
    if any('email' in ex.get('module', '').lower() for ex in data['examples'])
}

for error_type, data in email_errors.items():
    print(f"\n{error_type}: {data['count']} occurrences")
    for example in data['examples']:
        print(f"  - {example['result'].get('error', 'N/A')}")
```

### Example 2: Performance regression detection

```python
from src.log_analyzer import LogAnalyzer

analyzer = LogAnalyzer()

# Compare this week vs last week
this_week = analyzer.get_performance_metrics(hours=24*7)
# (Would need to implement historical comparison)

for tool, metrics in this_week['tools'].items():
    if metrics['avg_duration_ms'] > 1000:
        print(f"WARNING: {tool} averaging {metrics['avg_duration_ms']:.0f}ms")
```

### Example 3: User activity report

```bash
# Generate report for specific user
python scripts/view_logs.py search --file audit --keyword "john@example.com" > john_activity.log
```

---

**For more information, see**:
- `src/logging_system.py` - Core logging implementation
- `src/log_analyzer.py` - Log analysis tools
- `src/log_rotation.py` - Log rotation utilities
- `scripts/view_logs.py` - Interactive log viewer
