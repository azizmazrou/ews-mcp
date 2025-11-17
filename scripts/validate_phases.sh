#!/bin/bash
# Phase 1 & 2 Validation Script

echo "🔍 Validating EWS MCP Server Phases 1 & 2..."
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PASS=0
FAIL=0

# Function to check file exists
check_file() {
    if [ -f "$1" ]; then
        echo -e "${GREEN}✓${NC} $2"
        ((PASS++))
    else
        echo -e "${RED}✗${NC} $2 (Missing: $1)"
        ((FAIL++))
    fi
}

# Function to check tool in file
check_tool() {
    if grep -q "class $1" "$2" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} $1 implemented"
        ((PASS++))
    else
        echo -e "${RED}✗${NC} $1 missing in $2"
        ((FAIL++))
    fi
}

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "PHASE 1: Core Infrastructure"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

check_file "src/main.py" "Main server file"
check_file "src/ews_client.py" "EWS client"
check_file "src/tools/base.py" "Base tool class"
check_file "docker-compose.yml" "Docker compose config"
check_file "Dockerfile" "Docker configuration"
check_file ".env.example" ".env.example"
check_file "requirements.txt" "Python dependencies"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "PHASE 2: MVP Tools (28 Required)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Email Tools (7)
echo ""
echo "Email Tools (7):"
check_tool "SendEmailTool" "src/tools/email_tools.py"
check_tool "ReadEmailsTool" "src/tools/email_tools.py"
check_tool "SearchEmailsTool" "src/tools/email_tools.py"
check_tool "GetEmailDetailsTool" "src/tools/email_tools.py"
check_tool "DeleteEmailTool" "src/tools/email_tools.py"
check_tool "MoveEmailTool" "src/tools/email_tools.py"
check_tool "UpdateEmailTool" "src/tools/email_tools.py"

# Calendar Tools (6)
echo ""
echo "Calendar Tools (6):"
check_tool "CreateAppointmentTool" "src/tools/calendar_tools.py"
check_tool "GetCalendarTool" "src/tools/calendar_tools.py"
check_tool "UpdateAppointmentTool" "src/tools/calendar_tools.py"
check_tool "DeleteAppointmentTool" "src/tools/calendar_tools.py"
check_tool "RespondToMeetingTool" "src/tools/calendar_tools.py"
check_tool "CheckAvailabilityTool" "src/tools/calendar_tools.py"

# Contact Tools (6)
echo ""
echo "Contact Tools (6):"
check_tool "CreateContactTool" "src/tools/contact_tools.py"
check_tool "SearchContactsTool" "src/tools/contact_tools.py"
check_tool "GetContactsTool" "src/tools/contact_tools.py"
check_tool "UpdateContactTool" "src/tools/contact_tools.py"
check_tool "DeleteContactTool" "src/tools/contact_tools.py"
check_tool "ResolveNamesTool" "src/tools/contact_tools.py"

# Task Tools (5)
echo ""
echo "Task Tools (5):"
check_tool "CreateTaskTool" "src/tools/task_tools.py"
check_tool "GetTasksTool" "src/tools/task_tools.py"
check_tool "UpdateTaskTool" "src/tools/task_tools.py"
check_tool "CompleteTaskTool" "src/tools/task_tools.py"
check_tool "DeleteTaskTool" "src/tools/task_tools.py"

# Attachment Tools (2)
echo ""
echo "Attachment Tools (2):"
check_file "src/tools/attachment_tools.py" "Attachment tools file"
check_tool "ListAttachmentsTool" "src/tools/attachment_tools.py"
check_tool "DownloadAttachmentTool" "src/tools/attachment_tools.py"

# Search Tools (1)
echo ""
echo "Search Tools (1):"
check_file "src/tools/search_tools.py" "Search tools file"
check_tool "AdvancedSearchTool" "src/tools/search_tools.py"

# Folder Tools (1)
echo ""
echo "Folder Tools (1):"
check_file "src/tools/folder_tools.py" "Folder tools file"
check_tool "ListFoldersTool" "src/tools/folder_tools.py"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Docker & Configuration"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

check_file "docker-compose.yml" "Docker Compose"
check_file ".env.example" "Environment template"
check_file "logs/.gitkeep" "Logs directory"
check_file "data/attachments/temp/.gitkeep" "Temp attachments directory"
check_file "data/attachments/saved/.gitkeep" "Saved attachments directory"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Documentation"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

check_file "README.md" "Main README"
check_file "docs/API.md" "API Documentation"
check_file "docs/SETUP.md" "Setup Guide"
check_file "docs/DEPLOYMENT.md" "Deployment Guide"
check_file "CHANGELOG.md" "Change Log"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Tests"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

check_file "tests/test_email_tools.py" "Email tool tests"
check_file "tests/test_calendar_tools.py" "Calendar tool tests"
check_file "tests/test_contact_tools.py" "Contact tool tests"
check_file "tests/test_task_tools.py" "Task tool tests"
check_file "tests/test_attachment_tools.py" "Attachment tool tests"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "VALIDATION SUMMARY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

TOTAL=$((PASS + FAIL))
PERCENTAGE=$((PASS * 100 / TOTAL))

echo ""
echo -e "Total Checks: $TOTAL"
echo -e "${GREEN}Passed: $PASS${NC}"
echo -e "${RED}Failed: $FAIL${NC}"
echo -e "Completion: ${PERCENTAGE}%"
echo ""

if [ $FAIL -eq 0 ]; then
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}✓ ALL CHECKS PASSED - READY FOR PHASE 3${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 0
elif [ $PERCENTAGE -ge 90 ]; then
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}⚠ MOSTLY COMPLETE - FIX REMAINING ISSUES${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
else
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}✗ INCOMPLETE - COMPLETE PHASE 1 & 2 FIRST${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
fi
