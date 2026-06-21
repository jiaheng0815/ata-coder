# ATA Coder Web UI

Browser-based frontend for ATA Coder's HTTP API server (`server.py`).

## Status

🚧 **In development** — functional but minimal. Ships with the Python package and is served on `http://localhost:<port>` when running in server mode.

## Structure

```
web/
├── index.html      # Main single-page app
├── css/            # Stylesheets
├── js/             # Compiled JavaScript (gitignored, built from ts/)
├── ts/             # TypeScript source
├── tsconfig.json   # TypeScript config
└── package.json    # Node.js project metadata
```

## Build

```bash
cd web
npm install        # install TypeScript compiler
npm run build      # compile ts/ → js/
```

## Architecture

The web UI communicates with the Python backend via:
- **SSE** (`/v1/chat/stream`) — real-time streaming of agent events
- **HTTP POST** (`/v1/chat`) — non-streaming chat requests
- **HTTP GET** (`/health`, `/sessions`) — status and session queries

The TypeScript companion server (`ts-server/`) is the **primary** HTTP/SSE server (Node.js 24 native TS). The Python `server.py` is a fallback for users who don't want Node.js.
