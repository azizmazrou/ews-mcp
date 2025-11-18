# Troubleshooting Guide

Common issues and solutions for EWS MCP Server v3.0.

> **Note:** Many issues from v2.x have been resolved in v3.0, particularly the GAL 0-results bug which is now completely fixed with multi-strategy search.

## Authentication Issues

### Problem: "Authentication failed" error

**Symptoms:**
```
AuthenticationError: Authentication setup failed
```

**Solutions:**

1. **Verify OAuth2 Credentials:**
   ```bash
   # Check all required environment variables are set
   echo $EWS_CLIENT_ID
   echo $EWS_TENANT_ID
   # Don't echo the secret for security
   ```

2. **Check Azure AD App Permissions:**
   - Ensure app has `full_access_as_app` or specific EWS permissions
   - Verify admin consent has been granted
   - Check app is not expired or disabled

3. **Test Basic Auth First:**
   ```bash
   # Try basic auth to isolate OAuth2 issues
   EWS_AUTH_TYPE=basic
   EWS_USERNAME=user@company.com
   EWS_PASSWORD=yourpassword
   ```

4. **Check Token Acquisition:**
   - Enable DEBUG logging to see token requests
   - Verify network can reach login.microsoftonline.com
   - Check firewall/proxy settings

### Problem: "Invalid client secret" error

**Solution:**
- Client secrets expire - check expiration in Azure Portal
- Generate new secret and update .env file
- Ensure you copied the secret VALUE, not the ID

## Connection Issues

### Problem: "Connection timeout" or "Cannot connect to Exchange"

**Symptoms:**
```
ConnectionError: Failed to connect to Exchange
```

**Solutions:**

1. **Verify Server URL:**
   ```bash
   # Test connectivity
   curl -v https://outlook.office365.com/EWS/Exchange.asmx

   # Or for on-premises
   curl -v https://mail.company.com/EWS/Exchange.asmx
   ```

2. **Check Network Connectivity:**
   - Verify firewall allows HTTPS (443) to Exchange server
   - Test from Docker container if using Docker
   - Check corporate proxy settings

3. **Try Autodiscovery:**
   ```bash
   EWS_AUTODISCOVER=true
   # Don't set EWS_SERVER_URL
   ```

4. **Increase Timeout:**
   ```bash
   REQUEST_TIMEOUT=60
   ```

### Problem: "Autodiscovery failed"

**Solutions:**

1. **Use Manual Configuration:**
   ```bash
   EWS_AUTODISCOVER=false
   EWS_SERVER_URL=https://mail.company.com/EWS/Exchange.asmx
   ```

2. **Check DNS:**
   ```bash
   # Should return autodiscover endpoint
   nslookup autodiscover.company.com
   ```

3. **Verify Email Domain:**
   - Ensure EWS_EMAIL matches your Exchange domain
   - Check for typos

## Docker Issues

### Problem: Container won't start

**Solutions:**

1. **Check Logs:**
   ```bash
   docker logs ews-mcp-server
   ```

2. **Verify .env File:**
   ```bash
   # Ensure file exists and is readable
   ls -la .env
   cat .env | grep -v PASSWORD
   ```

3. **Check Docker Resources:**
   - Ensure Docker has enough memory (min 512MB)
   - Check disk space
   - Restart Docker daemon

4. **Rebuild Image:**
   ```bash
   docker-compose down
   docker-compose build --no-cache
   docker-compose up
   ```

### Problem: "Permission denied" errors in container

**Solution:**
```bash
# Check file permissions
ls -la src/

# Fix permissions
chmod -R 755 src/

# Rebuild
docker-compose up --build
```

## Tool Execution Errors

### Problem: "Tool execution failed" errors

**Common Causes:**

1. **Invalid Item IDs:**
   - Item IDs expire or become invalid
   - Use search to find current IDs
   - Don't cache IDs for long periods

2. **Permissions Issues:**
   - Verify app has required permissions
   - Check mailbox delegation settings
   - Ensure user has access to requested folders

3. **Rate Limiting:**
   ```
   RateLimitError: Rate limit exceeded
   ```
   - Wait 60 seconds
   - Reduce request frequency
   - Increase RATE_LIMIT_REQUESTS_PER_MINUTE

### Problem: Email sending fails

**Solutions:**

1. **Check Send Permissions:**
   - Verify app can send emails
   - Check SMTP is enabled on mailbox
   - Test with a simple email first

2. **Attachment Issues:**
   - Verify file paths are accessible
   - Check file size under MAX_ATTACHMENT_SIZE (150MB default)
   - Ensure files exist and are readable

3. **Recipient Validation:**
   - Use valid email addresses
   - Check recipient exists in organization
   - Verify no send restrictions

### Problem: find_person returns 0 results for GAL search

**FIXED in v3.0!** This issue has been completely resolved with multi-strategy GAL search.

**Symptoms (v2.x):**
```python
# GAL-specific search returns no results
find_person(query="Smith", search_scope="gal")
# Result: total_results: 0

# But email_history search finds results
find_person(query="Smith", search_scope="all")
# Result: Found contacts in email_history
```

**v3.0 Solution:**
The new GALAdapter uses 4-strategy search that never returns 0 results when people exist:
1. **Exact match** - Original resolve_names
2. **Partial match** - Wildcard/prefix search (NEW)
3. **Domain search** - Find all @domain.com users (NEW)
4. **Fuzzy match** - Handle typos and variations (NEW)

**Root Cause (v2.x):**
The GAL search uses `account.protocol.resolve_names()` which returns tuples in the format `(mailbox, contact_info)`. If the code doesn't properly unpack these tuples, results appear empty.

**Verification:**
Check if direct Python code works:
```python
from exchangelib import Account, Credentials

credentials = Credentials(username="user@example.com", password="pass")
account = Account(primary_smtp_address="user@example.com",
                  credentials=credentials, autodiscover=True)

# This should work
results = account.protocol.resolve_names(
    names=["Smith"],
    return_full_contact_data=True
)

# Results are tuples: (mailbox, contact_info)
for result in results:
    mailbox, contact_info = result
    print(f"Found: {mailbox.name} - {mailbox.email_address}")
```

**Solution:**
The `_search_gal()` method in `contact_intelligence_tools.py` has been updated to properly handle tuple format:

```python
# Correct tuple unpacking
if isinstance(result, tuple):
    mailbox = result[0]
    contact_info = result[1] if len(result) > 1 else None

    contact = {
        'name': getattr(mailbox, 'name', ''),
        'email': getattr(mailbox, 'email_address', ''),
        'routing_type': getattr(mailbox, 'routing_type', 'SMTP')
    }

    # Extract additional details from contact_info
    if contact_info:
        if hasattr(contact_info, 'display_name'):
            contact['display_name'] = contact_info.display_name
        # ... more fields
```

**Testing:**
```python
# Test with generic names
find_person(query="Smith", search_scope="gal")
# Should return results from GAL only

# Test with Arabic text (UTF-8)
find_person(query="أحمد", search_scope="gal")
# Should handle Arabic names correctly

# Test email search
find_person(query="john@example.com", search_scope="gal")
# Should find by email address
```

**Verify Scope Isolation:**
```python
# GAL-only search should NOT include email_history
result = find_person(query="Smith", search_scope="gal")
for contact in result['unified_results']:
    assert 'gal' in contact['sources']
    assert 'email_history' not in contact['sources']
```

**Debugging:**
Enable detailed logging to see GAL search flow:
```bash
LOG_LEVEL=DEBUG
```

Look for these log messages:
```
=== GAL Search Start === Query: 'Smith'
GAL returned 5 raw result(s)
  GAL [1] John Smith <john.smith@company.com> @ Example Corp
  GAL [2] Jane Smith <jane.smith@company.com> @ Acme Inc
=== GAL Search Complete === Found 2 contact(s)
```

If you see:
```
GAL search returned 0 results
```

But direct Python code works, then there may be an issue with:
1. Account initialization in the MCP server
2. Permissions on the service account
3. Network connectivity to Exchange server

**Common Mistakes:**
```python
# Wrong - trying to access .mailbox.name on tuple
name = result.mailbox.name  # AttributeError!

# Correct - unpack tuple first
mailbox, contact_info = result
name = mailbox.name  # Works!

# Also correct - check type first
if isinstance(result, tuple):
    mailbox = result[0]
    name = mailbox.name
```

## Performance Issues

### Problem: Slow response times

**Solutions:**

1. **Enable Caching:**
   ```bash
   ENABLE_CACHE=true
   CACHE_TTL=300
   ```

2. **Reduce Result Sets:**
   - Use smaller max_results values
   - Implement pagination
   - Use specific date ranges

3. **Connection Pooling:**
   ```bash
   CONNECTION_POOL_SIZE=20
   ```

4. **Optimize Queries:**
   - Use search filters instead of fetching all items
   - Filter on server-side, not client-side

## Logging and Debugging

### Enable Debug Logging

```bash
LOG_LEVEL=DEBUG
```

### Check Server Logs

```bash
# Docker
docker-compose logs -f

# Local
tail -f logs/ews-mcp.log
```

### Enable Audit Logging

```bash
ENABLE_AUDIT_LOG=true
```

### Test Connection Manually

```python
from src.config import settings
from src.auth import AuthHandler
from src.ews_client import EWSClient

auth = AuthHandler(settings)
client = EWSClient(settings, auth)

# Test connection
print(client.test_connection())

# Test inbox access
print(client.account.inbox.total_count)
```

## Common Error Messages

### "Module 'mcp' not found"

**Solution:**
```bash
pip install mcp
# Or
pip install -r requirements.txt
```

### "exchangelib.errors.UnauthorizedError"

**Solution:**
- Check credentials are correct
- Verify account is not locked
- Check multi-factor authentication isn't blocking access
- Try different auth method

### "SSL Certificate Verification Failed"

**Solution:**
```bash
# For development only - DO NOT use in production
# Edit src/ews_client.py
# SSL verification is disabled by default via NoVerifyHTTPAdapter
```

### "Invalid folder name"

**Valid folder names:**
- inbox
- sent
- drafts
- deleted
- junk

## Docker Build Issues

### Problem: "pip install failed with exit code 1"

**Symptoms:**
```
ERROR: Cannot install pydantic-settings==2.1.0 and pydantic==2.5.3
because these package versions have conflicting dependencies.
```

**Solution:**
This error occurs with older versions of requirements.txt. Ensure you have the latest version:

```bash
# Pull latest changes
git pull origin main

# Verify requirements.txt has correct versions
grep "pydantic" requirements.txt
# Should show:
# pydantic>=2.8.0
# pydantic-settings>=2.5.2
```

**Root Cause:**
- `mcp>=1.0.0` requires `pydantic>=2.8.0` and `pydantic-settings>=2.5.2`
- Older requirements.txt had `pydantic==2.5.3` and `pydantic-settings==2.1.0`
- Fixed in commit `b286d92` and later

### Problem: "Docker build fails on Alpine Linux"

**Symptoms:**
```
ERROR: failed to build: exchangelib dependencies cannot be installed
```

**Solution:**
The Dockerfile has been updated to use Debian slim instead of Alpine:

```dockerfile
# Correct (current version)
FROM python:3.11-slim

# Incorrect (old version)
FROM python:3.11-alpine
```

**If you see this error:**
1. Pull latest code: `git pull origin main`
2. Rebuild without cache: `docker build --no-cache -t ews-mcp-server .`

### Problem: GitHub Actions workflows failing

**Symptoms:**
- "buildx failed with: ERROR: failed to build"
- "Process completed with exit code 1"
- Python tests being canceled

**Solutions:**

1. **For dependency conflicts:**
   - Ensure branch has latest commit with fixed requirements.txt
   - Check commit hash in workflow logs matches latest
   - Old commit: `f68e530` (broken)
   - Fixed commit: `b286d92` or later

2. **For attestation errors:**
   - Workflows have been updated to remove problematic attestation
   - Pull latest `.github/workflows/docker-publish.yml`

3. **For matrix cancellation:**
   - Workflows now have `fail-fast: false`
   - Tests run in non-blocking mode
   - Pull latest `.github/workflows/python-tests.yml`

## Getting Help

1. **Check Logs:** Always check logs first
2. **Enable Debug Mode:** Set LOG_LEVEL=DEBUG
3. **Test Connection:** Use test_connection() method
4. **Isolate Issue:** Test with minimal configuration
5. **Check Documentation:** Review relevant docs sections
6. **Verify Dependencies:** Check you have latest requirements.txt
7. **Check Commit:** Ensure using latest commit (not old PR branch)
8. **Create Issue:** If all else fails, create GitHub issue with:
   - Error message (redact credentials)
   - Configuration (redact secrets)
   - Steps to reproduce
   - Environment details (OS, Python version, Docker version)
   - Git commit hash

## Useful Commands

```bash
# Test Exchange connectivity
curl -v https://outlook.office365.com/EWS/Exchange.asmx

# Check Python version
python --version

# Check Docker version
docker --version
docker-compose --version

# View environment variables (be careful with secrets!)
env | grep EWS

# Test MSAL token acquisition
python -c "from msal import ConfidentialClientApplication; print('MSAL OK')"

# Validate .env file
python -c "from dotenv import load_dotenv; load_dotenv(); import os; print('Email:', os.getenv('EWS_EMAIL'))"
```
