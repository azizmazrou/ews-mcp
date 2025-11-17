# EWS MCP Server - 30 Natural Language Test Prompts

Test all functionality using these conversational prompts through MCP clients (n8n, Claude Desktop, etc.)

---

## 📧 Email Operations (10 Test Prompts)

### Test 1: Send Simple Email
**Prompt:**
```
Send an email to colleague@example.com with the subject "Test Email #1" and body "This is a test email to verify the email sending functionality is working correctly."
```

**Validates:** Basic email sending, authentication working

---

### Test 2: Send Email with Multiple Recipients
**Prompt:**
```
Send an email to team@example.com, copy manager@example.com, and blind copy hr@example.com with subject "Team Update #2" and message "Weekly team update for all stakeholders."
```

**Validates:** CC and BCC functionality, multiple recipients

---

### Test 3: Send HTML Formatted Email
**Prompt:**
```
Send an HTML email to recipient@example.com with subject "Formatted Email #3" containing bold text "Important Notice" and a bulleted list of three items.
```

**Validates:** HTML email formatting, body_type parameter

---

### Test 4: Read Latest Emails from Inbox
**Prompt:**
```
Show me the last 10 emails in my inbox with their subjects, senders, and received times.
```

**Validates:** Reading emails, folder access, inbox operations

---

### Test 5: Check Sent Items
**Prompt:**
```
What are the last 5 emails I sent? Show me the recipients and subjects.
```

**Validates:** Sent items folder, sent email retrieval

---

### Test 6: Search Emails by Subject
**Prompt:**
```
Find all emails in my inbox that have the word "meeting" in the subject line. Show up to 20 results.
```

**Validates:** Email search functionality, subject filtering

---

### Test 7: Find Emails from Specific Sender
**Prompt:**
```
Show me all emails from boss@company.com in my inbox.
```

**Validates:** Sender filtering, from_address search

---

### Test 8: Search Emails by Date Range (TIMEZONE TEST!)
**Prompt:**
```
Find all emails I received between November 1st and November 5th, 2025 in Asia/Riyadh timezone.
```

**Validates:** ⏰ CRITICAL - Date range filtering, timezone handling, no "UTC+03:00" error

---

### Test 9: Get Full Email Details
**Prompt:**
```
Show me the complete details of the most recent email in my inbox, including the full body text and any attachment information.
```

**Validates:** Email details retrieval, body content access

---

### Test 10: Find Unread Emails
**Prompt:**
```
List all my unread emails in the inbox. Show me the subjects and who they're from.
```

**Validates:** Unread filter, is_read parameter

---

## 📅 Calendar Operations (8 Test Prompts)

### Test 11: View Today's Schedule (TIMEZONE TEST!)
**Prompt:**
```
What's on my calendar today, November 5th, 2025? Show all events with their times in Asia/Riyadh timezone.
```

**Validates:** ⏰ CRITICAL - Calendar retrieval, timezone handling, today's date filtering

---

### Test 12: View This Week's Calendar
**Prompt:**
```
Show me all my calendar appointments for the next 7 days. Include the subject, time, and location for each.
```

**Validates:** Default date range (7 days), calendar view

---

### Test 13: Create Simple Meeting (TIMEZONE TEST!)
**Prompt:**
```
Schedule a meeting for tomorrow, November 6th at 10:00 AM until 11:00 AM Asia/Riyadh time. Call it "Project Review Meeting #13" with description "Quarterly project status review."
```

**Validates:** ⏰ CRITICAL - Appointment creation, timezone handling in start/end times

---

### Test 14: Create Meeting with Location and Reminder
**Prompt:**
```
Book a meeting room for November 7th from 2:00 PM to 3:30 PM. Title: "Budget Planning #14", Location: "Conference Room A, Building 1", and set a reminder for 30 minutes before.
```

**Validates:** Location field, reminder settings, longer meeting duration

---

### Test 15: Schedule Meeting with Attendees
**Prompt:**
```
Create a team meeting for November 8th at 9:00 AM for one hour. Title: "Weekly Team Sync #15", invite colleague1@company.com and colleague2@company.com, location is "Virtual - Teams".
```

**Validates:** Meeting invitations, attendee list, virtual meetings

---

### Test 16: Create All-Day Event
**Prompt:**
```
Mark November 10th, 2025 as "Company Holiday #16" - all day event with note "Office closed for national holiday."
```

**Validates:** All-day events, is_all_day parameter

---

### Test 17: Modify Existing Appointment
**Prompt:**
```
Find the "Project Review Meeting #13" from Test 13 and change the time to 11:00 AM - 12:00 PM on the same day. Also update the title to "UPDATED: Project Review Meeting #13".
```

**Validates:** Update appointment, time modification, subject change

---

### Test 18: Cancel an Appointment
**Prompt:**
```
Delete the "Company Holiday #16" event from my calendar.
```

**Validates:** Delete appointment functionality

---

## 👤 Contact Operations (6 Test Prompts)

### Test 19: Create Detailed Contact
**Prompt:**
```
Add a new contact: John Doe, email john.doe@example.com, phone +966123456789, works at Example Corp as a Software Engineer.
```

**Validates:** Full contact creation with all fields

---

### Test 20: Create Basic Contact
**Prompt:**
```
Save a contact for Jane Smith with email jane.smith@example.com. That's all the info I have right now.
```

**Validates:** Minimal contact creation, required fields only

---

### Test 21: Search Contacts by Name
**Prompt:**
```
Find all contacts with "John" in their name.
```

**Validates:** Contact search, name query

---

### Test 22: List All Contacts
**Prompt:**
```
Show me all my contacts. Display their names, email addresses, and companies.
```

**Validates:** Get all contacts, contact list retrieval

---

### Test 23: Update Contact Information
**Prompt:**
```
Update John Doe's contact: change his job title to "Senior Software Engineer" and company to "Updated Corp".
```

**Validates:** Contact update, field modification

---

### Test 24: Remove a Contact
**Prompt:**
```
Delete Jane Smith from my contacts.
```

**Validates:** Contact deletion

---

## ✅ Task Operations (6 Test Prompts)

### Test 25: Create Simple Task (TIMEZONE TEST!)
**Prompt:**
```
Create a task "Complete Report #25" with description "Finish quarterly report" due November 10th, 2025 at 5:00 PM Asia/Riyadh time.
```

**Validates:** ⏰ CRITICAL - Task creation, due date with timezone

---

### Test 26: Create Detailed High-Priority Task
**Prompt:**
```
Add a high-priority task "Project Deliverable #26" starting November 5th at 9:00 AM, due November 15th at 5:00 PM. Description: "Complete all Q4 deliverables with metrics." Set reminder for November 15th at 9:00 AM.
```

**Validates:** Task with start date, due date, priority, reminder

---

### Test 27: View All Tasks
**Prompt:**
```
Show me all my tasks. Include the subject, due date, and completion status for each.
```

**Validates:** Task list retrieval, get all tasks

---

### Test 28: Update Task Progress
**Prompt:**
```
Update "Complete Report #25" to 50% complete and add a note "Report is halfway done - working on metrics section."
```

**Validates:** Task progress update, percent_complete field

---

### Test 29: Mark Task as Complete
**Prompt:**
```
Mark the "Project Deliverable #26" task as completed.
```

**Validates:** Complete task, 100% completion

---

### Test 30: Delete a Task
**Prompt:**
```
Remove the "Complete Report #25" task from my task list.
```

**Validates:** Task deletion

---

## ✅ Critical Tests Validation Checklist

### 🌍 Timezone Fix Validation (HIGH PRIORITY)
These prompts specifically test the timezone fix:

- [ ] **Test 8** - Email search with date range ⏰
  - **Expected:** Returns emails for Nov 1-5 in Asia/Riyadh time
  - **Error Check:** NO "UTC+03:00" error

- [ ] **Test 11** - Today's calendar in Asia/Riyadh ⏰
  - **Expected:** Shows today's events in correct timezone
  - **Error Check:** NO "UTC+03:00" error

- [ ] **Test 13** - Create appointment with timezone ⏰
  - **Expected:** Meeting created at correct time
  - **Error Check:** NO "UTC+03:00" error

- [ ] **Test 25** - Create task with due date timezone ⏰
  - **Expected:** Task with correct due date/time
  - **Error Check:** NO "UTC+03:00" error

### 🔐 Authentication Fix Validation
All 30 tests validate authentication:

- [ ] Server starts without errors
- [ ] All requests authenticate successfully
- [ ] NO "auth requires username/password" errors

### 🌐 HTTP/SSE Transport Validation
All 30 tests validate transport:

- [ ] All prompts execute via MCP protocol
- [ ] Responses returned correctly
- [ ] NO connection timeouts or errors

### 📦 Dependency Fix Validation
Specific tests validate dependencies:

- [ ] **Tests 1-3** - email-validator working (send email)
- [ ] **Tests 8, 11, 13, 25** - pytz/EWSTimeZone working (timezone ops)
- [ ] **Test 19** - EmailAddress import working (contact creation)

---

## 🎯 Testing Strategy

### Phase 1: Quick Smoke Test (5 tests)
Run these to verify basic functionality:
1. **Test 1** - Send email
2. **Test 4** - Read emails
3. **Test 11** - View calendar ⏰
4. **Test 22** - List contacts
5. **Test 27** - List tasks

**Goal:** Confirm server is operational and all tool types work

---

### Phase 2: Timezone Validation (4 tests)
Run these to verify timezone fix:
1. **Test 8** - Email date search ⏰
2. **Test 11** - Calendar today ⏰
3. **Test 13** - Create meeting ⏰
4. **Test 25** - Create task ⏰

**Goal:** Confirm NO "UTC+03:00" errors occur

---

### Phase 3: Full Coverage (30 tests)
Run all tests in order to validate:
- All CRUD operations (Create, Read, Update, Delete)
- All tools functional
- Edge cases handled
- Complete integration

**Goal:** 30/30 tests pass

---

## 📊 Success Criteria

### Perfect Score: 30/30 ✅

**Email Operations:** 10/10 tests pass
- Send (3), Read (2), Search (4), Details (1)

**Calendar Operations:** 8/8 tests pass
- View (2), Create (4), Update (1), Delete (1)

**Contact Operations:** 6/6 tests pass
- Create (2), Search (1), List (1), Update (1), Delete (1)

**Task Operations:** 6/6 tests pass
- Create (2), List (1), Update (1), Complete (1), Delete (1)

### Zero Critical Errors ✅

- [ ] ✅ NO "No time zone found with key UTC+03:00" errors
- [ ] ✅ NO "auth requires username/password" errors
- [ ] ✅ NO "cannot import EmailAddress" errors
- [ ] ✅ NO "email-validator not installed" errors
- [ ] ✅ NO connection or timeout errors

---

## 📝 Test Execution Notes

### For n8n Users:
1. Create an HTTP Request node
2. POST to: `http://localhost:8000/messages`
3. Convert each prompt to proper MCP JSON-RPC format
4. Check response for `"success": true`

### For Claude Desktop Users:
1. Configure EWS MCP server in settings
2. Type each prompt naturally in chat
3. Claude will use MCP to execute operations
4. Verify responses contain expected data

### For MCP Client Developers:
1. Send prompts via MCP tools/call method
2. Map natural language to tool parameters
3. Validate JSON-RPC responses
4. Check for error messages

---

## 🔍 Response Validation

### Successful Response Should Include:
```
✅ "success": true
✅ Relevant data (emails, events, contacts, tasks)
✅ No error messages
✅ Proper timestamps in configured timezone
```

### Failed Response Might Show:
```
❌ "success": false
❌ Error message explaining failure
❌ "No time zone found with key UTC+XX:XX" (timezone error)
❌ "auth requires..." (authentication error)
```

---

## 🎉 Completion Checklist

After running all 30 tests, verify:

### Functionality ✅
- [ ] Can send emails (Tests 1-3)
- [ ] Can read/search emails (Tests 4-10)
- [ ] Can manage calendar (Tests 11-18)
- [ ] Can manage contacts (Tests 19-24)
- [ ] Can manage tasks (Tests 25-30)

### Timezone Handling ✅
- [ ] Asia/Riyadh timezone working
- [ ] No UTC offset errors
- [ ] Dates/times display correctly
- [ ] All datetime operations succeed

### Integration ✅
- [ ] MCP protocol working
- [ ] HTTP/SSE transport operational
- [ ] Authentication successful
- [ ] All dependencies loaded

---

## 🚀 Production Readiness

### If All 30 Tests Pass:

✅ **EWS MCP Server is PRODUCTION READY**

You have validated:
- Complete functionality across all tools
- Timezone support for Asia/Riyadh (and all IANA zones)
- Authentication working correctly
- HTTP/SSE transport operational
- All dependencies installed and working
- Zero critical errors

### Your server supports:
- ✉️ Email management
- 📅 Calendar/appointment management
- 👤 Contact management
- ✅ Task management
- 🌍 Proper timezone handling
- 🔐 Secure authentication
- 🌐 HTTP/API access

**Ready for use in production workflows! 🎉**

---

## 📚 Additional Resources

- **Full Documentation:** See `README.md`
- **Connection Guide:** See `docs/CONNECTION_GUIDE.md`
- **Timezone Fix Details:** See `docs/TIMEZONE_FIX.md`
- **Quick Start:** See `QUICKSTART.md`
- **Technical Tests:** See `TEST_SUITE.md` (curl commands)
