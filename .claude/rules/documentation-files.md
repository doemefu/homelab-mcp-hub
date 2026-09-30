# Documentation Files — Purpose & Owner

| File | Audience | Covers |
|------|----------|--------|
| `README.md` | Everyone | Purpose, status, quick reference, build/run |
| `CONTRIBUTING.md` | Contributors | Prerequisites, local environment, tests, process, review checklist |
| `INTERFACES.md` | Integrators | MCP endpoint, authorization rules, tools, errors, consumed services, config, Secret keys, logging |
| `DEPLOYMENT.md` | Operators | K8s manifests, Flux bootstrap, owner actions, verification, troubleshooting |
| `CHANGELOG.md` | Users of the hub | Changes per release |
| `CLAUDE.md` | Claude Code sessions | Repo conventions, non-negotiables, agent team |
| `LICENSE` | Everyone | MIT licence |

The contract is `docs/080-mcp-hub.md` (plus ADR `docs/adr/0003-mcp-hub-authorization.md`) in the `doemefu/homelab` repo; link, never copy.

**Rule:** Docs are written in parallel with the code change, not after. This repository is public: docs describe behaviour and tasks, never account identifiers, usernames or addresses.
