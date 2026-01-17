# Telegram Archive Roadmap

This document outlines planned features and the long-term vision for Telegram Archive.

For version history and changes, see [CHANGELOG.md](./CHANGELOG.md).

---

## v5.0.0 - Real-time Sync & High-Performance Viewer (In Progress)

**Status:** PR #11 in testing

### Core Features
- [x] Real-time listener mode (ENABLE_LISTENER)
- [x] Instant message/edit/deletion sync
- [x] Rate-limiting protection against mass operations
- [x] Shared TelegramClient (prevents session locking)
- [x] Per-chat statistics in viewer
- [x] Server-side chat search
- [x] Cursor-based pagination (O(1) performance)
- [x] WebSocket real-time updates (PostgreSQL LISTEN/NOTIFY)
- [x] Album/grouped media display
- [x] Chat restore script (scripts/restore_chat.py)

### Remaining
- [ ] Final testing on production data
- [ ] Documentation updates
- [ ] Release

---

## v6.0.0 - Forensic & Legal Admissibility (Planned)

**Goal:** Make Telegram Archive valid evidence in judicial systems worldwide.

### Cryptographic Integrity
- [ ] SHA-256 hash chains for all messages
- [ ] Merkle tree root calculation per backup
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

## v7.0.0 - Multi-tenancy & Access Control (Planned)

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

### Viewer Enhancements
- [ ] Full-text search across all messages
- [ ] Message reactions display
- [ ] Chat statistics dashboard
- [ ] Custom themes
- [ ] Sticker/animated emoji support

### Backup Features
- [ ] Multi-account support (backup multiple Telegram accounts)
- [ ] S3/MinIO cloud storage backend
- [ ] End-to-end encryption at rest
- [ ] Incremental backup compression
- [ ] Backup scheduling presets (conservative, aggressive)

### Integrations
- [ ] REST API for external integrations
- [ ] Webhooks for new message notifications
- [ ] Export formats: HTML archive, PDF, MBOX
- [ ] Scheduled backup reports (email/Slack)

### Mobile
- [ ] Progressive Web App (PWA) support
- [ ] Mobile-optimized viewer interface
- [ ] Offline viewing capability

---

## Contributing

Have a feature request? [Open an issue](https://github.com/GeiserX/Telegram-Archive/issues)!

See [AGENTS.md](../AGENTS.md) for development guidelines.
