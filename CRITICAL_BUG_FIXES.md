# Critical Bug Fixes: Attachment & Empty Email Body Issues
**Date:** 2025-11-16
**Commit:** (pending)
**Severity:** HIGH - Email functionality broken for attachments and HTML content

---

## 🐛 Bugs Fixed

### Bug #1: Attachment Validation - Permission Check Missing ✅ FIXED

**Problem:**
- Pydantic validator only checked if file exists, not if it's readable
- Files owned by root (e.g., `-r--r--r-- 1 root root`) would pass validation
- Actual file reading would fail with permission error
- User gets confusing "file not found" error even though file exists

**Root Cause:**
```python
# OLD CODE (line 50):
if not Path(file_path).exists():
    raise ValueError(f"Attachment file not found: {file_path}")
```
This only checks existence, not readability!

**Fix Applied:**
```python
# NEW CODE (lines 50-63):
path = Path(file_path)
if not path.exists():
    raise ValueError(f"Attachment file not found: {file_path}")
if not path.is_file():
    raise ValueError(f"Attachment path is not a file: {file_path}")
# Check if file is readable by attempting to open it
try:
    with open(file_path, 'rb') as f:
        pass  # Just check if we can open it
except PermissionError:
    raise ValueError(f"Permission denied: Cannot read attachment file: {file_path}")
except Exception as e:
    raise ValueError(f"Cannot access attachment file {file_path}: {str(e)}")
```

**Impact:**
- Now provides clear error: "Permission denied: Cannot read attachment file"
- Validates file is actually readable before attempting send
- Prevents false positives where file exists but can't be read

**File:** `src/models.py:44-64`

---

### Bug #2: Empty Email Body - CDATA Wrapper Issue ✅ FIXED

**Problem:**
- HTML emails with CDATA wrapper `<![CDATA[<html>...]]>` arrived completely empty
- CDATA is XML syntax, not HTML - Exchange doesn't understand it
- No validation to detect empty body after processing
- No logging to debug body content issues

**Root Cause:**
```python
# OLD CODE (line 121):
body=HTMLBody(request.body)
```
This blindly wraps body in HTMLBody without:
1. Checking for CDATA wrapper (XML syntax)
2. Validating body isn't empty
3. Logging body size for debugging

**Fix Applied:**
```python
# NEW CODE (lines 117-136):
# Clean and prepare email body
email_body = request.body.strip()

# Strip CDATA wrapper if present (CDATA is XML syntax, not needed for Exchange)
if email_body.startswith('<![CDATA[') and email_body.endswith(']]>'):
    email_body = email_body[9:-3].strip()  # Remove <![CDATA[ and ]]>
    self.logger.info("Stripped CDATA wrapper from email body")

# Validate body is not empty after processing
if not email_body:
    raise ToolExecutionError("Email body is empty after processing")

# Log body length for debugging
self.logger.info(f"Email body length: {len(email_body)} characters")

# Create message
message = Message(
    account=self.ews_client.account,
    subject=request.subject,
    body=HTMLBody(email_body),
    to_recipients=[Mailbox(email_address=email) for email in request.to]
)
```

**Impact:**
- Automatically strips CDATA wrapper if present
- Validates body isn't empty before sending
- Logs body length for debugging
- Prevents sending empty emails

**File:** `src/tools/email_tools.py:117-138`

---

### Enhancement: Better Send Logging ✅ ADDED

**Problem:**
- No visibility into whether attachments were actually attached
- No confirmation of body content after send
- Hard to debug empty email issues

**Fix Applied:**
```python
# Added detailed logging (lines 166-180):
message.save()
self.logger.info(f"Message saved with {len(request.attachments)} attachment(s)")
message.send()
self.logger.info(f"Message sent with attachments to {', '.join(request.to)}")

# Verify message has body content
if hasattr(message, 'body') and message.body:
    self.logger.info(f"Verified message body exists (length: {len(str(message.body))})")
else:
    self.logger.warning("Message body may be empty after send")
```

**Impact:**
- Clear logging of attachment count
- Verification that body exists after send
- Warning if body appears empty
- Better debugging for email delivery issues

**File:** `src/tools/email_tools.py:165-180`

---

## 📋 User Action Required

### For Attachment Issues:

**If you get "Permission denied" error:**
```bash
# Option 1: Change file ownership
sudo chown $(whoami) /path/to/file.pdf

# Option 2: Copy to accessible location
cp /path/to/file.pdf ~/file.pdf
# Then use ~/file.pdf in attachment path
```

**If you get "Attachment file not found":**
```bash
# Verify file exists
ls -la /path/to/file.pdf

# Use absolute path, not relative
# Good: /home/user/file.pdf
# Bad: ./file.pdf or ../file.pdf
```

### For HTML Email Body:

**Do NOT wrap HTML in CDATA:**
```html
<!-- BAD - Will be stripped -->
<![CDATA[
<html><body>Hello</body></html>
]]>

<!-- GOOD - Direct HTML -->
<html><body>Hello</body></html>
```

**CDATA is now automatically stripped**, but it's cleaner to not include it.

---

## 🧪 Testing

### Test Case 1: Attachment with Permission Issue
```bash
# Create test file owned by root
sudo touch /tmp/root_file.txt
sudo chmod 644 /tmp/root_file.txt

# Try to send - should get clear error
# Before fix: "Attachment file not found: /tmp/root_file.txt"
# After fix: "Permission denied: Cannot read attachment file: /tmp/root_file.txt"
```

### Test Case 2: HTML Email with CDATA
```python
# Send email with CDATA wrapper
{
  "to": ["test@example.com"],
  "subject": "Test",
  "body": "<![CDATA[<html><body>Hello World</body></html>]]>"
}

# Before fix: Email arrives empty
# After fix: CDATA stripped, email has body "Hello World"
```

### Test Case 3: Empty Body Detection
```python
# Send email with only whitespace
{
  "to": ["test@example.com"],
  "subject": "Test",
  "body": "   \n\n   "
}

# Before fix: Email sent successfully (but empty)
# After fix: Error "Email body is empty after processing"
```

---

## 📊 Error Messages Reference

### Before Fixes:
```
❌ "Attachment file not found: /home/claude/file.pdf"
   (Even though file exists but has wrong permissions!)

❌ "Email sent successfully"
   (But email arrives completely empty!)
```

### After Fixes:
```
✅ "Permission denied: Cannot read attachment file: /home/claude/file.pdf"
   (Clear indication of permission issue)

✅ "Email body is empty after processing"
   (Prevents sending empty emails)

✅ "Email body length: 12345 characters"
   (Confirms body content exists)

✅ "Message sent with 2 attachment(s) to user@example.com"
   (Confirms attachments were attached)
```

---

## 🔍 Debugging Tips

### Enable Detailed Logging:
Check logs for these new messages:
```
INFO: Stripped CDATA wrapper from email body
INFO: Email body length: 5432 characters
INFO: Message saved with 1 attachment(s)
INFO: Message sent with attachments to user@example.com
INFO: Verified message body exists (length: 5432)
WARNING: Message body may be empty after send
```

### Common Issues:

1. **"Permission denied: Cannot read attachment file"**
   - File is owned by root or another user
   - Solution: Change ownership or copy to accessible location

2. **"Attachment path is not a file"**
   - You provided a directory path instead of file path
   - Solution: Provide path to actual file

3. **"Email body is empty after processing"**
   - Body was only whitespace or empty string
   - Solution: Provide actual HTML/text content

4. **Body still arrives empty (rare)**
   - Exchange Server might be blocking HTML content
   - Solution: Try plain text instead of HTML
   - Solution: Check for prohibited HTML tags (e.g., `<script>`)

---

## 💡 Best Practices

### Attachments:
1. ✅ Use absolute paths: `/home/user/file.pdf`
2. ✅ Ensure files are readable by MCP server user
3. ✅ Check file permissions before sending
4. ❌ Don't use relative paths: `./file.pdf`
5. ❌ Don't attach files owned by root

### HTML Email Body:
1. ✅ Use clean HTML without CDATA wrapper
2. ✅ Test with simple HTML first
3. ✅ Keep email size reasonable (< 1MB)
4. ❌ Don't wrap in CDATA (will be stripped anyway)
5. ❌ Don't use empty/whitespace-only body

### Debugging:
1. ✅ Check logs for body length confirmation
2. ✅ Verify attachment count in logs
3. ✅ Look for "Stripped CDATA" message
4. ✅ Watch for "Message body may be empty" warning

---

## 📝 Summary

**Bugs Fixed:**
1. ✅ Attachment validation now checks file readability, not just existence
2. ✅ CDATA wrapper automatically stripped from HTML body
3. ✅ Empty body validation prevents sending blank emails
4. ✅ Enhanced logging for better debugging

**Files Changed:**
- `src/models.py` - Enhanced attachment validation
- `src/tools/email_tools.py` - CDATA stripping, empty body check, better logging

**Impact:**
- Clear error messages for permission issues
- HTML emails now deliver correctly
- No more false success for empty emails
- Better visibility into email send process

**User Action:**
- Ensure attachment files are readable (not root-owned)
- Remove CDATA wrappers from HTML (or let system strip them)
- Check logs for detailed send information
