<div align="center">

# TermFlow

Resilient remote tmux terminal & 4-level guarded Agent Broker across Web, Mobile, and Native clients.

[![Family: *Flow](https://img.shields.io/badge/family-*Flow-8A2BE2.svg)](https://github.com/mcocdaa)
[![CI](https://github.com/mcocdaa/TermFlow/actions/workflows/ci.yml/badge.svg)](https://github.com/mcocdaa/TermFlow/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/mcocdaa/TermFlow?display_name=tag&sort=semver)](https://github.com/mcocdaa/TermFlow/releases)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![TypeScript 5.x](https://img.shields.io/badge/TypeScript-5.x-blue.svg)](https://www.typescriptlang.org/)
[![Docker Ready](https://img.shields.io/badge/docker-ready-blue.svg)](deploy/compose.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[English](README.md) · [简体中文](README_CN.md)

<img src="docs/assets/demo_termflow.gif" alt="TermFlow Demo" width="840">

</div>

## ✨ Highlights

- **Outbound-Only Node A (`Computer → Term → Window → Pane`)**: Computer A dials out over TLS/WSS to Control Plane B with zero inbound ports (`A 主动连接 B`, `A 不开放端口`); closing browsers or losing mobile connectivity never interrupts running `tmux` jobs.
- **Mobile-First Web & Native Terminal**: Responsive `xterm.js` workspace equipped with a tactile `MobileKeyBar` (`Esc`, `Tab`, `Ctrl`, `Alt`, arrow keys, and quick tmux chords) plus 7 curated geek themes.
- **4-Level Guarded Agent Broker (`OpenCode`)**: Replayable agent event streams with a 4-tier risk guard and mandatory human-in-the-loop approval before any write or critical terminal action executes.
- **Zero-Trust TOTP & One-Time Pairing**: Control Plane authentication protected by TOTP MFA, short-lived one-time node enrollment tokens, and device authorization via QR code or pairing code.
- **Multi-Platform Native Clients**: Unified Vue 3 + TypeScript + Tauri 2 client architecture shipping across Web, Windows, Linux, macOS, Android, and iOS Simulator.
- **Strict Control Plane Boundary**: Control Plane B authenticates, routes, and relays terminal streams without executing host shell commands, keeping Node A as the sole terminal authority.

## 🚀 Quick Start

### 1. Deploy Control Plane B + Web C + Agent Broker (Docker Compose)

**Option A — Published Release Images (Recommended)**

```bash
TAG=v0.2.0
BASE="https://raw.githubusercontent.com/mcocdaa/TermFlow/${TAG}"
mkdir -p termflow && cd termflow
curl -fsSLO "${BASE}/deploy/compose.release.yaml"
curl -fsSLO "${BASE}/deploy/opencode-config.yaml"
curl -fsSLo .env "${BASE}/.env.example"
chmod 0600 .env

# Configure TERMFLOW_ADMIN_TOKEN, OPENCODE_SERVER_*, OPENCODE_AGENT_MCP_TOKEN,
# DEEPSEEK_API_KEY, and set TERMFLOW_RELEASE_IMAGE_TAG=${TAG} in .env
docker compose --env-file .env -f compose.release.yaml up -d
curl -fsS http://127.0.0.1:8765/healthz
```

**Option B — Local Source Checkout**

```bash
cp .env.example .env
docker compose --env-file .env -f deploy/compose.yaml up -d --build
curl -fsS http://127.0.0.1:8765/healthz
```

### 2. Enroll Linux Node A (`tmux 3.2+`)

```bash
curl -fsSL \
  https://github.com/mcocdaa/TermFlow/releases/download/v0.2.0/install-termflow-node.sh \
  | bash

termflow login \
  --server https://termflow.example.com \
  --code '<one-time-enrollment-code-from-web-c>'

termflow new --name project-a
```

Detach anytime with `Ctrl+B` then `D` (`termflow attach project-a` to re-enter).

<details>
<summary>📦 Standalone Control Plane, Offline Tarball & Packaging Reference</summary>

### Standalone Control Plane Container (Terminal Only, Without Agent Runtime)

When you only need remote terminal routing without the OpenCode Agent runtime (`B 的 Web/API 通过宿主机 loopback 端口发布；A 主动连接 B，是连接 B 的客户端，不是 B 的网关；A 不开放端口`):

```bash
mkdir -p data totp-secrets
docker network create termflow-net

docker run -d --name termflow-control-plane \
  --restart unless-stopped \
  --network termflow-net \
  --publish 127.0.0.1:8765:8000 \
  --volume "$PWD/data:/app/data" \
  --volume "$PWD/totp-secrets:/app/totp-secrets" \
  --env TERMFLOW_ADMIN_TOKEN='<at-least-32-byte-admin-token>' \
  --env TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////app/data/termflow.db \
  --env TERMFLOW_PUBLIC_BASE_URL=https://termflow.example.com \
  --env TERMFLOW_ALLOW_INSECURE_LOOPBACK="${TERMFLOW_ALLOW_INSECURE_LOOPBACK:-true}" \
  --env TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE=/app/totp-secrets/totp-master-key \
  ghcr.io/mcocdaa/termflow-control-plane:v0.2.0

curl -fsS http://127.0.0.1:8765/healthz
```

For offline archive loading and TOTP recovery:

```bash
docker load -i termflow-control-plane.tar
docker exec termflow-control-plane termflow-control auth totp reset
```

### Containerized Node A (Docker)

Generate a one-time enrollment code via Web C or API (`POST /api/v1/enrollment-tokens`), then launch `termflow-node` on the shared bridge network:

```bash
mkdir -p termflow-node-identity termflow-node-work

docker run -d \
  --name termflow-node \
  --restart unless-stopped \
  --network termflow_default \
  --cap-drop ALL \
  --cap-add CHOWN \
  --cap-add DAC_OVERRIDE \
  --cap-add SETUID \
  --cap-add SETGID \
  --security-opt no-new-privileges:true \
  --read-only \
  --tmpfs /tmp \
  --volume "$PWD/termflow-node-identity:/home/termflow" \
  --volume "$PWD/termflow-node-work:/work" \
  --env TERMFLOW_SERVER=http://control-plane:8000 \
  --env TERMFLOW_CODE='<one-time-enrollment-code>' \
  --env TERMFLOW_ALLOW_INSECURE_HTTP=true \
  --env TERMFLOW_NEW=demo \
  ghcr.io/mcocdaa/termflow-node:v0.2.0
```

Inspect or attach to the containerized node as UID 1000 (`termflow`):

```bash
docker exec --user termflow -it termflow-node termflow attach demo
docker exec --user termflow termflow-node termflow doctor
docker exec --user termflow termflow-node termflow status demo --json
```

### Build Version & Packaging Contracts

- **Version precedence**: `Git Tag > TERMFLOW_BUILD_VERSION > 0.2.0-dev.0`
- **Packaging workflows** (`workflow_dispatch`, `workflow_call`, `Actions artifact`, `GitHub Release`, `GHCR`):
  - `Package A · Linux Node` (`termflow-node-linux-x86_64`, requires `tmux 3.2`+, offline wheelhouse via `Path.cwd().as_uri()`)
  - `Package B + Web C · Control Plane` (pinned `Node 22.23.2` + Python 3.12 runtime)
  - `Package C · Native Clients` (Windows, Linux, macOS, Android, iOS Simulator from [GitHub Releases](https://github.com/mcocdaa/TermFlow/releases))

</details>

## 📚 Documentation

| Document | Description |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | System topology, process boundaries, and network security model between Node A, Control Plane B, Web C, and OpenCode |
| [docs/protocol.md](docs/protocol.md) | Versioned WebSocket & Pydantic message contracts between Node A and Control Plane B |
| [docs/security.md](docs/security.md) | TOTP MFA, one-time pairing, 4-level Agent Broker risk guard, and container hardening |
| [docs/api-examples.md](docs/api-examples.md) | REST & WebSocket API usage examples for enrollment, sessions, panes, and agent approvals |
| [docs/web-client.md](docs/web-client.md) | Web C & mobile terminal guide, QR pairing flow, themes, and `MobileKeyBar` |
| [docs/operations.md](docs/operations.md) | Production deployment, upgrades, volume backups, TOTP reset, and recovery procedures |
| [docs/github-actions.md](docs/github-actions.md) | CI/CD pipelines, release artifact matrix, and GHCR image publishing |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common diagnostics for `tmux` sockets, WebSocket handshakes, and container permissions |
| [docs/superpowers/README.md](docs/superpowers/README.md) | Architecture specs, design decisions, and implementation records |

## 📄 License

Released under the [MIT License](LICENSE).
