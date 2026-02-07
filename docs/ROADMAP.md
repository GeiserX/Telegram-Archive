# Telegram Archive Roadmap

This document outlines planned features and the long-term vision for Telegram Archive.

For version history and changes, see [CHANGELOG.md](./CHANGELOG.md).

---

## Near-term Improvements

### Security Hardening

- [ ] Rate limiting on `/api/login` endpoint (slowapi or similar)
- [ ] JWT or session tokens with expiration (replace static PBKDF2 token)
- [ ] Search input validation and length limits
- [ ] Telegram session file encryption at rest
- [ ] Docker HEALTHCHECK instructions in Dockerfiles

### Notification System

- [ ] Detect when user has blocked notifications at OS/browser level
- [ ] Auto-disable push subscriptions for blocked users (avoid wasted resources)
- [ ] Notification preferences per chat (mute specific chats)

### Mass Operation Protection

- [ ] True zero-footprint mode: buffer ALL operations before applying
- [ ] Configurable suspicious activity alerts (email/webhook)
- [ ] Undo window for deletions (soft-delete with recovery period)

### Viewer Polish

- [ ] Custom themes (light mode, OLED dark, Telegram classic)
- [ ] Voice message player with waveform visualization
- [ ] Keyboard shortcuts (j/k navigation, Esc to close, etc.)
- [ ] Message deep links (shareable URLs to specific messages)
- [ ] i18n / localization (viewer is English-only currently)

### Developer Experience

- [ ] OpenAPI/Swagger documentation (FastAPI built-in, needs enabling for public use)
- [ ] Grafana/Prometheus metrics endpoint (backup health, message counts, media size)
- [ ] Database vacuum/optimization scheduled task (SQLite VACUUM, PostgreSQL ANALYZE)
- [ ] Backup integrity verification (checksum media files against DB records)

---

## v7.0.0 — Search & Discovery

### Full-text Search

- [ ] Elasticsearch or Meilisearch integration for full-text search across all messages
- [ ] Semantic search (find by meaning, not just keywords)
- [ ] Advanced filters: date range, media type, sender, has:link, has:media

---

## v8.0.0 — Forensic & Legal Admissibility

**Goal:** Make Telegram Archive valid evidence in judicial systems worldwide.

### Cryptographic Integrity

- [ ] SHA-256 hash chains for all messages
- [ ] Merkle tree root calculation per backup run
- [ ] RFC 3161 Trusted Timestamping Authority integration
- [ ] Blockchain anchoring (Bitcoin/Ethereum) for immutable proof
- [ ] Tamper detection on archive verification

### Chain of Custody

- [ ] Immutable, hash-chained audit log
- [ ] Every action logged: backup, view, export, access attempts
- [ ] Multi-signature access for sensitive archives
- [ ] Role separation: archivist vs viewer permissions
- [ ] "Break glass" emergency access with mandatory logging

### Source Authentication

- [ ] Store Telegram server-side signatures with messages
- [ ] Device attestation (TPM/Secure Enclave where available)
- [ ] Cross-reference verification between independent archives

### Court-Ready Export

- [ ] Forensic export package format:
  ```
  evidence_package/
  ├── messages.json           # The actual messages
  ├── merkle_tree.json        # Full hash tree
  ├── tsa_timestamps/         # RFC 3161 timestamp tokens
  ├── blockchain_anchors/     # Transaction hashes
  ├── audit_log.json          # Hash-chained access log
  ├── telegram_signatures/    # Original Telegram auth data
  ├── device_attestation.json # Device proof
  └── verification_script.py  # Self-contained verifier
  ```
- [ ] Verification CLI tool: `telegram-archive verify evidence_package/`
- [ ] Legal template library (affidavits per jurisdiction)

### Standards Compliance

- [ ] ISO 27037 (Digital evidence handling)
- [ ] NIST SP 800-86 (Forensic techniques guide)
- [ ] eIDAS (EU qualified timestamps/signatures)
- [ ] Federal Rules of Evidence 901/902 (US)

### Configuration

```env
FORENSIC_MODE=true
HASH_ALGORITHM=SHA-256
TSA_URL=https://freetsa.org/tsr
BLOCKCHAIN_ANCHOR=ethereum
AUDIT_LOG_RETENTION=forever
```

---

## v9.0.0 — Multi-tenancy & Access Control

### Multi-tenant Architecture

- [ ] Single instance serving multiple users
- [ ] Per-user isolated databases or schemas
- [ ] Shared channel access between users
- [ ] Admin panel for user management

### Authentication Providers

- [ ] OAuth/Social login (Google, GitHub, Discord)
- [ ] Magic link authentication (passwordless email)
- [ ] OIDC/SAML support (Enterprise SSO)
- [ ] 2FA/MFA support

### Role-Based Permissions

- [ ] Admin: full access, user management
- [ ] Archivist: backup operations, no deletion
- [ ] Viewer: read-only access to assigned chats
- [ ] Per-chat access control lists

---

## Future Ideas

### Backup Features

- [ ] Multi-account support (backup multiple Telegram accounts)
- [ ] S3/MinIO cloud storage backend
- [ ] End-to-end encryption at rest
- [ ] Incremental backup compression
- [ ] Backup scheduling presets (conservative, aggressive)
- [ ] Telegram bot integration (trigger backup, get status via Telegram)

### Export & Integrations

- [ ] REST API for external integrations
- [ ] Webhooks for new message notifications
- [ ] Export formats: HTML archive, PDF, MBOX
- [ ] Scheduled backup reports (email/Slack)
- [ ] Import from other backup formats

### Mobile Experience

- [ ] Mobile-optimized gesture navigation
- [ ] Offline viewing capability (service worker cache)
- [ ] iOS/Android native app wrapper

### AI Features

- [ ] Chat summarization
- [ ] Auto-tagging and categorization
- [ ] Sentiment analysis dashboard
- [ ] Translation on-demand

---

## Recently Completed

Features that were previously on this roadmap and have been implemented:

| Feature | Version | Notes |
|---------|---------|-------|
| Message reactions display | v5.x | Emoji + custom emoji support |
| Sticker display | v5.x | Rendered in message view |
| Web Push notifications | v5.0 | Full mode with VAPID, persistent subscriptions |
| WebSocket real-time updates | v5.0 | Live message sync, edits, deletions |
| Mass operation rate limiting | v5.0 | Sliding window with configurable threshold |
| PWA support | v5.0 | Service worker, installable |
| Pinned messages | v5.4 | Multiple pins, real-time sync, banner |
| Forum topics | v6.2.0 | Topic navigation and filtering |
| Chat folders | v6.2.0 | Telegram dialog filters |
| Archived chats | v6.2.0 | Separate archived section |
| Dependabot + CodeQL | v6.2.3 | Automated dependency updates and SAST |
| Ruff linter/formatter | v6.2.3 | CI enforcement + pre-commit hooks |
| Security hardening | v6.2.3 | CSP, CORS, secure cookies, container hardening |
| PBKDF2 auth tokens | v6.2.4 | Replaced weak SHA256 hashing |

---

## Contributing

Have a feature request? [Open an issue](https://github.com/GeiserX/Telegram-Archive/issues)!

See [AGENTS.md](../AGENTS.md) for development guidelines.
