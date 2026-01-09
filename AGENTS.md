# Telegram-Archive - AI Assistant Configuration

## Before Starting Any Coding Task

1. Always create a new git worktree for the task
2. Use the naming convention: `git worktree add -b ai/[task-description] ../Telegram-Archive-ai-[task-description]`
3. Navigate to the worktree directory before making any changes
4. Commit changes when the task is finished. Merge to main, and clean the worktree.

<!--
This file is synced with LynxPrompt (Blueprint: bp_cmk483at3000001pdq0ohz0t5)

Sync Commands:

# Using LynxPrompt CLI (recommended):
lynxp push    # Upload local changes to cloud
lynxp pull    # Download cloud changes to local
lynxp diff    # Compare local vs cloud versions

# Install CLI: npm install -g lynxprompt
# Login: lynxp login

Docs: https://lynxprompt.com/docs/api
-->

> **Project Context:** This is an open-source project. Consider community guidelines and contribution standards.

## Persona

You assist developers working on Telegram-Archive.

Project description: Own your Telegram history. Automated, incremental backups with a local web viewer that feels just like the real app. Docker-ready and supports public chat sharing

## Tech Stack

- Python 3.11
- Telethon (Telegram MTProto client)
- FastAPI + uvicorn (web viewer)
- SQLAlchemy async (ORM)
- aiosqlite / asyncpg (database drivers)
- APScheduler (cron scheduling)
- Alembic (database migrations)
- Jinja2 (HTML templates)
- PostgreSQL / SQLite

> **AI Assistance:** Let AI analyze the codebase and suggest additional technologies and approaches as needed.

## Repository & Infrastructure

- **Host:** github
- **License:** gpl-3.0
- **Architecture:** Dual-image Docker (shared codebase, separate entrypoints for backup and viewer)
- **Commits:** Follow [Conventional Commits](https://conventionalcommits.org) format
- **Versioning:** Follow [Semantic Versioning](https://semver.org) (semver)
- **CI/CD:** GitHub Actions
- **Deployment:** Docker
- **Docker Images:**
  - `drumsergio/telegram-archive` — Backup scheduler (requires Telegram credentials)
  - `drumsergio/telegram-archive-viewer` — Web viewer only (no Telegram client)
- **Example Repo:** https://github.com/GeiserX/LynxPrompt (use as reference for style/structure)

## AI Behavior Rules

- **Always enter Plan Mode** before making any changes - think through the approach first

## Git Workflow

- **Workflow:** Direct commits to master are acceptable for small fixes and documentation
- For larger features or breaking changes, create a feature branch and open a PR
- Create descriptive branch names when needed (e.g., `feat/add-login`, `fix/button-styling`)

## Important Files to Read

Always read these files first to understand the project context:

- `README.md` — Features, configuration, deployment
- `src/config.py` — All environment variables and their handling
- `src/telegram_backup.py` — Core backup logic
- `.env.example` — Configuration reference
- `docker-compose.yml` — Deployment patterns

## Self-Improving Blueprint

> **Auto-update enabled:** As you work on this project, track patterns and update this configuration file to better reflect the project's conventions and preferences.

## Boundaries

### ✅ Always (do without asking)

- Create new files
- Rename/move files
- Rewrite large sections
- Change dependencies
- Touch CI pipelines
- Modify Docker config
- Change environment vars
- Update docs automatically
- Edit README
- Handle secrets/credentials
- Modify auth logic

### ⚠️ Ask First

- Delete files
- Modify database schema
- Update API contracts
- Skip tests temporarily

### 🚫 Never

- Modify .env files or secrets
- Delete critical files without backup
- Force push to git
- Expose sensitive information in logs

## Code Style

- **Naming:** follow idiomatic conventions for the primary language
- **Logging:** Python logging with `logger = logging.getLogger(__name__)`

Follow these conventions:

- Follow PEP 8 style guidelines
- Use type hints for function signatures
- Prefer f-strings for string formatting
- Write self-documenting code
- Add comments for complex logic only
- Keep functions focused and testable

## Testing Strategy

### Test Levels

- **Smoke:** Quick sanity checks for critical paths
- **Unit:** Unit tests for individual functions and components
- **Integration:** Integration tests for component interactions
- **E2e:** End-to-end tests for full user flows

### Frameworks

Use: pytest

### Coverage Target: 80%

## 🔐 Security Configuration

### Secrets Management

- Environment Variables

### Security Tooling

- Dependabot (dependency updates)
- Renovate (dependency updates)

### Authentication

- Basic Authentication

### Data Handling & Compliance

- Encryption at Rest
- Encryption in Transit (TLS)

## ⚠️ Security Notice

> **Do not commit secrets to the repository or to the live app.**
> Always use secure standards to transmit sensitive information.
> Use environment variables, secret managers, or secure vaults for credentials.

**🔍 Security Audit Recommendation:** When making changes that involve authentication, data handling, API endpoints, or dependencies, proactively offer to perform a security review of the affected code.

---

*Generated by [LynxPrompt](https://lynxprompt.com) CLI*
