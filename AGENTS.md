# telegram-archive - AI Assistant Configuration

## Before Starting Any Coding Task

1. Check existing worktrees with `git worktree list` and create a new one for this task if needed
2. Use the naming convention: `git worktree add -b ai/<task> .worktrees/<task>`
3. Navigate to the worktree directory before making any changes
4. Commit changes when the task is finished. Merge to main, and clean the worktree.

## Persona

You assist developers working on telegram-archive.

## Tech Stack

- Python
- FastAPI
- sqlalchemy

> **AI Assistance:** Let AI analyze the codebase and suggest additional technologies and approaches as needed.

## Repository & Infrastructure

- **License:** GPL-3.0
- **CI/CD:** 
- **Commits:** Follow [Conventional Commits](https://conventionalcommits.org) format
- **Versioning:** Follow [Semantic Versioning](https://semver.org) (semver)
- **CI/CD:** 

## AI Behavior Rules

- Optimize code for LLM reasoning: prefer flat/explicit patterns, minimal abstractions, structured logging, and linear control flow
- When you learn new project patterns or conventions, suggest updates to this configuration file
- Always verify your work before returning: run tests, check builds, confirm changes work as expected
- Reuse existing terminals when possible. Close terminals you no longer need
- Always check documentation (via MCP or project docs) before assuming knowledge about APIs or libraries
- **Use Plan Mode** for complex tasks, multi-step changes, or risky modifications
- When stuck, **attempt creative workarounds** before asking for help

## Git Workflow

- **Workflow:** Create feature branches and submit pull requests
- Create a descriptive branch name (e.g., `feat/add-login`, `fix/button-styling`)
- Open a PR for review before merging
- Do NOT commit directly to main/master branch
- **After every push/merge**, check CI status with `gh run list` or `gh pr checks` and fix any test or lint failures before moving on

## Boundaries

### ✅ Always (do without asking)

- Read any file in the project
- Modify files in src/ or lib/
- Run build, test, and lint commands
- Create test files
- Fix linting errors automatically

### ⚠️ Ask First

- Add new dependencies to package.json
- Modify configuration files at root level
- Create new modules or directories
- Refactor code structure significantly

### 🚫 Never

- Modify .env files or secrets
- Delete critical files without backup
- Force push to git
- Expose sensitive information in logs

## Code Style

- **Naming:** follow idiomatic conventions for the primary language
- **Logging:** [90m⏭ Skip[39m

Follow these conventions:

- Follow PEP 8 style guidelines
- Use type hints for function signatures
- Prefer f-strings for string formatting
- Write self-documenting code
- Add comments for complex logic only
- Keep functions focused and testable

## 🔐 Security Configuration

### Secrets Management

- Environment Variables

### Security Tooling

- Dependabot (dependency updates)
- Renovate (dependency updates)

### Data Handling & Compliance

- Encryption at Rest
- Encryption in Transit (TLS)

## ⚠️ Security Notice

> **Do not commit secrets to the repository or to the live app.**
> Always use secure standards to transmit sensitive information.
> Use environment variables, secret managers, or secure vaults for credentials.

**🔍 Security Audit Recommendation:** When making changes that involve authentication, data handling, API endpoints, or dependencies, proactively offer to perform a security review of the affected code.

## Architecture — Key Patterns

### Module Structure

- **`src/telegram_backup.py`** — Scheduled backup flow: `backup_all()` → `_backup_dialog()` → iterates messages → `_process_message()` → `_commit_batch()`. Gap filling: `_fill_gaps()` → `_fill_gap_range()`. Forum topics: `_backup_forum_topics()`.
- **`src/listener.py`** — Real-time event handlers: `on_new_message`, `on_message_edited`, `on_message_deleted`, `on_chat_action`, `on_pinned_messages`. Instantiated with `TelegramListener(config, db, client)`.
- **`src/config.py`** — All config from env vars. Required: `API_ID`, `API_HASH`, `PHONE_NUMBER`. Properties are lazy-parsed from env.
- **`src/message_utils.py`** — Shared utility module. Contains `extract_topic_id(message)` used by both backup and listener.
- **`src/db/adapter.py`** — Database operations. `src/db/models.py` — SQLAlchemy models. `src/db/base.py` — DB manager.

### Forum Topic Filtering

Topic IDs are extracted from `message.reply_to.reply_to_top_id` (primary) with fallback to `reply_to_msg_id`. The General topic (id=1) service messages may not carry `reply_to` metadata and can bypass filtering. The `SKIP_TOPIC_IDS` env var uses format `chat_id:topic_id,...` parsed into `dict[int, set[int]]`.

### Logging Rules

- **Never log chat IDs, topic IDs, or topic titles** — these are considered PII per the project's guidelines. Log only aggregated counts (e.g., "skipping N topics across M chats").
- **Never log message content** — same PII rule applies.

## CI/CD Pipeline

### Lint Workflow (`.github/workflows/lint.yml`)

CI runs **both** `ruff check .` AND `ruff format --check .`. Always run both locally before pushing:
```bash
python3 -m ruff check . && python3 -m ruff format --check .
```

### Test Workflow (`.github/workflows/tests.yml`)

- Runs `pytest tests/` with `--cov=src --cov-report=xml`
- Uploads to Codecov
- Python 3.14 on Ubuntu
- Web tests (test_database_viewer, test_multi_user_auth, test_v720_features) require FastAPI/pydantic — may fail locally if versions mismatch

### CodeRabbit

- Free OSS plan has **hourly commit rate limits** (low threshold)
- The incremental review system marks rate-limited commits as "reviewed" even though no review was posted
- To force a full review after rate limit expires: comment `@coderabbitai full review`
- **Do NOT trigger repeatedly** — each trigger counts against the limit and extends the cooldown
- Wait the **full cooldown** (check the minutes shown in the rate limit message), then trigger exactly **once**

## Testing — Critical Patterns

### MagicMock Truthiness Pitfall

When using `MagicMock()` for Telegram message objects, any attribute access returns a truthy MagicMock. This breaks code that checks `message.reply_to` or `getattr(reply_to, "forum_topic", False)`.

**ALWAYS set these on mock messages:**
```python
msg = MagicMock()
msg.reply_to = None  # Prevents false-positive topic filtering
```

**ALWAYS set this on mock configs that use `MagicMock()` (not real Config):**
```python
config.should_skip_topic = MagicMock(return_value=False)
```

### Test Style

- Existing tests use `unittest.TestCase` with `MagicMock`/`AsyncMock` — follow this pattern for consistency
- Config tests use `patch.dict(os.environ, {...}, clear=True)` — required env vars: `API_ID`, `API_HASH`, `PHONE_NUMBER`
- Async tests use `pytest.mark.asyncio`
- The `TelegramBackup` is instantiated via `TelegramBackup.__new__(TelegramBackup)` with mocked `db`, `client`, `config`

### Frameworks

Use: pytest, pytest-asyncio, pytest-cov

### Coverage Target: 80%

## Alembic Migrations — Critical Reminders

- **`Base.metadata.create_all(checkfirst=True)`** creates ALL tables from SQLAlchemy models at once, including tables that should be created by future Alembic migrations. This means pre-Alembic databases can have schema objects from migrations that haven't "run" yet.
- **`scripts/entrypoint.sh`** stamps pre-Alembic databases by detecting which schema objects exist. **Every time you add a new migration, you MUST update the stamping logic in entrypoint.sh** — both the PostgreSQL block and the SQLite block — to detect the new migration's artifacts (tables, columns, indexes). If you forget, existing databases that were created via `create_all()` will be stamped at a lower version, and Alembic will try to re-create objects that already exist, causing crash-loops.
- **Every migration MUST be idempotent.** Use `sa.inspect(conn)` to check if tables/columns/indexes exist before creating them. The stamping logic only helps fresh databases (no `alembic_version` table); databases already stamped at an older version skip stamping entirely, so Alembic runs the migration against a schema that `create_all()` may have already populated. Pattern:
  ```python
  conn = op.get_bind()
  inspector = sa.inspect(conn)
  if "my_column" not in {c["name"] for c in inspector.get_columns("my_table")}:
      op.add_column(...)
  if "my_table" not in inspector.get_table_names():
      op.create_table(...)
  ```
- Check highest migration first, then descend (009 → 008 → 007 → ...).
- Test with both PostgreSQL and SQLite paths.

---

*Generated by [LynxPrompt](https://lynxprompt.com) CLI*
