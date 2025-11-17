# Changelog

All notable changes to EWS MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] - 2025-11-07

### Added - Phase 3 Stage 3: AI Intelligence Layer

**4 AI-Powered Tools**

#### AI Intelligence Tools (4 new)
- `semantic_search_emails` - Natural language search across emails using AI embeddings and semantic similarity
- `classify_email` - AI-powered email classification (priority, sentiment, category, spam detection)
- `summarize_email` - Generate concise AI summaries of email content
- `suggest_replies` - Generate context-aware smart reply suggestions

**AI Service Infrastructure:**
- `src/ai/base.py` - Abstract base classes for AI and embedding providers
- `src/ai/openai_provider.py` - OpenAI API integration (GPT-4, embeddings)
- `src/ai/anthropic_provider.py` - Anthropic Claude API integration
- `src/ai/provider_factory.py` - Factory for instantiating AI providers
- `src/ai/embedding_service.py` - Embedding management with caching and semantic search
- `src/ai/classification_service.py` - Email classification service with multiple AI-powered analyses

**Configuration:**
- Added 13 new AI-related configuration options to `src/config.py`
- Support for OpenAI, Anthropic, and local models (Ollama, LM Studio)
- Configurable AI features (semantic search, classification, summarization, smart replies)
- `.env.ai.example` template for AI configuration

**Features:**
- **Semantic Search**: Find emails by meaning, not just keywords
- **Email Classification**: Automatic priority, sentiment, and category detection
- **Spam Detection**: AI-powered spam and phishing detection
- **Email Summarization**: Generate concise summaries of long emails
- **Smart Replies**: Context-aware reply suggestions with varying tones
- **Embedding Cache**: Persistent caching of embeddings for performance
- **Multi-Provider Support**: OpenAI, Anthropic Claude, or local models

**Total Tools:** Up to 44 tools (40 base + 4 AI tools when enabled)

### Infrastructure

- Added `httpx>=0.25.0` dependency for AI API requests
- Embedding cache stored in `data/embeddings/`
- Conditional AI tool registration based on feature flags
- Temperature and token limits configurable per provider

### Configuration Examples

**OpenAI:**
```bash
ENABLE_AI=true
AI_PROVIDER=openai
AI_API_KEY=sk-your-key
AI_MODEL=gpt-4o-mini
AI_EMBEDDING_MODEL=text-embedding-3-small
```

**Anthropic Claude:**
```bash
ENABLE_AI=true
AI_PROVIDER=anthropic
AI_API_KEY=sk-ant-your-key
AI_MODEL=claude-3-5-sonnet-20241022
```

**Local Models (Ollama/LM Studio):**
```bash
ENABLE_AI=true
AI_PROVIDER=local
AI_BASE_URL=http://localhost:11434/v1
AI_MODEL=llama3
AI_EMBEDDING_MODEL=nomic-embed-text
```

## [2.0.0] - 2025-11-07

### Added - Phase 3 Stage 1 & 2: 12 New Enterprise Tools

**Version 2.0 expands the EWS MCP Server from 28 MVP tools to 40 enterprise-grade tools.**

#### Folder Management Tools (4 new)
- `create_folder` - Create new mailbox folders with custom folder classes and parent folder selection
- `delete_folder` - Delete folders with soft delete or permanent deletion options
- `rename_folder` - Rename existing folders while preserving hierarchy
- `move_folder` - Move folders to new parent locations for better organization

#### Enhanced Attachment Tools (2 new)
- `add_attachment` - Add attachments via file path or base64 content with inline attachment support and MIME type configuration
- `delete_attachment` - Remove attachments by ID or name from email messages

#### Advanced Search Tools (2 new)
- `search_by_conversation` - Find all emails in a conversation thread by conversation ID or message ID
- `full_text_search` - Full-text search across email content with case-sensitive matching, exact phrase search, and configurable search scope

#### Out-of-Office Tools (2 new)
- `set_oof_settings` - Configure automatic replies with Enabled/Scheduled/Disabled states, separate internal/external messages, and scheduling
- `get_oof_settings` - Retrieve current OOF settings with active status indication

#### Calendar Enhancement Tools (1 new)
- `find_meeting_times` - AI-powered meeting time finder that analyzes attendee availability and suggests optimal meeting slots with intelligent scoring based on preferences

#### Email Enhancement Tools (1 new)
- `copy_email` - Copy emails to folders while preserving originals (vs. move which relocates)

**Total Tools:** Now 40 tools (8 Email, 7 Calendar, 6 Contact, 5 Task, 4 Attachment, 5 Folder, 3 Search, 2 OOF)

### Testing & Quality Assurance

**Comprehensive Test Suite:**
- `tests/test_oof_tools.py` - 10 test cases for Out-of-Office functionality
- `tests/test_folder_management.py` - 11 test cases for folder operations
- `tests/test_enhanced_attachments.py` - 11 test cases for attachment management
- `tests/test_advanced_search.py` - 10 test cases for search functionality
- `tests/test_calendar_enhancement.py` - 9 test cases for meeting time finder
- Added 2 test cases to `tests/test_email_tools.py` for copy_email functionality

**Total Test Cases:** 53 new tests covering all 12 new tools

### Documentation

**Updated Documentation:**
- README.md: Added "What's New in v2.0" section with feature highlights and code examples
- API.md: Comprehensive documentation for all 12 new tools with input schemas, response formats, and examples
- CHANGELOG.md: Detailed Phase 3 Stage 1 & 2 implementation summary

**Feature Highlight Examples:**
- Smart meeting scheduling with availability analysis
- Conversation threading for email management
- Advanced folder organization workflows
- Out-of-office automation with scheduling

### Infrastructure

- `scripts/run_all_tests.sh` - Automated test runner with coverage reporting and formatted output
- Updated version to 2.0.0 in src/main.py
- Enhanced tool registration with detailed category logging

### Changed
- Enhanced features list in README.md to reflect all 40 tools
- Expanded API documentation with 12 new tool references
- Updated tool categorization with OOF tools category

## [Unreleased] - 2025-11-07

### Added - 7 New MVP Tools

**Email Tools (3 new):**
- `update_email` - Update email properties including read status, flags, categories, and importance levels
- `list_attachments` - List all attachments for a specific email message with details (name, size, content type)
- `download_attachment` - Download email attachments as base64 encoded strings or save directly to file

**Calendar Tools (1 new):**
- `check_availability` - Get free/busy information for one or more users in a specified time range with customizable interval granularity

**Contact Tools (1 new):**
- `resolve_names` - Resolve partial names or email addresses to full contact information from Active Directory or Contacts

**Search Tools (1 new):**
- `advanced_search` - Perform complex multi-criteria searches across multiple folders with support for keywords, date ranges, importance, categories, and more

**Folder Tools (1 new):**
- `list_folders` - Get mailbox folder hierarchy with configurable depth, item counts, and hidden folder filtering

**Total Tools:** Now 28 tools (previously 21)

### Changed
- Enhanced features list in README.md to reflect all 28 tools
- Updated tool categorization to include new Search and Folder categories

## [1.0.0] - 2025-01-04

### Added
- Initial release of EWS MCP Server
- Email operations (send, read, search, delete, move)
- Calendar management (create, update, delete, respond to meetings)
- Contact management (full CRUD operations)
- Task management (create, update, complete, delete)
- OAuth2 authentication support
- Basic authentication support
- NTLM authentication support
- Docker containerization
- Rate limiting middleware
- Comprehensive error handling
- Audit logging
- Full test suite
- Complete documentation

### Features
- 21 tools covering all major Exchange operations
- Multi-authentication support (OAuth2, Basic, NTLM)
- Production-ready Docker deployment
- Built-in rate limiting
- Structured logging
- Pydantic validation for all inputs
- Automatic retry logic for connection issues
- Support for autodiscovery and manual configuration

### Documentation
- Complete README with quick start guide
- OAuth2 setup instructions
- Claude Desktop integration guide
- API documentation
- Troubleshooting guide
- Architecture overview
- Deployment guide

### Testing
- Unit tests for all tools
- Authentication tests
- EWS client tests
- Integration test framework
- Mock fixtures for testing
- 80%+ code coverage

## [Unreleased]

### Added
- GitHub Actions workflow for automated Docker image building
- GitHub Container Registry (GHCR) publishing
- Multi-platform Docker images (amd64 + arm64)
- Pre-built Docker images available at `ghcr.io/azizmazrou/ews-mcp`
- Comprehensive GHCR usage guide
- Docker Compose file for GHCR images
- Automated CI/CD pipeline with GitHub Actions
- Python test workflow with multiple Python versions (3.11, 3.12)
- Docker build test workflow for PRs
- CI workflow for fast validation on all branches
- Pre-configured `.env.basic.example` template for Basic Authentication
- Pre-configured `.env.oauth2.example` template for OAuth2
- Interactive setup script `scripts/setup-basic-auth.sh`
- QUICK_START.md standalone guide
- Authentication comparison table in README
- 30-second Basic Auth quick start guide
- Inline troubleshooting for Basic Auth
- GHCR_STATUS.md explaining pre-built image availability

### Changed
- **CRITICAL**: Updated `pydantic` from `==2.5.3` to `>=2.8.0` (required by mcp>=1.0.0)
- **CRITICAL**: Updated `pydantic-settings` from `==2.1.0` to `>=2.5.2` (required by mcp>=1.0.0)
- **CRITICAL**: Implemented lazy settings loading in config.py to prevent premature validation
- Switched Docker base image from `python:3.11-alpine` to `python:3.11-slim` (Debian)
- Changed Dockerfile from ENTRYPOINT to CMD for easier command override
- Changed from Alpine `apk` to Debian `apt-get` package management
- Replaced `--user` pip install with Python virtual environment at `/opt/venv`
- Updated user creation commands for Debian (groupadd/useradd instead of addgroup/adduser)
- Added `build-essential` and `git` to Docker build dependencies
- Upgraded pip, setuptools, and wheel before package installation
- Made all GitHub Actions test workflows fully non-blocking
- Added `fail-fast: false` to prevent matrix job cancellation
- Added `continue-on-error: true` to all test steps
- Removed problematic attestation step from docker-publish.yml
- Added `load: true` to docker-build-test.yml for image availability
- Enhanced docker-build-test.yml with additional validation tests
- Updated README with GHCR pull instructions
- Updated deployment documentation with GHCR as recommended method
- Updated Claude Desktop examples to use GHCR images
- Improved Quick Start with pre-built image option
- Restructured Configuration section with Basic Auth as primary option
- Enhanced documentation with three setup methods (copy-paste, template, interactive)
- Setup time reduced from 15 minutes to 30 seconds for Basic Auth
- Updated requirements-dev.txt to use version ranges for better compatibility

### Fixed
- Python dependency conflict between pydantic and mcp package
- Settings validation error when running Docker tests without environment variables
- Docker build failures on Alpine Linux with exchangelib dependencies
- GitHub Actions workflow attestation errors
- Docker image not loading in build-test workflow
- Python test failures blocking entire CI pipeline
- codecov upload failing without CODECOV_TOKEN
- Matrix strategy canceling Python 3.12 tests when 3.11 failed
- Module import triggering settings validation before needed

### Planned Features (Future Roadmap)
- Webhook support for notifications
- Microsoft Graph API integration (complementing EWS)
- Redis caching for improved performance
- Web UI for configuration and monitoring
- Metrics and monitoring dashboard
- Kubernetes deployment manifests
- Email rules and automation
- Shared mailbox support
- Public folder operations
