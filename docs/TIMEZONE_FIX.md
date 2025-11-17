# Timezone Fix - Complete Documentation

This document explains the comprehensive timezone fix applied to resolve the persistent `"No time zone found with key UTC+03:00"` error.

## The Problem

### Error Message
```
Failed to get calendar: No time zone found with key UTC+03:00
Failed to search emails: No time zone found with key UTC+03:00
```

### Root Cause

The error occurred because **exchangelib requires specific datetime formats**:

1. **EWSDateTime objects** (not regular Python datetime)
2. **EWSTimeZone as tzinfo** (not pytz, not timezone offset)
3. **IANA timezone names** (e.g., `Asia/Riyadh`, not `UTC+03:00`)

When regular Python `datetime` objects were passed to exchangelib methods like `calendar.view()` or query filters, exchangelib attempted to convert them internally. During this conversion, the timezone was transformed into UTC offset format (`UTC+03:00`) instead of preserving the IANA timezone name (`Asia/Riyadh`), causing the error.

## The Solution

### Overview

The fix involves three key changes:

1. **Use EWSDateTime instead of datetime** for all exchangelib operations
2. **Use EWSTimeZone** (IANA timezone name) as tzinfo
3. **Properly convert timezone-aware datetimes** before creating EWSDateTime

### Implementation

#### 1. Configuration (src/config.py)

Added timezone configuration:
```python
class Settings(BaseSettings):
    timezone: str = "UTC"  # Default timezone
```

Set via environment variable:
```bash
TIMEZONE=Asia/Riyadh  # IANA timezone name
```

#### 2. EWS Client (src/ews_client.py)

Set default timezone when creating Account:
```python
from exchangelib import EWSTimeZone

tz = EWSTimeZone(self.config.timezone)  # e.g., EWSTimeZone('Asia/Riyadh')

account = Account(
    primary_smtp_address=self.config.ews_email,
    credentials=credentials,
    autodiscover=True,
    access_type=DELEGATE,
    default_timezone=tz  # Use EWSTimeZone
)
```

#### 3. Utility Functions (src/utils.py)

**get_timezone()** - Returns EWSTimeZone:
```python
def get_timezone():
    """Get configured timezone as EWSTimeZone."""
    tz_name = os.environ.get('TIMEZONE', 'UTC')
    return EWSTimeZone(tz_name)  # Returns EWSTimeZone('Asia/Riyadh')
```

**make_tz_aware()** - Creates EWSDateTime with EWSTimeZone:
```python
def make_tz_aware(dt: datetime) -> EWSDateTime:
    """Convert Python datetime to EWSDateTime with EWSTimeZone."""
    tz = get_timezone()  # EWSTimeZone('Asia/Riyadh')

    if dt.tzinfo is not None:
        # Already has timezone - convert to target timezone first
        tz_name = os.environ.get('TIMEZONE', 'UTC')
        target_tz = pytz.timezone(tz_name)
        dt_converted = dt.astimezone(target_tz)

        # Create EWSDateTime from converted values
        return EWSDateTime(
            dt_converted.year, dt_converted.month, dt_converted.day,
            dt_converted.hour, dt_converted.minute, dt_converted.second,
            dt_converted.microsecond,
            tzinfo=tz  # EWSTimeZone, not pytz!
        )

    # Naive datetime - create EWSDateTime directly
    return EWSDateTime(
        dt.year, dt.month, dt.day,
        dt.hour, dt.minute, dt.second,
        dt.microsecond,
        tzinfo=tz
    )
```

**parse_datetime_tz_aware()** - Parse ISO strings to EWSDateTime:
```python
def parse_datetime_tz_aware(dt_str: str) -> EWSDateTime:
    """Parse ISO 8601 string and return EWSDateTime."""
    dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    return make_tz_aware(dt)  # Returns EWSDateTime!
```

#### 4. Tool Updates

**All tools updated to use timezone-aware functions:**

```python
# calendar_tools.py
start_time = parse_datetime_tz_aware(kwargs["start_time"])  # EWSDateTime
end_time = parse_datetime_tz_aware(kwargs["end_time"])      # EWSDateTime
item = CalendarItem(..., start=start_time, end=end_time)

# email_tools.py
start = parse_datetime_tz_aware(kwargs["start_date"])  # EWSDateTime
query = query.filter(datetime_received__gte=start)

# task_tools.py
due_date = parse_datetime_tz_aware(kwargs["due_date"])  # EWSDateTime
task = Task(..., due_date=due_date)
```

### Why This Works

#### The Conversion Flow

**Before (BROKEN):**
```
User Input: "2025-11-05T00:00:00+03:00"
    ↓
Python datetime with timezone offset (+03:00)
    ↓
Pass to exchangelib
    ↓
exchangelib tries to convert
    ↓
Converts to "UTC+03:00" (offset format)
    ↓
❌ ERROR: "No time zone found with key UTC+03:00"
```

**After (WORKING):**
```
User Input: "2025-11-05T00:00:00+03:00"
    ↓
Python datetime with timezone offset (+03:00)
    ↓
parse_datetime_tz_aware()
    ↓
make_tz_aware() converts to target timezone
    ↓
Creates EWSDateTime with EWSTimeZone('Asia/Riyadh')
    ↓
Pass to exchangelib
    ↓
✅ exchangelib sees EWSDateTime with proper EWSTimeZone
    ↓
✅ No conversion needed - uses "Asia/Riyadh" directly
```

#### Key Insight

**exchangelib checks the type of datetime objects:**
- If it's `EWSDateTime` with `EWSTimeZone` → uses it directly
- If it's regular `datetime` → converts it (causes the error)

By always using `EWSDateTime` with `EWSTimeZone`, we bypass the problematic conversion.

## Supported Timezones

All IANA standard timezone names are supported:

### Middle East
- `Asia/Riyadh` - Saudi Arabia (UTC+03:00)
- `Asia/Dubai` - UAE (UTC+04:00)
- `Asia/Kuwait` - Kuwait (UTC+03:00)
- `Asia/Bahrain` - Bahrain (UTC+03:00)
- `Asia/Qatar` - Qatar (UTC+03:00)

### United States
- `America/New_York` - Eastern Time
- `America/Chicago` - Central Time
- `America/Denver` - Mountain Time
- `America/Los_Angeles` - Pacific Time

### Europe
- `Europe/London` - UK (GMT/BST)
- `Europe/Paris` - France (CET/CEST)
- `Europe/Berlin` - Germany (CET/CEST)
- `Europe/Moscow` - Russia (MSK)

### Asia
- `Asia/Tokyo` - Japan (JST)
- `Asia/Shanghai` - China (CST)
- `Asia/Kolkata` - India (IST)
- `Asia/Singapore` - Singapore (SGT)

### Other
- `Australia/Sydney` - Australia (AEDT/AEST)
- `Pacific/Auckland` - New Zealand (NZDT/NZST)

## Usage

### Environment Variable

Set the timezone using the `TIMEZONE` environment variable:

```bash
docker run -d \
  -e TIMEZONE=Asia/Riyadh \
  -e EWS_EMAIL=user@company.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=user@company.com \
  -e EWS_PASSWORD=password \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

### Environment File

```bash
# ews-mcp.env
TIMEZONE=Asia/Riyadh
EWS_EMAIL=user@company.com
EWS_AUTH_TYPE=basic
EWS_USERNAME=user@company.com
EWS_PASSWORD=password
```

```bash
docker run -d --env-file ews-mcp.env ghcr.io/azizmazrou/ews-mcp-server:latest
```

### Verification

Check logs to confirm timezone is loaded:

```bash
docker logs <container-id>
```

Expected output:
```
INFO - Using timezone: Asia/Riyadh
INFO - Successfully loaded timezone: Asia/Riyadh
INFO - Successfully connected to Exchange
```

## Testing

### Test Calendar Operations

```bash
curl -X POST http://localhost:8000/messages \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "get_calendar",
      "arguments": {
        "start_date": "2025-11-05T00:00:00+03:00",
        "end_date": "2025-11-05T23:59:59+03:00"
      }
    },
    "id": 1
  }'
```

### Test Email Search with Dates

```bash
curl -X POST http://localhost:8000/messages \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "search_emails",
      "arguments": {
        "folder": "inbox",
        "start_date": "2025-11-01T00:00:00+03:00",
        "end_date": "2025-11-05T23:59:59+03:00"
      }
    },
    "id": 2
  }'
```

### Test Task Creation

```bash
curl -X POST http://localhost:8000/messages \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "create_task",
      "arguments": {
        "subject": "Test Task",
        "body": "Test body",
        "due_date": "2025-11-10T17:00:00+03:00"
      }
    },
    "id": 3
  }'
```

## Troubleshooting

### Error: "No time zone found with key UTC+XX:XX"

**Solution:** Update to latest image with timezone fixes:
```bash
docker pull ghcr.io/azizmazrou/ews-mcp-server:latest
```

### Error: "Failed to load timezone Asia/Riyadh"

**Solution:** Verify timezone name is correct. List available timezones:
```bash
docker run --rm ghcr.io/azizmazrou/ews-mcp-server:latest \
  ls /usr/share/zoneinfo/Asia/
```

### Wrong Timestamp Display

**Solution:** Verify TIMEZONE environment variable is set:
```bash
docker exec <container-id> env | grep TIMEZONE
```

## Commit History

Timeline of timezone fixes:

1. **1962da5** - Added timezone configuration (TIMEZONE env var)
2. **2d6c2e7** - Implemented timezone-aware datetime handling
3. **1d76e79** - Fixed EWSTimeZone constructor API
4. **2a6d598** - Changed to use EWSDateTime instead of datetime
5. **aea8814** - Fixed timezone-aware datetime conversion (final fix)

## Related Files

- `src/config.py` - Timezone configuration
- `src/ews_client.py` - Account timezone setup
- `src/utils.py` - Timezone utility functions
- `src/tools/calendar_tools.py` - Calendar operations
- `src/tools/email_tools.py` - Email operations
- `src/tools/task_tools.py` - Task operations

## References

- [exchangelib EWSDateTime Documentation](https://ecederstrand.github.io/exchangelib/exchangelib/ewsdatetime.html)
- [IANA Time Zone Database](https://www.iana.org/time-zones)
- [Microsoft Exchange Timezone Information](https://learn.microsoft.com/en-us/exchange/client-developer/exchange-web-services/time-zones-and-ews-in-exchange)
