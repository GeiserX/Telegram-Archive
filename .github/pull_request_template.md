## Summary

<!-- Describe the change in 1-2 bullets -->

-

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that causes existing functionality to change)
- [ ] Documentation update
- [ ] Infrastructure/CI change

## Database Changes

- [ ] Schema changes (Alembic migration required)
- [ ] Data migration script added in `scripts/`
- [ ] No database changes

## Data Consistency Checklist

<!-- Required when modifying database code (see AGENTS.md) -->

- [ ] All `chat_id` values use marked format (via `_get_marked_id()`)
- [ ] All datetime values pass through `_strip_tz()` before DB operations
- [ ] INSERT and UPDATE operations handle the same fields identically

## Testing

- [ ] Tests pass locally (`python -m pytest tests/ -v`)
- [ ] Linting passes (`ruff check .`)
- [ ] Formatting passes (`ruff format --check .`)
- [ ] Manually tested in development environment

## Security Checklist

- [ ] No secrets or credentials committed
- [ ] User input properly validated/sanitized
- [ ] Authentication/authorization properly checked

## Deployment Notes

<!-- Any special considerations for deployment? Docker image rebuild needed? -->

-
