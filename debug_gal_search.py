#!/usr/bin/env python3
"""
Diagnostic script to debug GAL search issues.
Tests GAL search at multiple levels to identify where the problem is.
"""

import sys
import os
import traceback
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

print("=" * 80)
print("GAL SEARCH DIAGNOSTIC TOOL")
print("=" * 80)
print()

# Step 1: Check version
print("[1/6] Checking code version...")
try:
    with open('src/tools/contact_intelligence_tools.py', 'r') as f:
        content = f.read()
        if 'VERSION: 2025-11-18-ENHANCED-REVAMP' in content:
            print("✅ ENHANCED-REVAMP version found")
        elif 'VERSION: 2025-11-18-COMPLETE-REVAMP' in content:
            print("⚠️  COMPLETE-REVAMP version found (old)")
        else:
            print("❌ Unknown version!")

        if '_search_gal' in content and 'Enhanced contact data extraction' in content:
            print("✅ Enhanced _search_gal method found")
        else:
            print("❌ Enhanced _search_gal method NOT found")
except Exception as e:
    print(f"❌ Error checking version: {e}")

print()

# Step 2: Check environment
print("[2/6] Checking environment variables...")
required_vars = ['EWS_EMAIL', 'EWS_AUTH_TYPE']
for var in required_vars:
    value = os.getenv(var)
    if value:
        print(f"✅ {var} is set: {value if var != 'EWS_PASSWORD' else '***'}")
    else:
        print(f"⚠️  {var} not set")

print()

# Step 3: Test imports
print("[3/6] Testing imports...")
try:
    from exchangelib import Account, Credentials
    print("✅ exchangelib imported")
except Exception as e:
    print(f"❌ Failed to import exchangelib: {e}")
    sys.exit(1)

try:
    from src.config import settings
    print("✅ settings imported")
    print(f"   Email: {settings.ews_email}")
    print(f"   Auth Type: {settings.auth_type}")
except Exception as e:
    print(f"❌ Failed to import settings: {e}")
    sys.exit(1)

print()

# Step 4: Test EWS connection
print("[4/6] Testing EWS connection...")
try:
    from src.auth import AuthHandler
    from src.ews_client import EWSClient

    auth_handler = AuthHandler(settings)
    ews_client = EWSClient(settings, auth_handler)

    print("✅ EWS client created")
    print(f"   Account: {ews_client.account.primary_smtp_address}")
    print(f"   Protocol version: {ews_client.account.version}")

    # Test basic connectivity
    inbox_count = ews_client.account.inbox.total_count
    print(f"✅ Connection works - Inbox has {inbox_count} items")
except Exception as e:
    print(f"❌ Failed to connect to EWS: {e}")
    traceback.print_exc()
    sys.exit(1)

print()

# Step 5: Test direct resolve_names
print("[5/6] Testing direct resolve_names() call...")
test_queries = [
    "Smith",      # Common English name
    "a",          # Single letter (should return many results)
    settings.ews_email.split('@')[0]  # Your own username
]

for query in test_queries:
    print(f"\n  Testing query: '{query}'")
    try:
        results = ews_client.account.protocol.resolve_names(
            names=[query],
            return_full_contact_data=True
        )

        if results:
            print(f"  ✅ Found {len(results)} result(s)")
            for i, result in enumerate(results[:3]):  # Show first 3
                if isinstance(result, tuple):
                    mailbox, contact_info = result[0], result[1] if len(result) > 1 else None
                    name = getattr(mailbox, 'name', 'N/A')
                    email = getattr(mailbox, 'email_address', 'N/A')
                    print(f"     [{i+1}] {name} <{email}>")
                    if contact_info:
                        company = getattr(contact_info, 'company_name', None)
                        if company:
                            print(f"         Company: {company}")
                else:
                    print(f"     [{i+1}] Object format: {result}")
        else:
            print(f"  ⚠️  No results found for '{query}'")

    except Exception as e:
        print(f"  ❌ Error: {e}")
        traceback.print_exc()

print()

# Step 6: Test via FindPersonTool
print("[6/6] Testing via FindPersonTool...")
try:
    from src.tools.contact_intelligence_tools import FindPersonTool

    tool = FindPersonTool(ews_client)

    # Test with a simple query
    test_query = "a"  # Should return many results
    print(f"\n  Testing find_person with query: '{test_query}'")

    import asyncio
    result = asyncio.run(tool.execute(query=test_query, search_scope="gal", max_results=5))

    print(f"  Success: {result.get('success')}")
    print(f"  Total results: {result.get('total_results')}")
    print(f"  Message: {result.get('message')}")

    if result.get('unified_results'):
        print(f"\n  First few results:")
        for i, contact in enumerate(result['unified_results'][:3]):
            print(f"    [{i+1}] {contact.get('name')} <{contact.get('email')}>")
            print(f"        Sources: {contact.get('sources')}")
            if contact.get('phone_numbers'):
                print(f"        Phones: {len(contact['phone_numbers'])} found")
    else:
        print("  ⚠️  No unified_results in response")

except Exception as e:
    print(f"  ❌ Error testing FindPersonTool: {e}")
    traceback.print_exc()

print()
print("=" * 80)
print("DIAGNOSTIC COMPLETE")
print("=" * 80)
print()
print("If you see ✅ for all steps but still get 0 results via MCP,")
print("please check:")
print("1. That you restarted the MCP server after updating the code")
print("2. That the MCP client is connecting to the right server")
print("3. The exact query you're using in the MCP call")
