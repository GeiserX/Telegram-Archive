# Contributing to Telegram-Archive

Thank you for considering contributing to Telegram-Archive!

## Quick Start

1. Fork the repository
2. Clone your fork:

   ```bash
   git clone https://github.com/YOUR_USERNAME/Telegram-Archive.git
   cd Telegram-Archive
   ```

3. Install dependencies:

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   pip install -e ".[dev]"
   ```

4. Set up pre-commit hooks:

   ```bash
   pre-commit install
   ```

5. Copy the environment file:

   ```bash
   cp .env.example .env
   # Edit .env with your Telegram API credentials (for backup testing only)
   ```

6. Run tests:

   ```bash
   python -m pytest tests/ -v
   ```

## Development Workflow

### Branch Naming

- `feat/` — New features
- `fix/` — Bug fixes
- `docs/` — Documentation updates
- `refactor/` — Code refactoring
- `test/` — Test additions/updates

### Commit Messages

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`

Examples:

- `feat(viewer): add message search`
- `fix(backup): handle timezone-aware datetimes in updates`
- `docs(readme): update Docker configuration section`

### Pull Requests

1. Create a feature branch from `master`
2. Make your changes
3. Run tests: `python -m pytest tests/ -v`
4. Run linting: `ruff check .`
5. Run formatting: `ruff format --check .`
6. Submit a PR with a clear description using the PR template

## Code Style

- Follow PEP 8 guidelines (enforced by Ruff)
- Use type hints for function signatures
- Prefer f-strings for string formatting
- Write self-documenting code; add comments only for complex logic
- Keep functions focused and testable

### Linting and Formatting

We use [Ruff](https://docs.astral.sh/ruff/) for both linting and formatting:

```bash
# Check for lint issues
ruff check .

# Auto-fix lint issues
ruff check --fix .

# Check formatting
ruff format --check .

# Auto-format
ruff format .
```

## Data Consistency Rules

When modifying database code, verify:

- All `chat_id` values use marked format (via `_get_marked_id()`)
- All datetime values pass through `_strip_tz()` before DB operations
- INSERT and UPDATE operations handle the same fields identically
- Tests exist in `tests/test_db_adapter.py` for data type handling

See the [AGENTS.md](AGENTS.md) for detailed consistency rules.

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=src --cov-report=term-missing

# Run a specific test file
python -m pytest tests/test_db_adapter.py -v
```

### When to Add Tests

- Fixing a bug — write a test that would have caught it
- Adding DB operations — test data type handling
- Modifying config parsing — test edge cases
- Adding new features — test happy path and error cases

## Project Structure

```
src/
├── __main__.py         # Entry point
├── config.py           # Environment variable handling
├── telegram_backup.py  # Core backup logic
├── realtime.py         # WebSocket real-time updates
├── db/
│   ├── adapter.py      # Database operations (SQLAlchemy async)
│   ├── models.py       # SQLAlchemy models
│   └── session.py      # Database session management
└── web/
    ├── main.py         # FastAPI application
    ├── static/         # CSS, JS, images
    └── templates/      # Jinja2 templates
```

## Reporting Bugs

Please use the [GitHub issue tracker](https://github.com/GeiserX/Telegram-Archive/issues) with the bug report template. Include:

- Clear description
- Steps to reproduce
- Expected vs actual behavior
- Docker logs if applicable

## Feature Requests

Open an issue with the feature request template. Include:

- Clear use case
- Proposed solution
- Alternatives considered

## License

By contributing, you agree that your contributions will be licensed under the GPL-3.0 License.
