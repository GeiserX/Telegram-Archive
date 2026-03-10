# Phase 4: Timezone Auto-detect & Display Settings

**Priority:** MEDIUM
**Status:** TODO
**Effort:** Small
**Files:** `src/web/templates/index.html`, `src/web/main.py`

---

## Overview

Auto-detect user's timezone from browser and apply it to message timestamps. Currently the viewer uses server-configured `VIEWER_TIMEZONE` env var (set to `Asia/Manila`). Browser knows the user's actual timezone — use it.

---

## Key Insights

- `Intl.DateTimeFormat().resolvedOptions().timeZone` gives browser timezone (e.g., `Asia/Manila`)
- Server sends timestamps in UTC (SQLite) or with timezone (PostgreSQL)
- Frontend already has `viewerTimezone` ref (line 1496) — used for display formatting
- Current timezone comes from `/api/config` or similar endpoint that exposes `config.viewer_timezone`
- Change: use browser timezone by default, allow manual override, store in localStorage
- Server timezone still used for: stats calculation schedule, backup scheduling

---

## Implementation Steps

### 1. Auto-detect timezone on app load

In `onMounted`, before or after auth check:

```javascript
// Auto-detect timezone
const detectedTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone
const savedTimezone = localStorage.getItem('tg-timezone')
viewerTimezone.value = savedTimezone || detectedTimezone || 'UTC'
```

### 2. General tab in settings — timezone picker

Add to the General tab (or Appearance tab):

```html
<div v-if="settingsTab === 'general'" class="space-y-6">
  <!-- Timezone -->
  <div>
    <h3 class="text-lg font-semibold text-tg-text mb-2">Timezone</h3>
    <p class="text-tg-muted text-sm mb-3">
      Detected: {{ detectedTimezone }}
    </p>
    <div class="flex gap-2">
      <select v-model="selectedTimezone"
        class="flex-1 bg-tg-input text-tg-text rounded-lg px-4 py-2 border border-gray-700 focus:border-blue-500 focus:outline-none text-sm">
        <option v-for="tz in commonTimezones" :key="tz" :value="tz">{{ tz }}</option>
      </select>
      <button @click="resetTimezone"
        class="px-3 py-2 bg-gray-700 hover:bg-gray-600 text-tg-text text-sm rounded-lg transition">
        Auto
      </button>
    </div>
    <p class="text-tg-muted text-xs mt-1">Used for displaying message timestamps.</p>
  </div>

  <!-- Date/time format -->
  <div>
    <h3 class="text-lg font-semibold text-tg-text mb-2">Time Format</h3>
    <div class="flex gap-3">
      <label class="flex items-center gap-2 cursor-pointer">
        <input type="radio" v-model="timeFormat" value="12h"
          class="rounded-full border-gray-600 bg-tg-input">
        <span class="text-tg-text text-sm">12-hour (3:30 PM)</span>
      </label>
      <label class="flex items-center gap-2 cursor-pointer">
        <input type="radio" v-model="timeFormat" value="24h"
          class="rounded-full border-gray-600 bg-tg-input">
        <span class="text-tg-text text-sm">24-hour (15:30)</span>
      </label>
    </div>
  </div>

  <!-- Message density (bonus) -->
  <div>
    <h3 class="text-lg font-semibold text-tg-text mb-2">Message Density</h3>
    <div class="flex gap-3">
      <button v-for="d in ['compact', 'normal', 'comfortable']" :key="d"
        @click="messageDensity = d; localStorage.setItem('tg-density', d)"
        :class="messageDensity === d ? 'bg-tg-active text-white' : 'bg-tg-hover text-tg-muted'"
        class="px-4 py-2 rounded-lg text-sm capitalize transition">
        {{ d }}
      </button>
    </div>
  </div>
</div>
```

### 3. Vue state for timezone

```javascript
const detectedTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone
const selectedTimezone = ref(localStorage.getItem('tg-timezone') || detectedTimezone)
const timeFormat = ref(localStorage.getItem('tg-timeformat') || '24h')
const messageDensity = ref(localStorage.getItem('tg-density') || 'normal')

// Common timezones list
const commonTimezones = [
  'UTC', 'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'America/Sao_Paulo', 'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Madrid',
  'Europe/Moscow', 'Asia/Dubai', 'Asia/Kolkata', 'Asia/Bangkok', 'Asia/Shanghai',
  'Asia/Tokyo', 'Asia/Seoul', 'Asia/Manila', 'Asia/Singapore',
  'Australia/Sydney', 'Pacific/Auckland',
]
// Add detected timezone if not in list
if (detectedTimezone && !commonTimezones.includes(detectedTimezone)) {
  commonTimezones.unshift(detectedTimezone)
}

watch(selectedTimezone, (tz) => {
  localStorage.setItem('tg-timezone', tz)
  viewerTimezone.value = tz
})

watch(timeFormat, (fmt) => {
  localStorage.setItem('tg-timeformat', fmt)
})

function resetTimezone() {
  selectedTimezone.value = detectedTimezone
  localStorage.removeItem('tg-timezone')
}
```

### 4. Update timestamp formatting

Find existing timestamp formatting functions and update to use `viewerTimezone` and `timeFormat`:

```javascript
function formatMessageTime(isoString) {
  const date = new Date(isoString)
  return date.toLocaleTimeString([], {
    timeZone: viewerTimezone.value,
    hour: '2-digit',
    minute: '2-digit',
    hour12: timeFormat.value === '12h',
  })
}
```

### 5. Optional: Report timezone to server

If the admin wants server-side timezone awareness (e.g., for stats calculation), add to the `/api/auth/check` response or a new endpoint:

```python
# In /api/auth/check or a new /api/preferences
# Frontend sends: { "timezone": "Asia/Manila" }
# Server stores per-session: request.state.user["timezone"] = tz
```

Not critical for MVP — server timezone for stats is fine as env var.

---

## Todo

- [ ] Auto-detect browser timezone in onMounted
- [ ] Build timezone picker in General tab
- [ ] Add time format toggle (12h/24h)
- [ ] Add message density options (compact/normal/comfortable)
- [ ] Store preferences in localStorage
- [ ] Update timestamp formatting to respect timezone and format
- [ ] Test with different browser timezones
- [ ] Test "Auto" reset button

---

## Success Criteria

- Timezone auto-detected from browser on first visit
- User can manually override timezone
- 12h/24h time format toggle works
- Preferences persist across page reloads
- Message timestamps update immediately on change
- "Auto" button resets to detected timezone
