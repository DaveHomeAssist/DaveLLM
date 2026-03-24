# DaveLLM Frontend-Backend Integration Guide

## Overview
The DaveLLM system consists of a FastAPI backend router and a browser-based frontend for managing persistent conversations with multiple llama.cpp inference nodes.

## Architecture

### Backend (`app.py`) - Pydantic V2
- **FastAPI** server on `http://127.0.0.1:8000`
- **Round-robin** node selection via `NODE_CYCLE`
- **In-memory** conversation storage (ephemeral)
- **OpenAI-compatible** chat format

### Frontend (`app.js` + `index.html`)
- **Browser-based** UI with persistent localStorage
- **Session management** via `session_id`
- **Conversation history** stored locally
- **Real-time node discovery** and status display

## API Contracts

### GET `/nodes`
```json
[
  {
    "id": "mac-node",
    "name": "Mac Test Node",
    "url": "http://127.0.0.1:9001"
  }
]
```

### POST `/chat`
**Request:**
```json
{
  "session_id": "default",
  "messages": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there"}
  ],
  "max_tokens": 256
}
```

**Response:**
```json
{
  "response": "Assistant's answer",
  "node": "Mac Test Node",
  "conversation_id": "default"
}
```

### GET `/health`
```json
{
  "status": "ok",
  "nodes": [
    {
      "id": "mac-node",
      "name": "Mac Test Node",
      "url": "http://127.0.0.1:9001"
    }
  ]
}
```

## Frontend Data Flow

1. **User Input** → User types prompt in textarea
2. **Local History** → Message saved to `state.history[]`
3. **API Request** → POST to `/chat` with full conversation
4. **Backend Processing** → Router selects node, sends to llama.cpp
5. **Response Parsing** → Extract `data.response`
6. **UI Update** → Display response in textarea
7. **Persistence** → Save to localStorage

## State Management

### Global State (`state` object)
```javascript
{
  nodes: [],                  // Available inference nodes
  selectedNode: null,         // Currently selected node ID
  isStreaming: false,         // Request in flight
  sessionId: "default",       // Conversation session
  history: [],                // Full message history [{role, content}, ...]
  autoRefreshId: null         // Auto-refresh interval ID
}
```

### LocalStorage Keys
- `routerUrl`: Backend API endpoint
- `history_<sessionId>`: Conversation messages for session
- `activeConversation`: Last used session ID

## Required HTML Elements

| ID | Type | Purpose |
|---|---|---|
| `router-url` | input | Configure backend URL |
| `refresh-nodes` | button | Reload node list |
| `node-list` | div | Node display container |
| `node-count` | span | Active node count |
| `node-select` | select | Node dropdown selector |
| `prompt` | textarea | User input field |
| `send` | button | Submit message |
| `clear-output` | button | Clear response display |
| `response` | textarea | Conversation display |
| `reset-conversation` | button | Clear history |
| `toggle-settings` | button | Show/hide settings |
| `settings-panel` | div | Settings container |
| `console-log` | div | Logging output |

## Error Handling

### Frontend Error Scenarios
- **No prompt**: Validation check blocks empty sends
- **Router unreachable**: Fetch error caught, logged
- **JSON parse error**: Try-catch wraps response parsing
- **HTTP error**: Non-200 status throws, caught

### Backend Error Scenarios
- **No nodes**: HTTP 503 from `pick_node()`
- **Node unreachable**: HTTP 502 with error message
- **Malformed response**: HTTP 502 with format error

## Integration Checklist

- ✅ Backend accepts `{session_id, messages[], max_tokens}`
- ✅ Frontend sends full conversation history per request
- ✅ Response format: `{response, node, conversation_id}`
- ✅ Session persistence: localStorage with key `history_<sessionId>`
- ✅ Node discovery: Auto-refresh every 3 seconds
- ✅ Error logging: Console log display in UI
- ✅ CORS enabled on backend for localhost
- ✅ Pydantic V2 migrations complete

## Deployment

### Development
```bash
# Terminal 1: Backend
cd /Users/daverobertson/Desktop/Dave-LLM
uvicorn app:app --reload

# Terminal 2: Static Server
python3 -m http.server 5500

# Browser
Open http://localhost:5500
Enter router URL: http://127.0.0.1:8000
```

### Production
- Use HTTPS
- Configure CORS origins whitelist
- Replace in-memory storage with database
- Add authentication
- Use environment variables for configuration

## Known Limitations

1. **Ephemeral Memory**: Backend conversations lost on restart
2. **No Real Persistence**: Use database for production
3. **Single Session**: Frontend manages one session at a time
4. **No Streaming**: Response waits for full completion
5. **Manual Node Config**: Add nodes by editing `DEFAULT_NODES`

## Future Improvements

- [ ] Multiple concurrent sessions
- [ ] Streaming responses
- [ ] User authentication
- [ ] Database persistence
- [ ] Model selection per node
- [ ] Request history export
- [ ] Conversation sharing
- [ ] Custom system prompts
