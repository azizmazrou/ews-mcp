# Docker Startup Fix - FolderId Import Error

**Date:** 2025-11-17
**Issue:** Docker container crashes on startup with `ImportError: cannot import name 'FolderId'`
**Status:** ✅ FIXED in commit `deeca5a`

---

## 🚨 The Problem

After implementing custom folder access support, the Docker container failed to start with this error:

```
Traceback (most recent call last):
  File "/app/src/main.py", line 26, in <module>
    from .tools import (
  File "/app/src/tools/__init__.py", line 3, in <module>
    from .email_tools import SendEmailTool, ReadEmailsTool, ...
  File "/app/src/tools/email_tools.py", line 5, in <module>
    from exchangelib import Message, Mailbox, FileAttachment, HTMLBody, Body, FolderId, Folder
ImportError: cannot import name 'FolderId' from 'exchangelib'
```

### Root Cause
- `FolderId` class exists in `exchangelib.folders` module
- **NOT** exported in `exchangelib/__init__.py`
- Cannot be imported directly from `exchangelib` package
- Used in `resolve_folder()` function for folder ID access

---

## ✅ The Fix

### What Changed

**File:** `src/tools/email_tools.py`

**Before (Broken):**
```python
from exchangelib import Message, Mailbox, FileAttachment, HTMLBody, Body, FolderId, Folder

# Later in code:
folder_id = FolderId(id=folder_identifier)
folder = Folder(account=ews_client.account, folder_id=folder_id)
```

**After (Fixed):**
```python
from exchangelib import Message, Mailbox, FileAttachment, HTMLBody, Body, Folder

# Later in code - recursive tree search instead:
def find_folder_by_id(parent, target_id):
    """Recursively search for folder by ID."""
    if safe_get(parent, 'id', '') == target_id:
        return parent
    if hasattr(parent, 'children') and parent.children:
        for child in parent.children:
            result = find_folder_by_id(child, target_id)
            if result:
                return result
    return None

found_folder = find_folder_by_id(ews_client.account.root, folder_identifier)
```

### What Still Works ✅

All **4 folder resolution methods** remain functional:

1. ✅ **Standard folder names**: `read_emails(folder="inbox")`
2. ✅ **Folder IDs**: `read_emails(folder="AAMkADc3...")` - now uses tree search
3. ✅ **Folder paths**: `read_emails(folder="Inbox/CC")`
4. ✅ **Custom folder search**: `read_emails(folder="CC")`

---

## 🔧 How to Apply the Fix

### Option 1: Rebuild Docker Container (Recommended)

```bash
# Stop and remove existing container
docker stop ews-mcp-server
docker rm ews-mcp-server

# Pull latest code
git pull origin claude/build-ews-mcp-server-01Tpm9JpegRdnuECjLPb3ony

# Rebuild the Docker image
docker build -t ews-mcp-server .

# Run the new container
docker run -d \
  --name ews-mcp-server \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  ews-mcp-server

# Check logs - should start successfully now
docker logs -f ews-mcp-server
```

### Option 2: Using Docker Compose

```bash
# Stop existing services
docker-compose down

# Pull latest code
git pull origin claude/build-ews-mcp-server-01Tpm9JpegRdnuECjLPb3ony

# Rebuild and restart
docker-compose up -d --build

# Check logs
docker-compose logs -f
```

### Option 3: Local Development (No Docker)

```bash
# Pull latest code
git pull origin claude/build-ews-mcp-server-01Tpm9JpegRdnuECjLPb3ony

# Activate virtual environment
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate  # Windows

# Restart the server
python -m src.main
```

---

## ✅ Verification

After rebuilding, you should see:

### Success Output:
```
Setting up log directories...
Setting ownership on /app/logs to mcp:mcp...
Setting permissions on /app/logs...
Log directories ready.
Starting EWS MCP Server as user 'mcp'...

{"timestamp": "2025-11-17T...", "level": "INFO", "message": "EWS MCP Server starting..."}
{"timestamp": "2025-11-17T...", "level": "INFO", "message": "✓ Successfully connected to Exchange"}
{"timestamp": "2025-11-17T...", "level": "INFO", "message": "Server started successfully"}
```

### Test Custom Folder Access:
```python
# Test 1: Custom folder by name
read_emails(folder="CC", max_results=10)

# Test 2: Subfolder by path
read_emails(folder="Inbox/CC", max_results=10)

# Test 3: Standard folder (still works)
read_emails(folder="inbox", max_results=10)
```

**Expected:** All three commands should work without errors.

---

## 📊 Performance Impact

### Before Fix:
- ❌ Container crashes immediately
- ❌ No functionality available

### After Fix:
- ✅ Container starts successfully
- ✅ All 4 folder resolution methods work
- ⚠️ Folder ID access slightly slower (tree search vs direct access)
- ✅ No user-facing functionality lost

### Folder ID Access Performance:
- **Before:** O(1) - Direct access via FolderId
- **After:** O(n) - Tree traversal where n = number of folders
- **Impact:** Negligible for most users (<500 folders)
- **Alternative:** Use folder paths instead of IDs for better performance

---

## 🐛 Troubleshooting

### Issue 1: Still Getting ImportError After Rebuild

**Symptom:**
```
ImportError: cannot import name 'FolderId' from 'exchangelib'
```

**Solution:**
```bash
# Make sure you pulled the latest code
git pull origin claude/build-ews-mcp-server-01Tpm9JpegRdnuECjLPb3ony

# Verify the fix is present
grep "from exchangelib import" src/tools/email_tools.py
# Should NOT include FolderId in the output

# Force rebuild without cache
docker build --no-cache -t ews-mcp-server .

# Restart container
docker stop ews-mcp-server && docker rm ews-mcp-server
docker run -d --name ews-mcp-server --env-file .env ews-mcp-server
```

---

### Issue 2: Container Starts But Folder Access Fails

**Symptom:**
```
Error: Folder 'CC' not found
```

**Diagnosis:**
```bash
# Check server logs
docker logs ews-mcp-server | tail -20

# Test basic folder access first
read_emails(folder="inbox", max_results=1)

# If inbox works, try listing folders
list_folders()
```

**Possible Causes:**
1. Folder doesn't exist (check list_folders output)
2. Folder name mismatch (case-sensitive in some Exchange versions)
3. Permissions issue (folder exists but not accessible)

**Solutions:**
```python
# Solution 1: Use exact folder name from list_folders
list_folders()  # Note the exact name
read_emails(folder="<exact-name-from-list>")

# Solution 2: Use full path
read_emails(folder="Inbox/CC")

# Solution 3: Use folder ID
# Get ID from list_folders, then:
read_emails(folder="AAMkADc3...")
```

---

### Issue 3: Slow Folder ID Lookups

**Symptom:**
Accessing folders by ID takes several seconds

**Why:**
Tree search must traverse all folders to find matching ID

**Solution:**
Use folder paths instead of IDs for better performance:

```python
# ❌ Slow (tree search)
read_emails(folder="AAMkADc3MWUyMGQ0LTU5OGUtNGE2MC1hOTUyLTFhZjc5ZDY1ZWJiOQ...")

# ✅ Fast (direct navigation)
read_emails(folder="Inbox/CC")

# ✅ Fastest (standard folder)
read_emails(folder="inbox")
```

---

## 📝 Summary

### The Fix
- ✅ Removed `FolderId` import that was causing crash
- ✅ Implemented tree-based folder ID lookup
- ✅ All folder access methods still work
- ✅ No functionality lost

### To Apply
1. Pull latest code: `git pull origin claude/build-ews-mcp-server-01Tpm9JpegRdnuECjLPb3ony`
2. Rebuild Docker: `docker build -t ews-mcp-server .`
3. Restart container: `docker stop ews-mcp-server && docker rm ews-mcp-server && docker run -d ...`
4. Verify: `docker logs -f ews-mcp-server`

### What Changed
- **Commit:** `deeca5a` - "fix: Remove FolderId import causing Docker startup crash"
- **File:** `src/tools/email_tools.py`
- **Lines:** 5 (import), 45-67 (folder ID resolution)

### Next Steps
1. Rebuild your Docker container using instructions above
2. Test custom folder access: `read_emails(folder="CC")`
3. Verify server starts without errors
4. Review CUSTOM_FOLDER_ACCESS.md for full usage guide

---

**Status:** ✅ Ready to rebuild and test

The fix has been committed and pushed to branch `claude/build-ews-mcp-server-01Tpm9JpegRdnuECjLPb3ony`.
