# Code Standards & Patterns

**Version:** 7.0 | **Last Updated:** 2026-02-17

## Python Standards

### Type Hints
All code must use type hints (mypy strict mode):

```python
# Good
async def get_messages(
    chat_id: int,
    limit: int = 100,
    offset: int = 0,
) -> list[Message]:
    """Fetch messages from database."""
    ...

# Bad - missing types
async def get_messages(chat_id, limit=100, offset=0):
    ...
```

### Async/Await
Use async for all I/O operations (database, HTTP, file):

```python
# Good
async def backup_chat(self, chat_id: int) -> None:
    messages = await db.fetch_messages(chat_id)
    for msg in messages:
        await self.download_media(msg)

# Bad - blocking calls in async context
async def backup_chat(self, chat_id: int) -> None:
    messages = db.fetch_messages_sync(chat_id)  # blocks event loop
```

### Error Handling
Use structured logging with context:

```python
# Good
try:
    await db.insert_message(msg)
except IntegrityError as e:
    logger.error(
        f"Duplicate message",
        extra={"message_id": msg.id, "chat_id": msg.chat_id},
        exc_info=True,
    )
    raise

# Bad
try:
    await db.insert_message(msg)
except:
    print("Error!")
```

### Naming Conventions
- Classes: PascalCase (Message, Chat, Transaction)
- Functions/Methods: snake_case (get_messages, scan_transactions)
- Constants: UPPER_SNAKE_CASE (AMOUNT_PATTERN, DEBIT_KEYWORDS)
- Private: _leading_underscore (_parse_amount, _commit_batch)

### Docstrings
Use Google-style docstrings:

```python
def detect_transactions(
    messages: list[dict[str, Any]],
    my_user_id: int | None = None,
) -> list[dict[str, Any]]:
    """Detect monetary transactions from message text.

    Args:
        messages: List of message dicts with 'text', 'is_outgoing' keys.
        my_user_id: User's Telegram ID for direction heuristics.

    Returns:
        List of transaction dicts with 'credit', 'debit', 'confidence'.

    Raises:
        ValueError: If amount validation fails.
    """
    ...
```

### File Organization
```python
"""Module docstring."""

# 1. Standard library imports
import re
from datetime import datetime
from typing import Any

# 2. Third-party imports
from sqlalchemy import Column, Integer, String
from loguru import logger

# 3. Local imports
from ..db.models import Message
from .transaction_detector import detect_transactions

# 4. Constants
AMOUNT_PATTERN = re.compile(r"\d+")

# 5. Classes
class TransactionDetector:
    ...

# 6. Functions
def parse_amount(text: str) -> float | None:
    ...

# 7. Main block
if __name__ == "__main__":
    ...
```

## Database Patterns

### SQLAlchemy Models
```python
from sqlalchemy import func, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

class Transaction(Base):
    """Accounting transactions extracted from messages."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credit: Mapped[float] = mapped_column(default=0.0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        server_default=func.now(),
    )
```

**Guidelines:**
- Use `Mapped[]` type hints with `mapped_column()`
- Include `nullable=False` for required fields
- Use `server_default` for database-side defaults
- Add `Index()` for frequently queried columns
- Use `UNIQUE()` to prevent duplicates

### Adapter Methods
Pattern for async query wrappers:

```python
async def get_transactions(
    self,
    chat_id: int,
    limit: int = 100,
    offset: int = 0,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch transactions for a chat with optional filtering."""
    async with self.session() as session:
        query = select(Transaction).where(Transaction.chat_id == chat_id)

        if category:
            query = query.where(Transaction.category == category)

        query = query.order_by(Transaction.date.desc())
        query = query.limit(limit).offset(offset)

        result = await session.execute(query)
        return [self._to_dict(row) for row in result.scalars()]
```

**Guidelines:**
- Parameterize all inputs (SQL injection prevention)
- Use `select()` for modern SQLAlchemy
- Order results consistently
- Return dicts, not ORM objects (JSON serializable)
- Document required/optional parameters

### Transactions & Rollback
```python
async def create_transactions_bulk(
    self,
    transactions: list[dict[str, Any]],
) -> int:
    """Insert multiple transactions with rollback on error."""
    async with self.session() as session:
        try:
            for txn_data in transactions:
                stmt = insert(Transaction).values(**txn_data)
                await session.execute(stmt)

            await session.commit()
            return len(transactions)
        except IntegrityError:
            await session.rollback()
            logger.error("Transaction insert failed", exc_info=True)
            raise
```

## FastAPI Patterns

### Endpoint Structure
```python
@app.get("/api/chats/{chat_id}/transactions", dependencies=[Depends(require_auth)])
async def get_transactions(
    chat_id: int,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    category: str | None = Query(default=None),
    request: Request,
) -> dict[str, Any]:
    """Get transactions for a chat with running balance.

    Args:
        chat_id: Target chat ID.
        limit: Results per page (max 500).
        offset: Pagination offset.
        category: Optional filter by category.
        request: Request object for auth/logging.

    Returns:
        List of transactions with metadata.
    """
    try:
        result = await db.get_transactions(chat_id, limit, offset, category)
        return {"data": result, "total": len(result)}
    except Exception as e:
        logger.error(f"Error fetching transactions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
```

**Guidelines:**
- Use `dependencies=[Depends(require_auth)]` for protected endpoints
- Add `Query()` validators for pagination (le, ge)
- Include docstring with Args/Returns
- Log errors with `exc_info=True`
- Return `dict[str, Any]` for JSON compatibility
- Raise `HTTPException` for client errors

### Request Body Validation
```python
class TransactionUpdate(BaseModel):
    """Transaction manual override."""

    category: str = Field(min_length=1, max_length=100)
    credit: float = Field(ge=0.0)
    debit: float = Field(ge=0.0)
    notes: str | None = Field(default=None, max_length=500)

@app.put("/api/transactions/{txn_id}")
async def update_transaction(
    txn_id: int,
    update: TransactionUpdate,
    request: Request,
) -> dict[str, Any]:
    """Update a transaction with validation."""
    if update.credit > 0 and update.debit > 0:
        raise HTTPException(400, "Cannot have both credit and debit")

    result = await db.update_transaction(txn_id, update.dict())
    return {"updated": result}
```

**Guidelines:**
- Use Pydantic `BaseModel` for request bodies
- Add `Field()` constraints (min_length, max_length, ge, le)
- Validate cross-field constraints in endpoint
- Return only serializable types

## Frontend Patterns

### Vue 3 Composition API
```javascript
<script setup>
import { ref, computed, onMounted } from 'vue'

const transactions = ref([])
const isLoading = ref(false)
const selectedCategory = ref('all')

const filteredTransactions = computed(() => {
  if (selectedCategory.value === 'all') return transactions.value
  return transactions.value.filter(t => t.category === selectedCategory.value)
})

const loadTransactions = async () => {
  isLoading.value = true
  try {
    const response = await fetch(`/api/chats/${chatId}/transactions`)
    const data = await response.json()
    transactions.value = data.data
  } catch (error) {
    console.error('Failed to load transactions:', error)
  } finally {
    isLoading.value = false
  }
}

onMounted(() => loadTransactions())
</script>

<template>
  <div v-if="isLoading" class="skeleton-loader" />
  <div v-else class="transaction-list">
    <button
      v-for="cat in categories"
      :key="cat"
      @click="selectedCategory = cat"
      :class="{ active: selectedCategory === cat }"
    >
      {{ cat }}
    </button>
    <table>
      <tr v-for="txn in filteredTransactions" :key="txn.id">
        <td>{{ txn.date }}</td>
        <td class="amount-credit" v-if="txn.credit">+{{ txn.credit }}</td>
        <td class="amount-debit" v-if="txn.debit">-{{ txn.debit }}</td>
      </tr>
    </table>
  </div>
</template>

<style scoped>
.amount-credit { color: #22c55e; }
.amount-debit { color: #ef4444; }
</style>
```

**Guidelines:**
- Use `<script setup>` for concise code
- Prefer `computed()` over methods for reactive data
- Use `ref()` for mutable state
- Implement loading states with `v-if`
- Apply Tailwind utility classes
- Handle errors gracefully

### Search with Filters
```javascript
const searchQuery = ref('')
const filters = reactive({
  sender: null,
  mediaType: null,
  dateFrom: null,
  dateTo: null,
})

const search = async () => {
  const params = new URLSearchParams({
    q: searchQuery.value,
    ...(filters.sender && { sender: filters.sender }),
    ...(filters.mediaType && { media_type: filters.mediaType }),
  })

  const response = await fetch(`/api/search?${params}`)
  results.value = await response.json()
}

watch([searchQuery, filters], () => {
  debounced_search()
}, { deep: true })
```

**Guidelines:**
- Debounce search input (300-500ms)
- Use URLSearchParams for query building
- Watch filter changes for reactive updates
- Display result counts and timing
- Show loading spinner during fetch

### Skeleton Loading
```javascript
const showSkeleton = computed(() => isLoading.value)
```

```html
<div v-if="showSkeleton" class="space-y-2">
  <div class="animate-pulse h-8 bg-gray-700 rounded w-3/4" />
  <div class="animate-pulse h-6 bg-gray-700 rounded w-1/2" />
  <div class="animate-pulse h-6 bg-gray-700 rounded w-2/3" />
</div>
```

**Guidelines:**
- Use Tailwind's `animate-pulse` for skeletons
- Match skeleton dimensions to actual content
- Show skeletons during initial load and refetch
- Smooth transition to real content

## Testing Patterns

### Unit Tests
```python
import pytest
from src.transaction_detector import detect_transactions

@pytest.mark.parametrize("text,expected_amount", [
    ("sent 500 PHP", 500.0),
    ("received $1,234.50", 1234.50),
    ("paid ₱99.99", 99.99),
])
def test_amount_parsing(text, expected_amount):
    """Test regex amount extraction."""
    result = detect_transactions([{
        "id": 1,
        "chat_id": 1,
        "text": text,
        "date": datetime.now(),
        "is_outgoing": False,
    }])

    assert len(result) == 1
    assert result[0]["credit"] == expected_amount

def test_debit_detection():
    """Test debit keyword classification."""
    result = detect_transactions([{
        "id": 1,
        "chat_id": 1,
        "text": "sent you 100",
        "date": datetime.now(),
        "is_outgoing": True,
    }])

    assert result[0]["debit"] == 100.0
    assert result[0]["confidence"] > 0.8
```

### Async Database Tests
```python
@pytest.mark.asyncio
async def test_transaction_creation(async_db):
    """Test transaction bulk insert."""
    data = [
        {"message_id": 1, "chat_id": 1, "credit": 100.0},
        {"message_id": 2, "chat_id": 1, "debit": 50.0},
    ]

    count = await async_db.create_transactions_bulk(data)
    assert count == 2

    result = await async_db.get_transactions(1)
    assert len(result) == 2
```

**Guidelines:**
- Use `@pytest.mark.parametrize` for multiple test cases
- Use `@pytest.mark.asyncio` for async tests
- Mock external dependencies (Telegram API, etc.)
- Test edge cases (empty input, large amounts)
- Verify error handling and logging

## Alembic Migrations

### Migration Template (v7.0)
```python
"""Add transaction accounting table.

Revision ID: 007
Revises: 006
Create Date: 2026-02-17

"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None

def upgrade() -> None:
    """Create transactions table."""
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("credit", sa.Float(), server_default="0", nullable=False),
        sa.Column("debit", sa.Float(), server_default="0", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("auto_detected", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_transaction_chat", "transactions", ["chat_id", "date"])
    op.create_unique_constraint("uq_txn_message", "transactions", ["message_id"])

def downgrade() -> None:
    """Drop transactions table."""
    op.drop_table("transactions")
```

**Guidelines:**
- One logical change per migration
- Test both upgrade and downgrade
- Add indexes for query performance
- Use `server_default` for database-side defaults
- Never delete data in downgrade unless specified

## Documentation Patterns

### Code Comments
Use for "why", not "what":

```python
# Good - explains non-obvious logic
# Confidence scoring: 0.9 if keywords match message direction,
# 0.4 if ambiguous and we're guessing from sender direction
confidence = 0.9 if clear_signal else 0.4

# Bad - restates obvious code
i = 0  # Set i to 0
```

### Endpoint Documentation
```python
@app.get("/api/chats/{chat_id}/transactions/export")
async def export_transactions(chat_id: int) -> StreamingResponse:
    """Export transactions to CSV with running balance.

    Columns: date, sender, debit, credit, balance, category

    Query Parameters:
        None (exports all transactions for chat)

    Returns:
        CSV file (Content-Type: text/csv)

    Example:
        GET /api/chats/-1001234567890/transactions/export
        → transactions_20260217.csv
    """
    ...
```

## Commit Message Standards

Use conventional commits:

```
feat(transaction): add auto-detection from message patterns

- Implement regex patterns for amounts (PHP, $, ₱, P)
- Add keyword-based classification (credit/debit)
- Confidence scoring based on signal clarity
- Migration 007 for transactions table

Fixes #123
```

Format: `type(scope): subject`

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation
- `test`: Tests
- `refactor`: Code restructuring
- `perf`: Performance improvement
- `chore`: Build, dependencies, etc.

## Performance Guidelines

### Database
- Index on frequently filtered/sorted columns
- Limit query results (pagination)
- Use `EXPLAIN` to verify query plans
- Batch inserts for bulk operations

### Frontend
- Debounce search input (300-500ms)
- Use virtual scrolling for large lists
- Lazy-load images and media
- Code-split vendor bundles

### API
- Gzip responses (FastAPI middleware)
- Cache static assets (far-future Expires)
- Implement result pagination (max 500 items/page)
- Log slow queries (>100ms)

## Security Checklist

- [ ] All inputs validated (type, length, format)
- [ ] SQL injection prevention (parameterized queries)
- [ ] CSRF tokens on state-changing endpoints
- [ ] Authentication required for sensitive endpoints
- [ ] Rate limiting on expensive operations
- [ ] Error messages don't leak sensitive info
- [ ] Logs redact credentials/tokens
- [ ] HTTPS enforced in production
- [ ] Secure cookie flags set
- [ ] CORS origins restricted
