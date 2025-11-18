#!/usr/bin/env python3
"""
MINIMAL test - directly tests Exchange resolve_names API
This bypasses all MCP infrastructure to test if GAL works at the Exchange level.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

print("\n" + "=" * 80)
print("DIRECT EXCHANGE GAL TEST (Minimal)")
print("=" * 80 + "\n")

try:
    from src.config import settings
    from src.auth import AuthHandler
    from src.ews_client import EWSClient

    print(f"Connecting to Exchange as: {settings.ews_email}\n")

    auth = AuthHandler(settings)
    client = EWSClient(settings, auth)
    account = client.account

    print(f"✅ Connected to: {account.protocol.service_endpoint}\n")

    # Test queries from simple to complex
    queries = ["a", "admin", "test", "Smith", "user"]

    print("Testing resolve_names() with different queries:")
    print("-" * 80)

    for query in queries:
        print(f"\nQuery: '{query}'")

        try:
            # This is the EXACT call our code uses
            results = account.protocol.resolve_names(
                names=[query],
                return_full_contact_data=True
            )

            if results:
                print(f"✅ Found {len(results)} result(s)!")

                # Show first result details
                result = results[0]
                print(f"\n   Result type: {type(result)}")

                if isinstance(result, tuple):
                    print(f"   Tuple length: {len(result)}")
                    mailbox = result[0]
                    contact_info = result[1] if len(result) > 1 else None

                    print(f"\n   Mailbox:")
                    print(f"      Name: {getattr(mailbox, 'name', 'N/A')}")
                    print(f"      Email: {getattr(mailbox, 'email_address', 'N/A')}")
                    print(f"      Type: {getattr(mailbox, 'mailbox_type', 'N/A')}")

                    if contact_info:
                        print(f"\n   Contact Info:")
                        print(f"      Display name: {getattr(contact_info, 'display_name', 'N/A')}")
                        print(f"      Company: {getattr(contact_info, 'company_name', 'N/A')}")
                        print(f"      Department: {getattr(contact_info, 'department', 'N/A')}")

                        # Check for phone numbers
                        if hasattr(contact_info, 'phone_numbers') and contact_info.phone_numbers:
                            print(f"      ✅ Phone numbers: {len(contact_info.phone_numbers)} found")
                            for p in contact_info.phone_numbers[:2]:
                                label = getattr(p, 'label', 'Unknown')
                                number = getattr(p, 'phone_number', 'N/A')
                                print(f"         {label}: {number}")
                    else:
                        print(f"\n   Contact Info: None")

                else:
                    print(f"   ⚠️  Result is not a tuple (unexpected format)")
                    print(f"   Result: {result}")

                # If we got here, GAL is working!
                print(f"\n{'=' * 80}")
                print(f"✅ SUCCESS - GAL IS WORKING!")
                print(f"{'=' * 80}")
                print(f"\nYour Exchange GAL is accessible and returning results.")
                print(f"The enhanced code should work correctly.\n")
                print(f"If you're still seeing 0 results in your MCP client:")
                print(f"  1. Make sure you restarted the MCP server")
                print(f"  2. Make sure you restarted the MCP client")
                print(f"  3. Check you're using search_scope='gal' not 'all'")
                print(f"  4. Check the server logs with LOG_LEVEL=DEBUG")
                break

            else:
                print(f"   ⚠️  0 results")

        except Exception as e:
            print(f"   ❌ Error: {e}")
            import traceback
            traceback.print_exc()

    else:
        # None of the queries returned results
        print(f"\n{'=' * 80}")
        print(f"❌ NO RESULTS FROM ANY QUERY")
        print(f"{'=' * 80}\n")
        print(f"This means:")
        print(f"  1. GAL might be empty on this Exchange server")
        print(f"  2. Your account ({settings.ews_email}) might not have GAL read permission")
        print(f"  3. GAL might be disabled or restricted\n")
        print(f"Solutions:")
        print(f"  - Ask your Exchange admin to check GAL permissions")
        print(f"  - Try with a different account")
        print(f"  - Check if you can see GAL in Outlook/OWA")

except Exception as e:
    print(f"\n❌ Failed to connect or test: {e}")
    import traceback
    traceback.print_exc()

print()
