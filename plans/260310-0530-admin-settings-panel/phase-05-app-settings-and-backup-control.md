# Phase 5: App Settings Model & Backup Control

**Priority:** MEDIUM-HIGH
**Status:** TODO
**Effort:** Large (cross-container, DB model, scheduler changes)
**Files:** `src/db/models.py`, `src/db/adapter.py`, `src/web/main.py`, `src/scheduler.py`, `src/web/templates/index.html`, new Alembic migration

---

## Overview

Enable admin to control backup schedule from the UI. Since viewer and backup run in separate containers sharing a DB, we use a `app_settings` key-value table as the communication channel. Backup container polls settings every 60s and adjusts its schedule. Also supports "active user mode" — when someone is actively browsing, backup runs every 5 min.

---

## Key Insights

- Backup container uses APScheduler with `CronTrigger` — has `reschedule_job()` method
- Containers share SQLite DB via Docker volume (`../database:/data`)
- No cross-container HTTP or IPC exists — DB is the only shared resource
- `SCHEDULE` env var currently: `0 */6 * * *` (every 6 hours)
- Scheduler object is `self.scheduler` in `BackupScheduler` class
- `asyncio_scheduler.reschedule_job("telegram_backup", trigger=new_trigger)` is the API
- Metadata table already exists for VAPID keys — can reuse pattern or create dedicated table

---

## Architecture

```
app_settings table (in shared SQLite DB)
┌──────────────────────────┬──────────────────────┬─────────────────┐
│ key                      │ value                │ updated_at      │
├──────────────────────────┼──────────────────────┼─────────────────┤
│ backup_schedule          │ "0 */6 * * *"        │ 2026-03-10 ...  │
│ active_viewers_count     │ "0"                  │ 2026-03-10 ...  │
│ active_viewers_deadline  │ "2026-03-10T06:00:00"│ 2026-03-10 ...  │
│ last_backup_started_at   │ "2026-03-10T05:30:00"│ 2026-03-10 ...  │
│ last_backup_completed_at │ "2026-03-10T05:35:00"│ 2026-03-10 ...  │
│ backup_status            │ "idle"               │ 2026-03-10 ...  │
│ master_password_hash     │ "abc123..."          │ 2026-03-10 ...  │
│ master_password_salt     │ "def456..."          │ 2026-03-10 ...  │
└──────────────────────────┴──────────────────────┴─────────────────┘
```

---

## Implementation Steps

### 1. AppSettings Model

Add to `src/db/models.py`:

```python
class AppSettings(Base):
    """Key-value settings shared between backup and viewer containers."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        server_default=func.now(),
    )
```

### 2. Adapter Methods

Add to `src/db/adapter.py`:

```python
# --- App Settings (v7.3.0) ---

@retry_on_locked()
async def get_setting(self, key: str) -> str | None:
    async with self.db_manager.get_session() as session:
        result = await session.execute(
            select(AppSettings).where(AppSettings.key == key)
        )
        row = result.scalar_one_or_none()
        return row.value if row else None

@retry_on_locked()
async def set_setting(self, key: str, value: str):
    async with self.db_manager.get_session() as session:
        existing = await session.execute(
            select(AppSettings).where(AppSettings.key == key)
        )
        row = existing.scalar_one_or_none()
        if row:
            row.value = value
            row.updated_at = datetime.utcnow()
        else:
            session.add(AppSettings(key=key, value=value))
        await session.commit()

@retry_on_locked()
async def get_all_settings(self) -> dict[str, str]:
    async with self.db_manager.get_session() as session:
        result = await session.execute(select(AppSettings))
        return {row.key: row.value for row in result.scalars().all()}
```

### 3. Alembic Migration (011)

Create `alembic/versions/20260310_011_add_app_settings.py`:

```python
revision: str = "011"
down_revision: str | None = "010"

def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "app_settings" not in inspector.get_table_names():
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(255), primary_key=True),
            sa.Column("value", sa.Text, nullable=False),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        )

def downgrade():
    op.drop_table("app_settings")
```

### 4. Admin Settings API Endpoints

Add to `src/web/main.py`:

```python
@app.get("/api/admin/settings")
async def get_admin_settings(request: Request, _=Depends(require_admin)):
    settings = await db.get_all_settings()
    return {"settings": settings}

@app.put("/api/admin/settings")
async def update_admin_settings(request: Request, body: dict = Body(...), _=Depends(require_admin)):
    allowed_keys = {"backup_schedule"}  # Whitelist
    for key, value in body.items():
        if key in allowed_keys:
            if key == "backup_schedule":
                # Validate cron expression
                parts = value.split()
                if len(parts) != 5:
                    raise HTTPException(400, "Invalid cron expression (need 5 parts)")
            await db.set_setting(key, str(value))
    return {"success": True}

@app.get("/api/backup-status")
async def get_backup_status(request: Request, _=Depends(require_auth)):
    """Any authenticated user can see backup status."""
    status = await db.get_setting("backup_status") or "unknown"
    last_completed = await db.get_setting("last_backup_completed_at")
    last_started = await db.get_setting("last_backup_started_at")
    schedule = await db.get_setting("backup_schedule")
    return {
        "status": status,
        "last_completed_at": last_completed,
        "last_started_at": last_started,
        "schedule": schedule or config.schedule,
    }
```

### 5. Active User Heartbeat Endpoint

```python
@app.post("/api/activity/ping")
async def activity_ping(request: Request, _=Depends(require_auth)):
    """Mark a user as actively viewing. Extends active-viewer deadline by 2 min."""
    deadline = (datetime.utcnow() + timedelta(minutes=2)).isoformat()
    # Increment active count or update deadline
    await db.set_setting("active_viewers_deadline", deadline)
    current = await db.get_setting("active_viewers_count") or "0"
    await db.set_setting("active_viewers_count", str(max(1, int(current))))
    return {"ok": True}
```

### 6. Scheduler: Poll Settings & Reschedule

Modify `src/scheduler.py` — add settings polling loop:

```python
async def _poll_settings(self):
    """Check app_settings every 60s. Reschedule if backup_schedule changed."""
    current_schedule = self.config.schedule
    while True:
        await asyncio.sleep(60)
        try:
            db_schedule = await self.db.get_setting("backup_schedule")
            if db_schedule and db_schedule != current_schedule:
                logger.info(f"Schedule changed: {current_schedule} → {db_schedule}")
                parts = db_schedule.split()
                if len(parts) == 5:
                    trigger = CronTrigger(
                        minute=parts[0], hour=parts[1], day=parts[2],
                        month=parts[3], day_of_week=parts[4]
                    )
                    self.scheduler.reschedule_job("telegram_backup", trigger=trigger)
                    current_schedule = db_schedule

            # Check active viewers
            deadline = await self.db.get_setting("active_viewers_deadline")
            if deadline:
                deadline_dt = datetime.fromisoformat(deadline)
                if deadline_dt > datetime.utcnow():
                    # Active user — run every 5 min
                    if current_schedule != "*/5 * * * *":
                        trigger = CronTrigger(minute="*/5")
                        self.scheduler.reschedule_job("telegram_backup", trigger=trigger)
                        current_schedule = "*/5 * * * *"
                else:
                    # No active users — restore normal schedule
                    normal = db_schedule or self.config.schedule
                    if current_schedule == "*/5 * * * *":
                        parts = normal.split()
                        trigger = CronTrigger(
                            minute=parts[0], hour=parts[1], day=parts[2],
                            month=parts[3], day_of_week=parts[4]
                        )
                        self.scheduler.reschedule_job("telegram_backup", trigger=trigger)
                        current_schedule = normal

            # Update backup status markers
            await self.db.set_setting("backup_status", "idle")
        except Exception as e:
            logger.warning(f"Settings poll error: {e}")
```

Add status tracking in backup job:

```python
async def _run_backup_job(self):
    await self.db.set_setting("backup_status", "running")
    await self.db.set_setting("last_backup_started_at", datetime.utcnow().isoformat())
    try:
        await run_backup(self.config, client=self.client)
    finally:
        await self.db.set_setting("backup_status", "idle")
        await self.db.set_setting("last_backup_completed_at", datetime.utcnow().isoformat())
```

Start polling in `run_forever()`:

```python
asyncio.create_task(self._poll_settings())
```

### 7. Frontend: General Tab — Backup Control

```html
<!-- In the General settings tab -->
<div class="space-y-4">
  <h3 class="text-lg font-semibold text-tg-text">Backup Schedule</h3>

  <!-- Status indicator -->
  <div class="flex items-center gap-2">
    <span :class="backupStatus === 'running' ? 'bg-green-500' : 'bg-gray-500'"
      class="w-2 h-2 rounded-full"></span>
    <span class="text-tg-text text-sm">{{ backupStatus === 'running' ? 'Backup in progress...' : 'Idle' }}</span>
    <span v-if="lastBackupAt" class="text-tg-muted text-xs ml-auto">
      Last: {{ formatRelativeTime(lastBackupAt) }}
    </span>
  </div>

  <!-- Schedule picker (preset + custom) -->
  <div>
    <label class="text-tg-muted text-sm mb-1 block">Schedule</label>
    <select v-model="backupSchedulePreset"
      class="w-full bg-tg-input text-tg-text rounded-lg px-4 py-2 border border-gray-700">
      <option value="0 */1 * * *">Every hour</option>
      <option value="0 */3 * * *">Every 3 hours</option>
      <option value="0 */6 * * *">Every 6 hours</option>
      <option value="0 */12 * * *">Every 12 hours</option>
      <option value="0 0 * * *">Daily at midnight</option>
      <option value="custom">Custom cron...</option>
    </select>
    <input v-if="backupSchedulePreset === 'custom'" v-model="customCron"
      placeholder="e.g., 0 */4 * * *"
      class="w-full mt-2 bg-tg-input text-tg-text font-mono rounded-lg px-4 py-2 border border-gray-700">
    <button @click="saveBackupSchedule" :disabled="savingSchedule"
      class="mt-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition disabled:opacity-50 text-sm">
      Save Schedule
    </button>
  </div>

  <!-- Active user boost -->
  <div class="flex items-center justify-between">
    <div>
      <span class="text-tg-text text-sm">Active User Boost</span>
      <p class="text-tg-muted text-xs">Run backups every 5 min while someone is actively browsing</p>
    </div>
    <button @click="activeUserBoost = !activeUserBoost"
      :class="activeUserBoost ? 'bg-blue-600' : 'bg-gray-700'"
      class="relative inline-flex h-6 w-11 items-center rounded-full transition">
      <span :class="activeUserBoost ? 'translate-x-6' : 'translate-x-1'"
        class="inline-block h-4 w-4 rounded-full bg-white transition"></span>
    </button>
  </div>
</div>
```

### 8. Frontend: Activity heartbeat

```javascript
// Send ping every 90s while page is visible and boost is enabled
let activityInterval = null

function startActivityPing() {
  if (activityInterval) return
  activityInterval = setInterval(async () => {
    if (document.visibilityState === 'visible' && activeUserBoost.value) {
      await fetch('/api/activity/ping', { method: 'POST' }).catch(() => {})
    }
  }, 90000)  // Every 90s (deadline is 2 min)
}

// Start on mount if authenticated
onMounted(() => {
  // ... existing code
  startActivityPing()
})
```

---

## Todo

- [ ] Add `AppSettings` model to `models.py`
- [ ] Add adapter methods: `get_setting`, `set_setting`, `get_all_settings`
- [ ] Create Alembic migration 011
- [ ] Add `GET/PUT /api/admin/settings` endpoints
- [ ] Add `GET /api/backup-status` endpoint
- [ ] Add `POST /api/activity/ping` endpoint
- [ ] Modify `scheduler.py`: add `_poll_settings()` coroutine
- [ ] Modify `scheduler.py`: add backup status tracking
- [ ] Build General tab UI (schedule picker + status + active boost toggle)
- [ ] Implement activity heartbeat in frontend
- [ ] Test: change schedule from UI → backup container picks up new schedule within 60s
- [ ] Test: active user boost → backups run every 5 min
- [ ] Test: user closes tab → boost stops after 2 min deadline expires
- [ ] Test: backup status shows correctly in UI
- [ ] Build and redeploy BOTH containers (backup + viewer)

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Settings poll adds DB load | Low | Single SELECT every 60s, negligible |
| Invalid cron crashes scheduler | High | Validate in API endpoint, catch exception in poll |
| Race condition on active_viewers | Low | Single writer (viewer), single reader (backup) |
| Backup container doesn't have adapter | Medium | Verify scheduler.py already imports db adapter |

---

## Success Criteria

- Admin can change backup schedule from UI
- Backup container picks up new schedule within 60s
- Backup status (idle/running) visible in UI
- Last backup timestamp shown
- Active user boost triggers 5-min intervals
- Boost stops when no active users for 2 minutes
- Both containers must be rebuilt and deployed
