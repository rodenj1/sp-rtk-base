# Local NTRIP Caster for Development & Testing

## Overview

A lightweight Python-based NTRIP caster running in Docker, designed for local development and testing of NTRIP v1.0 and v2.0 connections. This caster handles the full NTRIP protocol for both server (data push) and client (data pull) connections.

## Quick Start

```bash
# Start the caster
docker compose -f docker/ntrip-caster/docker-compose.yml up -d

# Check it's running
curl http://localhost:2101/

# Run validation tests
uv run python tools/test_ntrip_caster.py

# View logs
docker compose -f docker/ntrip-caster/docker-compose.yml logs -f

# Stop
docker compose -f docker/ntrip-caster/docker-compose.yml down
```

## Architecture

The caster is a pure Python asyncio application (`docker/ntrip-caster/ntrip_caster.py`) with zero external dependencies. It runs in a minimal `python:3.12-alpine` Docker container.

### Protocol Support

| Feature | NTRIP v1.0 | NTRIP v2.0 |
|---------|-----------|-----------|
| Server auth | `SOURCE <password> /<mount>` | `POST /<mount>` + Basic auth |
| Server response | `ICY 200 OK` | `HTTP/1.1 200 OK` |
| Client request | `GET /<mount> HTTP/1.0` | `GET /<mount> HTTP/1.1` |
| Client response | `ICY 200 OK` + raw stream | `HTTP/1.1 200 OK` + stream |
| Sourcetable | `SOURCETABLE 200 OK` | `HTTP/1.1 200 OK` |
| Data encoding | Raw bytes | Raw bytes or chunked |
| Auth rejection | `ERROR - Bad Password` | `HTTP/1.1 401 Unauthorized` |

### Mountpoint Management

- Mountpoints are created dynamically when an NTRIP server (SOURCE/POST) connects
- Mountpoints are removed when the server disconnects
- Multiple clients can connect to a single mountpoint
- Data is broadcast from the server to all connected clients in real-time

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CASTER_PORT` | `2101` | TCP port for NTRIP connections |
| `SERVER_PASSWORD` | `testpass` | Password for NTRIP servers pushing data |
| `LOG_LEVEL` | `info` | Logging level: `debug`, `info`, `warn` |

### Docker Compose

The `docker-compose.yml` maps port 2101 and configures the environment:

```yaml
services:
  ntrip-caster:
    build: .
    container_name: sp-base-ntrip-caster
    ports:
      - "2101:2101"
    environment:
      - CASTER_PORT=2101
      - SERVER_PASSWORD=testpass
      - LOG_LEVEL=info
    restart: unless-stopped
```

## Testing

### Validation Script

The `tools/test_ntrip_caster.py` script validates all NTRIP protocol operations:

```bash
uv run python tools/test_ntrip_caster.py [--host HOST] [--port PORT] [--password PASS]
```

Tests performed:
1. **TCP Connectivity** — Can connect to the caster
2. **Sourcetable v1.0** — `GET / HTTP/1.0` returns `SOURCETABLE 200 OK`
3. **Sourcetable v2.0** — `GET / HTTP/1.1` returns `HTTP/1.1 200 OK`
4. **v1.0 SOURCE Auth** — Password accepted, `ICY 200 OK` returned
5. **v1.0 Auth Rejection** — Wrong password returns error
6. **v2.0 POST Auth** — Basic auth accepted, `HTTP/1.1 200 OK` returned
7. **v2.0 Auth Rejection** — Wrong credentials return `401 Unauthorized`
8. **v1.0 Data Push** — SOURCE + raw RTCM data accepted
9. **v2.0 Data Push** — POST + chunked RTCM data accepted

### Manual Testing with curl

```bash
# Get sourcetable (v1.0 style)
curl http://localhost:2101/

# Get sourcetable (v2.0 style)
curl -H "Ntrip-Version: Ntrip/2.0" http://localhost:2101/
```

### Testing with sp-base Relay

Configure an NTRIP destination pointing to the local caster:

```yaml
destinations:
  - type: ntrip
    host: localhost
    port: 2101
    mountpoint: MYBASE
    password: testpass
    ntrip_version: "1.0"  # or "2.0"
```

## Files

| File | Description |
|------|-------------|
| `docker/ntrip-caster/ntrip_caster.py` | Python NTRIP caster (pure asyncio, no deps) |
| `docker/ntrip-caster/Dockerfile` | Docker build (python:3.12-alpine) |
| `docker/ntrip-caster/docker-compose.yml` | Docker Compose configuration |
| `tools/test_ntrip_caster.py` | Comprehensive validation test script |

## Troubleshooting

### Container won't start
```bash
docker compose -f docker/ntrip-caster/docker-compose.yml logs
```

### Port already in use
```bash
# Check what's using port 2101
lsof -i :2101
# Or change the port in docker-compose.yml
```

### Connection refused
Ensure the container is running and healthy:
```bash
docker compose -f docker/ntrip-caster/docker-compose.yml ps
```

### Enable debug logging
Set `LOG_LEVEL=debug` in `docker-compose.yml` to see all request parsing details.
