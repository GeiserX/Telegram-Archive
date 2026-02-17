# Telegram Archive Documentation

**Version:** 7.0 | **Last Updated:** 2026-02-17

Welcome to the Telegram Archive documentation. This folder contains comprehensive technical documentation for developers, operators, and users.

## Quick Navigation

### For First-Time Users
1. **Start here:** [v7.0 Quick Reference](./v70-quick-reference.md) — What's new, quick API reference, troubleshooting
2. **Setup:** See main [README.md](../README.md) for installation and configuration
3. **Questions?** Check FAQ in quick reference or file a GitHub issue

### For Developers
1. **Overview:** [Codebase Summary](./codebase-summary.md) — Project structure and modules
2. **Architecture:** [System Architecture](./system-architecture.md) — Design patterns and data flows
3. **Standards:** [Code Standards](./code-standards.md) — Coding patterns, type hints, testing
4. **Specification:** [Project Overview & PDR](./project-overview-pdr.md) — Requirements and features

### For DevOps/Operators
1. **Quick Start:** [v7.0 Quick Reference](./v70-quick-reference.md) — Migration checklist
2. **Architecture:** [System Architecture](./system-architecture.md) — Deployment patterns
3. **Changes:** [CHANGELOG](./CHANGELOG.md) — What's new in each version
4. **Reference:** [Codebase Summary](./codebase-summary.md) — Components overview

### For Product Managers
1. **Specification:** [Project Overview & PDR](./project-overview-pdr.md) — Features, requirements, metrics
2. **Changes:** [CHANGELOG](./CHANGELOG.md) — Release history
3. **Roadmap:** [ROADMAP](./ROADMAP.md) — Planned features

### For API Consumers
1. **Quick Reference:** [v7.0 Quick Reference](./v70-quick-reference.md) — Endpoint list with examples
2. **Architecture:** [System Architecture](./system-architecture.md) — Request/response patterns
3. **Standards:** [Code Standards](./code-standards.md) — Error handling and validation

## Documentation Files

### Core Documentation (v7.0)

| File | Purpose | Lines | Audience |
|------|---------|-------|----------|
| [v70-quick-reference.md](./v70-quick-reference.md) | What's new, quick API reference, troubleshooting | 300 | Everyone |
| [codebase-summary.md](./codebase-summary.md) | Technical overview of modules and structure | 191 | Developers, PM |
| [system-architecture.md](./system-architecture.md) | Design patterns, data flows, deployment | 389 | Developers, DevOps |
| [code-standards.md](./code-standards.md) | Development guidelines, patterns, examples | 600 | Developers |
| [project-overview-pdr.md](./project-overview-pdr.md) | Product requirements, features, acceptance criteria | 403 | PM, Developers |

### Reference Documentation

| File | Purpose | Content |
|------|---------|---------|
| [CHANGELOG.md](./CHANGELOG.md) | Complete version history | All releases with features, fixes, migration notes |
| [ROADMAP.md](./ROADMAP.md) | Future features and milestones | v7.1+, planned enhancements |

## v7.0 Features

### Search & Discovery
- Full-text search across all messages
- Advanced filters: sender, media type, date range
- Global cross-chat search
- Result highlighting with context
- Deep linking to specific messages

### Media Gallery
- Responsive grid view of all media
- Type filters: photo, video, document, audio
- Lightbox viewer with fullscreen
- Keyboard navigation
- Download functionality

### Transaction Accounting (NEW)
- Auto-detect monetary transactions from message text
- Support for multiple currencies (PHP, $, ₱, P)
- Keyword-based classification (credit/debit)
- Confidence scoring for accuracy
- Spreadsheet-like interface with inline editing
- CSV export with running balance

### User Experience
- Skeleton loading states for better perceived performance
- Keyboard shortcuts: Esc, Ctrl+K, ?
- URL hash routing for shareable links
- Mobile-responsive design
- Vue 3 frontend with Tailwind CSS

## Key Metrics

| Aspect | Value | Notes |
|--------|-------|-------|
| **API Endpoints** | 30+ | New in v7.0 |
| **Database Models** | 6 | Message, Chat, User, Reaction, Forward, Transaction |
| **Test Coverage** | High | Unit + async tests |
| **Performance (Search)** | <100ms | P95 for typical queries |
| **Performance (Media)** | <200ms | P95 gallery load |
| **Backward Compat** | Full | v6.x databases work unchanged |

## Technology Stack

**Backend**
- Python 3.11+
- FastAPI (web framework)
- SQLAlchemy (ORM)
- Telethon (Telegram client)

**Database**
- SQLite (default)
- PostgreSQL (recommended for large deployments)

**Frontend**
- Vue 3 (Composition API)
- Tailwind CSS
- HTML5 / ES2020 JavaScript

**Deployment**
- Docker & Docker Compose
- Alembic (database migrations)

## Getting Started

### For Users
1. See main [README.md](../README.md) for installation
2. Read [v70-quick-reference.md](./v70-quick-reference.md) for usage
3. Run migrations automatically on startup

### For Developers
1. Clone repository
2. Read [code-standards.md](./code-standards.md) for development rules
3. Check [system-architecture.md](./system-architecture.md) for project structure
4. Write tests following patterns in code standards

### For Contributors
1. Fork repository
2. Create feature branch
3. Follow [Code Standards](./code-standards.md)
4. Run tests: `pytest tests/`
5. Submit pull request
6. Reference relevant issues with `Fixes #123`

## Common Tasks

### I want to understand the codebase
1. Start: [Codebase Summary](./codebase-summary.md)
2. Deep dive: [System Architecture](./system-architecture.md)
3. Patterns: [Code Standards](./code-standards.md)

### I want to add a new feature
1. Requirements: [Project Overview & PDR](./project-overview-pdr.md)
2. Patterns: [Code Standards](./code-standards.md)
3. Architecture: [System Architecture](./system-architecture.md)

### I want to troubleshoot an issue
1. FAQ: [v70-quick-reference.md](./v70-quick-reference.md#troubleshooting)
2. Architecture: [System Architecture](./system-architecture.md) (error handling section)
3. Changelog: [CHANGELOG](./CHANGELOG.md) (known issues)

### I want to deploy to production
1. Setup: Main [README.md](../README.md)
2. Deployment: [System Architecture](./system-architecture.md#deployment-architecture)
3. Migration: [CHANGELOG](./CHANGELOG.md) (migration notes)

### I want to understand transaction detection
1. Quick overview: [v70-quick-reference.md#transaction-accounting](./v70-quick-reference.md#3-transaction-accounting)
2. Deep dive: [System Architecture](./system-architecture.md#transaction-detection-flow-v70)
3. Implementation: [Code Standards](./code-standards.md) (regex patterns)

## File Organization

```
Telegram-Archive/
├── README.md                    ← Start here for users
├── docs/                        ← You are here
│   ├── README.md               ← Documentation index
│   ├── v70-quick-reference.md  ← Quick start for v7.0
│   ├── codebase-summary.md     ← Technical overview
│   ├── system-architecture.md  ← Design & deployment
│   ├── code-standards.md       ← Development guidelines
│   ├── project-overview-pdr.md ← Requirements & spec
│   ├── CHANGELOG.md            ← Version history
│   └── ROADMAP.md              ← Future features
├── src/                         ← Source code
│   ├── db/                      ← Database layer
│   ├── web/                     ← FastAPI + Vue frontend
│   └── transaction_detector.py  ← Pattern matching
└── plans/
    └── reports/                 ← Documentation reports
```

## Documentation Standards

### Writing Style
- Clear, concise language
- Active voice preferred
- Code examples over prose for technical details
- Links to related documents
- Tables for structured information

### Code Examples
- Syntax-highlighted
- Runnable or representative
- Include imports and context
- Type hints included
- Error handling shown

### Cross-References
- Link to related sections
- Reference actual code files
- Include line numbers for code snippets
- Verify links work

## Feedback & Contributions

### Report Issues
- GitHub Issues: https://github.com/GeiserX/Telegram-Archive/issues
- Include documentation issue label
- Provide specific examples
- Suggest improvements

### Contribute Documentation
1. Fork repository
2. Edit documentation
3. Test links and examples
4. Submit pull request
5. Reference related issues

## Documentation Maintenance

**Update Schedule:**
- Security updates: Immediate
- Feature documentation: With each release
- API changes: Immediately
- Examples: Quarterly review

**Responsibility:**
- Feature author: Initial documentation
- Maintainer: Review and approval
- Community: Corrections and improvements

**Version Control:**
- Changes tracked in git
- CHANGELOG reflects updates
- Documentation reviewed in PRs

## Quick Links

- **Main README:** [README.md](../README.md)
- **GitHub Repository:** https://github.com/GeiserX/Telegram-Archive
- **Issues:** https://github.com/GeiserX/Telegram-Archive/issues
- **Discussions:** https://github.com/GeiserX/Telegram-Archive/discussions
- **Releases:** https://github.com/GeiserX/Telegram-Archive/releases

## Version Info

- **Current Version:** 7.0
- **Release Date:** 2026-02-17
- **Minimum Python:** 3.11
- **Database:** SQLite 3.36+ or PostgreSQL 12+
- **Browsers:** Chrome 90+, Firefox 88+, Safari 14+

---

**Last Updated:** 2026-02-17
**Maintained by:** Telegram Archive Team
**License:** GPL-3.0
