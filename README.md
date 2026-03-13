# Swift MCP Server

Read-only MCP server for OpenStack Swift object storage. Runs in an LXD container and exposes Swift containers and objects to AI agents via the Model Context Protocol.

## Purpose

Allows AI agents (Claude, GitHub Copilot, etc.) to browse and read files stored in OpenStack Swift without write access. The server authenticates with Keystone using standard OpenStack credentials and auto-discovers the S3-compatible endpoint and EC2 credentials.

## Setup

### 1. Prepare credentials

Create a file with your OpenStack credentials:

```
export OS_AUTH_URL=
export OS_USERNAME=
export OS_PASSWORD=
export OS_PROJECT_NAME=
export OS_REGION_NAME=
export OS_USER_DOMAIN_NAME=
export OS_PROJECT_DOMAIN_NAME=
```

### 2. Create the LXD container

```bash
./lxd_setup.sh [CONTAINER_NAME] [CREDENTIALS_FILE]
```

Defaults: container name `swift-mcp-server`, credentials from `~/external_env_files/ps5_swift`.

### 3. Start the server

```bash
lxc exec swift-mcp-server -- /opt/swift-mcp/start.sh
```

The server listens on port `8000` by default. To use a different port:

```bash
lxc exec swift-mcp-server -- bash -c 'MCP_PORT=9000 /opt/swift-mcp/start.sh'
```

## Connecting AI agents

The server uses SSE transport. Find the container IP with `lxc list swift-mcp-server`.

### Claude Code

```bash
claude mcp add --transport sse swift-mcp http://<container-ip>:8000/sse
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "swift-mcp": {
      "type": "sse",
      "url": "http://<container-ip>:8000/sse"
    }
  }
}
```

### GitHub Copilot (VS Code)

Add to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "swift-mcp": {
      "type": "sse",
      "url": "http://<container-ip>:8000/sse"
    }
  }
}
```

## Available tools

| Tool | Description |
|---|---|
| `list_containers` | List all Swift containers |
| `list_objects` | List objects in a container, with optional prefix and delimiter |
| `get_object` | Read file content (text or binary) |
| `head_object` | Get file metadata without downloading content |
