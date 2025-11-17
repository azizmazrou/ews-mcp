# Architecture Overview

Technical architecture and design decisions for EWS MCP Server.

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     AI Assistant (Claude)                    │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP Protocol (stdio/SSE)
┌──────────────────────────▼──────────────────────────────────┐
│                     EWS MCP Server                           │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              MCP Protocol Handler                       │ │
│  │  - list_tools()                                        │ │
│  │  - call_tool(name, arguments)                          │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               │                                              │
│  ┌────────────▼───────────────────────────────────────────┐ │
│  │              Tool Registry & Router                     │ │
│  │  - Email Tools (6)                                     │ │
│  │  - Calendar Tools (5)                                  │ │
│  │  - Contact Tools (5)                                   │ │
│  │  - Task Tools (5)                                      │ │
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

## Component Design

### 1. MCP Protocol Handler (`src/main.py`)

**Responsibilities:**
- Implement MCP protocol (stdio transport)
- Register and expose tools to AI assistant
- Route tool calls to appropriate handlers
- Convert responses to MCP format

**Key Methods:**
- `list_tools()`: Returns available tools with schemas
- `call_tool(name, arguments)`: Executes tool and returns result

### 2. Tool Registry (`src/tools/`)

**Design Pattern:** Factory + Strategy

**Structure:**
```
BaseTool (Abstract Base Class)
├── EmailTool(s)
│   ├── SendEmailTool
│   ├── ReadEmailsTool
│   └── ...
├── CalendarTool(s)
├── ContactTool(s)
└── TaskTool(s)
```

**Each Tool Implements:**
- `get_schema()`: Returns JSON schema for MCP
- `execute(**kwargs)`: Performs the operation
- `safe_execute(**kwargs)`: Wrapped execution with error handling

### 3. Middleware Layer

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

#### Audit Logger
- **Purpose:** Compliance and debugging
- **Logs:** Operation, user, success/failure, timestamp
- **Format:** Structured logging

### 4. EWS Client Wrapper (`src/ews_client.py`)

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

### 5. Authentication Handler (`src/auth.py`)

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

### 6. Configuration Management (`src/config.py`)

**Pattern:** Settings Pattern with Pydantic

**Features:**
- Environment variable loading
- Type validation
- Default values
- Validation rules (e.g., OAuth2 requires client_id)

## Data Flow

### Example: Sending an Email

```
1. AI Assistant → MCP Request
   {
     "tool": "send_email",
     "arguments": {
       "to": ["user@example.com"],
       "subject": "Hello",
       "body": "World"
     }
   }

2. MCP Server → call_tool()
   - Validates tool exists
   - Checks rate limit

3. Rate Limiter → Check quota
   - Returns OK or RateLimitError

4. Tool Registry → SendEmailTool.execute()

5. Pydantic Validator → Validate input
   SendEmailRequest(**arguments)

6. EWS Client → Create Message
   Message(account, to_recipients, subject, body)

7. Authentication → Get credentials
   OAuth2Credentials or Credentials

8. Exchange Server → Send email
   HTTPS POST to /EWS/Exchange.asmx

9. Response Flow (reverse)
   Exchange → EWS Client → Tool → MCP Server → AI Assistant

10. Audit Log → Record operation
    "send_email | user@example.com | success"
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
- Minimal base image (Alpine)
- No unnecessary packages
- Read-only file system (where possible)

## Performance Optimizations

### 1. Connection Reuse
- Single Account instance per server
- Connection pooling in exchangelib

### 2. Lazy Loading
- Account created only when first accessed
- Credentials acquired on-demand

### 3. Caching (Future)
- Contact lookups
- Folder metadata
- Calendar free/busy

### 4. Async Operations
- MCP protocol is async
- Tools can run concurrently

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
- Test each tool in isolation
- Validate input/output schemas

### Integration Tests
- Test against real Exchange server
- End-to-end workflows
- Marked with `@pytest.mark.integration`

### Test Coverage Goals
- Minimum 80% code coverage
- 100% coverage for critical paths (auth, client)

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
- **AWS ECS/Fargate**
- **Azure Container Instances**
- **Google Cloud Run**

## Monitoring and Observability

### Logging Levels
- **DEBUG:** Detailed execution flow
- **INFO:** Normal operations
- **WARNING:** Recoverable errors
- **ERROR:** Tool failures
- **CRITICAL:** Server failures

### Metrics (Future)
- Request rate
- Error rate
- Response time
- Tool usage distribution

### Health Checks
- Connection to Exchange
- Authentication validity
- Tool availability

## Future Enhancements

### Planned Features
1. **Caching Layer**
   - Redis integration
   - TTL-based expiration

2. **Webhook Support**
   - Push notifications
   - Event subscriptions

3. **Microsoft Graph API**
   - Modern API alternative
   - Better performance

4. **Web UI**
   - Configuration management
   - Monitoring dashboard

5. **Metrics Export**
   - Prometheus format
   - Grafana dashboards

### Scalability Considerations
- Horizontal scaling (multiple instances)
- Load balancing
- Session affinity
- Distributed rate limiting

## Design Decisions

### Why exchangelib?
- Mature, well-maintained library
- Full EWS protocol support
- Good documentation
- Active community

### Why Pydantic?
- Type safety
- Automatic validation
- Great error messages
- JSON schema generation

### Why stdio transport?
- Simple integration with Claude Desktop
- No network configuration needed
- Secure (local only)

### Why Alpine Linux?
- Minimal size (~50MB base)
- Fast build times
- Security updates

### Why Token Bucket for rate limiting?
- Simple to implement
- Fair distribution
- Predictable behavior
