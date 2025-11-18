# Architecture Overview - v3.0

Technical architecture and design decisions for EWS MCP Server v3.0 (Person-Centric Rewrite).

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     AI Assistant (Claude)                    │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP Protocol (stdio/SSE)
┌──────────────────────────▼──────────────────────────────────┐
│                   EWS MCP Server v3.0                        │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              MCP Protocol Handler                       │ │
│  │  - list_tools()                                        │ │
│  │  - call_tool(name, arguments)                          │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               │                                              │
│  ┌────────────▼───────────────────────────────────────────┐ │
│  │              Tool Registry & Router                     │ │
│  │  - Contact Intelligence (3)  - Email (9)               │ │
│  │  - Calendar (6)              - Contacts (6)            │ │
│  │  - Tasks (5)                 - Search (3)              │ │
│  │  - Folders (5)               - Out-of-Office (2)       │ │
│  │  - Attachments (5)                                     │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               │                                              │
│  ┌────────────▼───────────────────────────────────────────┐ │
│  │              Service Layer (NEW in v3.0)               │ │
│  │  - PersonService (person discovery)                    │ │
│  │  - EmailService (email operations)                     │ │
│  │  - ThreadService (conversation threading)              │ │
│  │  - AttachmentService (all format support)              │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               │                                              │
│  ┌────────────▼───────────────────────────────────────────┐ │
│  │              Adapter Layer (NEW in v3.0)               │ │
│  │  - GALAdapter (multi-strategy search)                  │ │
│  │  - CacheAdapter (intelligent caching)                  │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               │                                              │
│  ┌────────────▼───────────────────────────────────────────┐ │
│  │              Core Models (NEW in v3.0)                 │ │
│  │  - Person (first-class entity)                         │ │
│  │  - EmailMessage                                        │ │
│  │  - ConversationThread                                  │ │
│  │  - Attachment                                          │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               │                                              │
│  ┌────────────▼───────────────────────────────────────────┐ │
│  │              Middleware Layer                           │ │
│  │  - Rate Limiter                                        │ │
│  │  - Error Handler                                       │ │
│  │  - Audit Logger                                        │ │
│  │  - Input Validator (Pydantic)                          │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               │                                              │
│  ┌────────────▼───────────────────────────────────────────┐ │
│  │              EWS Client Wrapper                         │ │
│  │  - Connection Management                               │ │
│  │  - Retry Logic (Tenacity)                              │ │
│  │  - Autodiscovery Support                               │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               │                                              │
│  ┌────────────▼───────────────────────────────────────────┐ │
│  │          Authentication Handler                         │ │
│  │  - OAuth2 (MSAL)                                       │ │
│  │  - Basic Auth                                          │ │
│  │  - NTLM                                                │ │
│  └────────────┬───────────────────────────────────────────┘ │
└───────────────┼──────────────────────────────────────────────┘
                │ HTTPS
┌───────────────▼──────────────────────────────────────────────┐
│        Microsoft Exchange Web Services (EWS)                 │
│  - Office 365 / Exchange Online                              │
│  - Exchange Server (On-Premises)                             │
└──────────────────────────────────────────────────────────────┘
```

## v3.0 Directory Structure

```
src/
├── core/                          # NEW - Domain models
│   ├── person.py                  # Person entity (CORE!)
│   ├── email_message.py           # EmailMessage entity
│   ├── thread.py                  # ConversationThread entity
│   └── attachment.py              # Attachment entities
│
├── services/                      # NEW - Business logic layer
│   ├── person_service.py          # PersonService (CRITICAL!)
│   ├── email_service.py           # EmailService
│   ├── thread_service.py          # ThreadService
│   └── attachment_service.py      # AttachmentService
│
├── adapters/                      # NEW - External integrations
│   ├── gal_adapter.py             # GALAdapter (GAL FIX!)
│   └── cache_adapter.py           # CacheAdapter
│
├── tools/                         # MCP tools
│   ├── email_tools.py
│   ├── calendar_tools.py
│   ├── contact_tools.py
│   ├── task_tools.py
│   ├── folder_tools.py
│   ├── search_tools.py
│   ├── attachment_tools.py
│   ├── oof_tools.py
│   └── contact_intelligence_tools.py  # Uses PersonService!
│
├── middleware/
│   ├── rate_limiter.py
│   ├── error_handler.py
│   └── logging.py                 # Enterprise logging
│
├── ews_client.py
├── auth.py
├── config.py
├── utils.py
└── main.py
```

## Component Design

### 1. Core Models (`src/core/`)

**Design Pattern:** Domain-Driven Design

The heart of v3.0 - pure domain models with no external dependencies.

#### Person Model (`person.py`)
The **primary entity** in v3.0 architecture.

```python
class Person(BaseModel):
    """A real human being in your professional network."""
    id: str
    name: str
    email_addresses: List[EmailAddress]
    phone_numbers: List[PhoneNumber]
    organization: Optional[str]
    department: Optional[str]
    job_title: Optional[str]
    office_location: Optional[str]
    communication_stats: Optional[CommunicationStats]
    sources: List[PersonSource]  # GAL, Contacts, Email History
    is_vip: bool

    @property
    def primary_email(self) -> Optional[str]:
        """Get primary email address."""

    @property
    def relationship_strength(self) -> float:
        """Calculate 0-1 relationship score."""

    @property
    def source_priority(self) -> int:
        """Get source priority for ranking (GAL > Contacts > Email)."""

    def merge_with(self, other: "Person") -> "Person":
        """Merge data from another Person object."""

    @classmethod
    def from_gal_result(cls, mailbox, contact_info) -> "Person":
        """Create Person from GAL search result."""

    @classmethod
    def from_contact(cls, contact) -> "Person":
        """Create Person from Exchange contact."""

    @classmethod
    def from_email_contact(cls, email, name, stats) -> "Person":
        """Create Person from email history."""
```

#### EmailMessage Model (`email_message.py`)
```python
class EmailMessage(BaseModel):
    message_id: str
    conversation_id: Optional[str]
    in_reply_to: Optional[str]
    references: List[str]
    subject: str
    sender: str
    recipients: List[str]
    body_text: Optional[str]
    body_html: Optional[str]
    # ... more fields
```

#### ConversationThread Model (`thread.py`)
```python
class ConversationThread(BaseModel):
    conversation_id: str
    subject: str
    messages: List[ThreadMessage]
    participants: List[str]
    # ... more fields
```

### 2. Service Layer (`src/services/`)

**Design Pattern:** Service-Oriented Architecture

Business logic layer that orchestrates operations.

#### PersonService (`person_service.py`)
The **orchestrator** for person discovery.

```python
class PersonService:
    """Person-centric service for discovering and managing people."""

    def __init__(self, ews_client):
        self.ews_client = ews_client
        self.gal_adapter = GALAdapter(ews_client)
        self.cache = get_cache()

    async def find_person(
        self,
        query: str,
        sources: List[str] = ["gal", "contacts", "email_history"],
        include_stats: bool = True,
        time_range_days: int = 365,
        max_results: int = 50
    ) -> List[Person]:
        """
        Find people using intelligent multi-source search.

        Search strategy:
        1. Try GAL (multi-strategy search)
        2. Try Personal Contacts (if enabled)
        3. Try Email History (if enabled)
        4. Merge and deduplicate results
        5. Rank by relevance
        """

    async def get_person(self, email: str) -> Optional[Person]:
        """Get complete information about a specific person."""

    async def get_communication_history(
        self, email: str, days_back: int
    ) -> Optional[CommunicationStats]:
        """Get detailed communication history with a person."""

    def _rank_persons(self, persons: List[Person], query: str) -> List[Person]:
        """
        Rank persons by relevance.

        Ranking criteria:
        1. Source priority (GAL > Contacts > Email History)
        2. Name/email match quality
        3. Communication volume
        4. Recency of contact
        5. VIP status
        6. Profile completeness
        """
```

#### EmailService (`email_service.py`)
```python
class EmailService:
    """Email operations with thread support."""

    async def send_email(
        self, to, subject, body,
        in_reply_to=None, thread_id=None, ...
    ) -> Dict[str, Any]:
        """Send email with optional thread context."""
```

#### ThreadService (`thread_service.py`)
```python
class ThreadService:
    """Conversation thread operations."""

    async def get_thread(self, conversation_id: str) -> ConversationThread:
        """Get complete conversation thread."""

    def format_reply_html(
        self, new_content: str, original_message: Dict
    ) -> str:
        """Format HTML reply with quoted original."""
```

#### AttachmentService (`attachment_service.py`)
```python
class AttachmentService:
    """Comprehensive attachment handling."""

    # Supported formats
    SUPPORTED_FORMATS = {
        'pdf': 'application/pdf',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'txt': 'text/plain',
        'csv': 'text/csv',
        'html': 'text/html',
        'zip': 'application/zip',
    }

    async def extract_content(self, attachment_data: bytes, filename: str) -> Dict:
        """Extract content from attachment."""
```

### 3. Adapter Layer (`src/adapters/`)

**Design Pattern:** Adapter Pattern

External system integrations isolated from business logic.

#### GALAdapter (`gal_adapter.py`)
**THE FIX** for the 0-results bug!

```python
class GALAdapter:
    """
    Multi-strategy Global Address List search.

    Eliminates 0-results scenarios with intelligent fallbacks.
    """

    async def search(
        self, query: str, max_results: int = 50
    ) -> List[Person]:
        """
        Multi-strategy search - never returns 0 when people exist.

        Strategy 1: Exact match (resolve_names)
        Strategy 2: Partial match (wildcard/prefix)
        Strategy 3: Domain search (@domain.com)
        Strategy 4: Fuzzy matching
        """

        # Strategy 1: Exact match
        results = await self._search_exact(query)
        if results:
            return results

        # Strategy 2: Partial match
        results = await self._search_partial(query)
        if results:
            return results

        # Strategy 3: Domain search
        if '@' in query:
            results = await self._search_domain(query)
            if results:
                return results

        # Strategy 4: Fuzzy match
        results = await self._search_fuzzy(query)
        return results

    async def _search_exact(self, query: str) -> List[Person]:
        """Exact match using resolve_names."""

    async def _search_partial(self, query: str) -> List[Person]:
        """Partial match with wildcards."""

    async def _search_domain(self, query: str) -> List[Person]:
        """Domain-based search."""

    async def _search_fuzzy(self, query: str) -> List[Person]:
        """Fuzzy matching for typos."""
```

#### CacheAdapter (`cache_adapter.py`)
```python
class CacheAdapter:
    """Simple in-memory cache with TTL support."""

    CACHE_DURATIONS = {
        'gal_search': 3600,      # 1 hour - GAL doesn't change often
        'person_details': 1800,   # 30 min
        'folder_list': 300,       # 5 min
        'email_search': 60,       # 1 min
        'contacts': 1800,         # 30 min
    }

    async def get_or_fetch(
        self, key: str, fetch_func: Callable, duration: int
    ) -> Any:
        """Get from cache or fetch and cache."""
```

### 4. Tool Registry (`src/tools/`)

**Design Pattern:** Factory + Strategy

Each tool is a thin wrapper around services.

```python
class FindPersonTool(BaseTool):
    """Search for contacts using PersonService v3.0."""

    async def execute(self, **kwargs) -> Dict[str, Any]:
        # Delegate to PersonService
        person_service = PersonService(self.ews_client)

        persons = await person_service.find_person(
            query=kwargs['query'],
            sources=kwargs.get('sources', ['gal', 'contacts', 'email_history']),
            include_stats=kwargs.get('include_stats', True),
            ...
        )

        # Convert to MCP response format
        return {
            'success': True,
            'results': [person.dict() for person in persons],
            ...
        }
```

### 5. Middleware Layer

#### Rate Limiter
- **Algorithm:** Token Bucket
- **Window:** 60 seconds (sliding)
- **Default Limit:** 25 requests/minute
- **Implementation:** In-memory deque

#### Error Handler
- **Pattern:** Centralized exception handling
- **Features:**
  - Maps exceptions to error responses
  - Determines retry-ability
  - Logs with appropriate severity

#### Enterprise Logging
**NEW in v3.0:** Multi-tier logging system.

```python
def setup_logging(log_level: str = "INFO") -> None:
    """
    Enterprise-level logging:
    - Console (stderr): INFO level for monitoring
    - File (ews-mcp.log): DEBUG level for troubleshooting
    - Errors (ews-mcp-errors.log): ERROR level only
    - Audit (audit.log): Compliance trail
    """
```

**Log files:**
- `logs/ews-mcp.log` - All DEBUG+ logs (10MB, 5 backups)
- `logs/ews-mcp-errors.log` - ERROR+ only (10MB, 3 backups)
- `logs/audit.log` - Audit trail (20MB, 10 backups)

### 6. EWS Client Wrapper (`src/ews_client.py`)

**Design Pattern:** Facade + Singleton (per account)

**Features:**
- Lazy connection initialization
- Automatic retry with exponential backoff
- Support for autodiscovery
- Connection pooling
- Proper cleanup on shutdown

**Retry Strategy:**
```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
```

### 7. Authentication Handler (`src/auth.py`)

**Design Pattern:** Strategy

**Supported Methods:**
1. **OAuth2 (Recommended)**
   - Uses MSAL library
   - Acquires token for client credentials flow
   - Automatic token refresh

2. **Basic Auth**
   - Username/password
   - Suitable for on-premises

3. **NTLM**
   - Windows integrated auth
   - Domain credentials

## Data Flow

### Example: Finding a Person (v3.0)

```
1. AI Assistant → MCP Request
   {
     "tool": "find_person",
     "arguments": {
       "query": "Ahmed",
       "search_scope": "all"
     }
   }

2. MCP Server → call_tool()
   - Validates tool exists
   - Checks rate limit

3. FindPersonTool → PersonService.find_person()

4. PersonService → Multi-source search

   4a. Check cache for "gal_search:Ahmed"

   4b. GALAdapter.search("Ahmed")
       - Strategy 1: Exact match → 0 results
       - Strategy 2: Partial match → 3 results
       - Return Person objects

   4c. Search Contacts folder
       - Filter by query
       - Return Person objects

   4d. Search Email History
       - Scan inbox/sent for query
       - Calculate communication stats
       - Return Person objects

5. PersonService → Merge & Deduplicate
   - Combine by email address
   - Merge data from multiple sources
   - Preserve highest quality data

6. PersonService → Rank by Relevance
   - Source priority (GAL > Contacts > Email)
   - Match quality
   - Communication volume
   - Recency
   - VIP status

7. Response Flow (reverse)
   PersonService → FindPersonTool → MCP Server → AI Assistant

8. Audit Log → Record operation
   "find_person | query=Ahmed | results=5 | success"
```

## Security Considerations

### 1. Credentials Management
- Never log secrets
- Use environment variables only
- Support Azure Key Vault (future)

### 2. Input Validation
- All inputs validated with Pydantic
- Type checking enforced
- Email address format validation

### 3. Rate Limiting
- Prevents API abuse
- Protects Exchange server
- Per-user limits

### 4. Error Handling
- Never expose internal errors to users
- Sanitize error messages
- Log detailed errors server-side

### 5. Docker Security
- Non-root user (uid 1000)
- Minimal base image (Debian slim)
- No unnecessary packages
- Read-only file system (where possible)

## Performance Optimizations

### 1. Connection Reuse
- Single Account instance per server
- Connection pooling in exchangelib

### 2. Lazy Loading
- Account created only when first accessed
- Credentials acquired on-demand

### 3. Intelligent Caching (NEW in v3.0)
- GAL search: 1 hour TTL
- Person details: 30 min TTL
- Contact lookups: 30 min TTL
- Folder metadata: 5 min TTL
- Email search: 1 min TTL

### 4. Async Operations
- MCP protocol is async
- Tools can run concurrently
- Services use async/await

### 5. Result Limits
- Maximum 2000 items scanned
- Pagination for large datasets
- Prevent timeouts

## Error Handling Strategy

### Exception Hierarchy
```
Exception
└── EWSMCPException (Base)
    ├── AuthenticationError
    ├── ConnectionError
    ├── RateLimitError
    ├── ValidationError
    └── ToolExecutionError
```

### Error Response Format
```json
{
  "success": false,
  "error": "Human-readable message",
  "error_type": "ValidationError",
  "is_retryable": false
}
```

## Testing Strategy

### Unit Tests
- Mock EWS client
- Test each service in isolation
- Test adapters independently
- Validate input/output schemas

### Integration Tests
- Test against real Exchange server
- End-to-end workflows
- Marked with `@pytest.mark.integration`

### Test Coverage Goals
- Minimum 80% code coverage
- 100% coverage for critical paths (auth, client, services)

## Deployment Options

### 1. Docker Container (Recommended)
- **Pros:** Isolated, reproducible, easy deployment
- **Cons:** Slight overhead

### 2. Kubernetes
- **Pros:** Scalable, highly available
- **Cons:** More complex

### 3. Local Python
- **Pros:** Simple for development
- **Cons:** Environment dependencies

### 4. Cloud Services
- AWS ECS/Fargate
- Azure Container Instances
- Google Cloud Run

## Monitoring and Observability

### Logging Levels
- **DEBUG:** Detailed execution flow (file only)
- **INFO:** Normal operations (console + file)
- **WARNING:** Recoverable errors
- **ERROR:** Tool failures (dedicated error log)
- **CRITICAL:** Server failures

### Metrics (Future)
- Request rate
- Error rate
- Response time
- Tool usage distribution
- Cache hit rate

### Health Checks
- Connection to Exchange
- Authentication validity
- Tool availability

## Design Decisions

### Why Person-Centric Architecture?
- More intuitive for AI assistants
- Better user experience ("find Ahmed" vs "search email address")
- Enables relationship insights
- Foundation for future AI features

### Why Multi-Strategy GAL Search?
- Fixes the 0-results bug completely
- Handles partial names, typos, variations
- Domain search for organization-wide discovery
- Better than failing silently

### Why Service Layer?
- Separation of concerns
- Reusable business logic
- Testable in isolation
- Easy to extend

### Why Adapters?
- Isolate external dependencies
- Easy to mock for testing
- Can swap implementations
- Clear interface contracts

### Why Pydantic Models?
- Type safety
- Automatic validation
- Great error messages
- JSON schema generation
- Serialization/deserialization

### Why In-Memory Caching?
- Simple to implement
- No additional dependencies
- Fast access
- Sufficient for single-instance deployment

## Future Enhancements

### Planned for v3.1+
1. **AI-powered relationship insights** - Using communication patterns
2. **Smart scheduling suggestions** - Based on availability and history
3. **Sentiment analysis** - Analyze tone of communications
4. **Auto-categorization** - Tag people by role, project, importance
5. **Response prediction** - Predict who will respond and when
6. **Network graph** - Visualize professional relationships

### Scalability Considerations
- Horizontal scaling (multiple instances)
- Distributed caching (Redis)
- Load balancing
- Session affinity
- Distributed rate limiting
