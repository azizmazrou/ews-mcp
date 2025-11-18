#!/usr/bin/env python3
"""
Quick test script to verify GAL search is working with the new code.
Run this to test GAL search directly without starting the full MCP server.
"""

import sys
import os
import asyncio

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

print("\n" + "=" * 80)
print("QUICK GAL SEARCH TEST")
print("=" * 80 + "\n")

# Check version
print("Step 1: Verifying code version...")
with open('src/tools/contact_intelligence_tools.py', 'r') as f:
    if 'VERSION: 2025-11-18-ENHANCED-REVAMP' in f.read():
        print("✅ Running ENHANCED-REVAMP version\n")
    else:
        print("❌ NOT running enhanced version!\n")

# Import and setup
print("Step 2: Setting up EWS connection...")
try:
    from src.config import settings
    from src.auth import AuthHandler
    from src.ews_client import EWSClient
    from src.tools.contact_intelligence_tools import FindPersonTool

    print(f"   Connecting as: {settings.ews_email}")
    print(f"   Auth type: {settings.auth_type}")

    auth = AuthHandler(settings)
    client = EWSClient(settings, auth)

    print(f"✅ Connected to Exchange")
    print(f"   Server: {client.account.protocol.service_endpoint}\n")

except Exception as e:
    print(f"❌ Failed to connect: {e}\n")
    sys.exit(1)

# Test GAL search
print("Step 3: Testing GAL search...")
print("-" * 80)

async def test_gal_search():
    tool = FindPersonTool(client)

    # Try multiple queries
    test_queries = [
        ("a", "Single letter (should return many)"),
        ("Smith", "Common name"),
        ("admin", "Common username"),
    ]

    for query, description in test_queries:
        print(f"\nQuery: '{query}' - {description}")
        print("-" * 40)

        try:
            result = await tool.execute(
                query=query,
                search_scope="gal",
                max_results=5
            )

            total = result.get('total_results', 0)
            success = result.get('success', False)
            message = result.get('message', '')

            print(f"Success: {success}")
            print(f"Total results: {total}")
            print(f"Message: {message}")

            if total > 0:
                print(f"\nResults:")
                for i, contact in enumerate(result.get('unified_results', [])[:5]):
                    name = contact.get('name', 'N/A')
                    email = contact.get('email', 'N/A')
                    sources = contact.get('sources', [])
                    company = contact.get('company', '')

                    print(f"  {i+1}. {name} <{email}>")
                    print(f"     Sources: {sources}")
                    if company:
                        print(f"     Company: {company}")

                    # Check for phone numbers
                    phones = contact.get('phone_numbers', [])
                    if phones:
                        print(f"     ✅ Phone numbers found: {len(phones)}")
                        for p in phones[:2]:
                            print(f"        {p.get('type')}: {p.get('number')}")

                    business = contact.get('business_phone')
                    if business:
                        print(f"     Business phone: {business}")

                print(f"\n✅ GAL SEARCH IS WORKING!")
                return True
            else:
                print(f"\n⚠️  No results for '{query}'")

        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 80)
    print("❌ NO RESULTS FROM ANY QUERY")
    print("=" * 80)
    print("\nPossible causes:")
    print("1. GAL is empty or restricted on your Exchange server")
    print("2. Service account doesn't have permission to query GAL")
    print("3. Exchange server configuration issue")
    print("\nTry testing with direct resolve_names:")
    print(f"  python -c \"from exchangelib import *; a = Account(...); print(a.protocol.resolve_names(['a'], True))\"")
    return False

# Run the test
if asyncio.run(test_gal_search()):
    print("\n" + "=" * 80)
    print("✅ SUCCESS - GAL Search is working with enhanced code!")
    print("=" * 80)
    print("\nIf you're still getting 0 results via MCP client:")
    print("1. Restart your MCP server")
    print("2. Restart your MCP client (Claude Desktop, etc.)")
    print("3. Check the server is using the latest code")
    print("4. Try with LOG_LEVEL=DEBUG to see detailed logs")
else:
    print("\nNext steps:")
    print("1. Check Exchange server GAL configuration")
    print("2. Verify account has GAL read permissions")
    print("3. Try a broader query like 'a' or 'test'")
