# Phase 3: Admin Settings UI

## Context Links
- [Research: Vue 3 CDN Admin UI Patterns](research/researcher-02-vue3-admin-settings-ui.md)
- [Frontend: src/web/templates/index.html](../../src/web/templates/index.html)
- [Phase 2: API Endpoints](phase-02-api-endpoints-and-chat-filtering.md)

## Overview
- **Priority:** P1 (depends on Phase 2)
- **Status:** complete
- **Effort:** 2.5h

Add admin-only settings panel accessible via cog icon in the sidebar header. Panel includes viewer account CRUD table with multi-select chat picker. Frontend tracks user role from auth/check response.

## Key Insights
- Frontend is a single Vue 3 SPA (`index.html`) using CDN imports (no build step). All state managed via Composition API refs.
- Login response now returns `role` and `username` (Phase 1). Auth check returns same.
- Sidebar header is at line ~430 with "Telegram Archive" title and stats dropdown.
- All available chats for the picker come from `GET /api/admin/chats` (Phase 2).
- Tailwind CSS via CDN handles all styling.

## Requirements

### Functional
- F1: Cog/gear icon visible in sidebar header ONLY for master users
- F2: Clicking cog opens settings panel (replaces chat list area or slides over)
- F3: Settings panel shows viewer accounts table: username | allowed chats count | actions
- F4: "Add User" form: username, password, multi-select chat picker
- F5: Edit user: modal/inline with optional password change + chat picker
- F6: Delete user with confirmation dialog
- F7: Logout button visible for all authenticated users
- F8: Show current user info (username, role badge) in header
- F9: "Activity Log" tab in settings panel showing audit entries per viewer (endpoint, chat_id, timestamp, IP)
<!-- Updated: Validation Session 1 - Added F9 audit log tab per full audit log decision -->

### Non-Functional
- NF1: No build step — all Vue 3 + Tailwind via CDN
- NF2: Mobile responsive (table scrolls horizontally)
- NF3: Chat picker loads all chats on settings panel open (single fetch)

## Architecture

### State Variables (new refs)

```javascript
// User identity
const userRole = ref(null)       // 'master' | 'viewer' | null
const userName = ref('')

// Settings panel
const showSettings = ref(false)
const settingsTab = ref('viewers')  // 'viewers' | 'activity'
const viewerAccounts = ref([])
const allChats = ref([])         // For chat picker (admin only)
const loadingViewers = ref(false)

// Add/Edit form
const showViewerForm = ref(false)
const editingViewer = ref(null)  // null = adding new, object = editing
const viewerForm = ref({ username: '', password: '', allowed_chat_ids: [] })
const viewerFormError = ref('')

// Chat picker search
const chatPickerSearch = ref('')

// Delete confirmation
const deletingViewer = ref(null)

// Activity Log (audit)
const auditLogs = ref([])
const auditLoading = ref(false)
const auditFilterViewer = ref(null)  // viewer_id or null for all
```
<!-- Updated: Validation Session 1 - Added audit log state variables -->

### UI Layout

```
Sidebar Header
  [Telegram Archive] [Stats] [Cog icon (master only)]

Settings Panel (v-if="showSettings") — replaces chat list
  [Back arrow] Settings
  [Viewers] [Activity Log]  ← tab switcher
  ─────────────────────

  Tab: Viewers
  [+ Add User] button
  ┌──────────────────────────────────────────────┐
  │ Username  │ Chats      │ Status │ Actions    │
  ├───────────┼────────────┼────────┼────────────┤
  │ john      │ 3 of 12    │ Active │ Edit  Del  │
  │ jane      │ 5 of 12    │ Active │ Edit  Del  │
  └──────────────────────────────────────────────┘

  Tab: Activity Log
  Filter: [All viewers ▾]
  ┌──────────────────────────────────────────────┐
  │ Time       │ User  │ Endpoint     │ Chat     │
  ├────────────┼───────┼──────────────┼──────────┤
  │ 14:32:05   │ john  │ /api/chats   │ -        │
  │ 14:31:22   │ john  │ /api/msgs    │ Chat A   │
  │ 14:30:01   │ jane  │ /api/chats   │ -        │
  └──────────────────────────────────────────────┘

Add/Edit User Modal (v-if="showViewerForm")
  Username: [__________]
  Password: [__________] (optional on edit)
  Allowed Chats:
    Search: [________]
    ☑ Chat Alpha
    ☑ Chat Beta
    ☐ Chat Gamma
    3 / 12 selected
  [Save] [Cancel]
```

## Related Code Files

| File | Action | Changes |
|------|--------|---------|
| `src/web/templates/index.html` | MODIFY | Add settings panel HTML, Vue state, API calls |

## Implementation Steps

### Step 1: Track User Role from Auth Response

Update `onMounted` auth check (~line 1993) and `performLogin` (~line 2477):

**In onMounted (after line 1996):**
```javascript
authRequired.value = !!data.auth_required
isAuthenticated.value = !!data.authenticated
userRole.value = data.role || null
userName.value = data.username || ''
```

**In performLogin (after line 2477):**
```javascript
if (data.success) {
    isAuthenticated.value = true
    userRole.value = data.role || 'master'
    userName.value = data.username || ''
    // ... existing loadChats, loadStats, etc.
}
```

### Step 2: Add State Variables

Add after `loginError` ref (~line 1458):

```javascript
// User identity
const userRole = ref(null)
const userName = ref('')

// Settings panel
const showSettings = ref(false)
const viewerAccounts = ref([])
const allChats = ref([])
const loadingViewers = ref(false)

// Viewer form
const showViewerForm = ref(false)
const editingViewer = ref(null)
const viewerForm = ref({ username: '', password: '', allowed_chat_ids: [] })
const viewerFormError = ref('')
const chatPickerSearch = ref('')
const deletingViewer = ref(null)
const savingViewer = ref(false)
```

### Step 3: Add API Functions

Add in the setup() methods section:

```javascript
// ── Admin Settings API ──

const loadViewerAccounts = async () => {
    if (userRole.value !== 'master') return
    loadingViewers.value = true
    try {
        const res = await fetch('/api/admin/viewers', { credentials: 'include' })
        if (res.ok) {
            const data = await res.json()
            viewerAccounts.value = data.viewers || []
        }
    } catch (e) {
        console.error('Failed to load viewers:', e)
    } finally {
        loadingViewers.value = false
    }
}

const loadAllChatsAdmin = async () => {
    if (userRole.value !== 'master') return
    try {
        const res = await fetch('/api/admin/chats', { credentials: 'include' })
        if (res.ok) {
            const data = await res.json()
            allChats.value = data.chats || []
        }
    } catch (e) {
        console.error('Failed to load admin chats:', e)
    }
}

const openSettings = async () => {
    showSettings.value = true
    await loadViewerAccounts()
    await loadAllChatsAdmin()
}

const closeSettings = () => {
    showSettings.value = false
    showViewerForm.value = false
    editingViewer.value = null
    deletingViewer.value = null
}

const openAddViewer = () => {
    editingViewer.value = null
    viewerForm.value = { username: '', password: '', allowed_chat_ids: [] }
    viewerFormError.value = ''
    chatPickerSearch.value = ''
    showViewerForm.value = true
}

const openEditViewer = (viewer) => {
    editingViewer.value = viewer
    viewerForm.value = {
        username: viewer.username,
        password: '',
        allowed_chat_ids: [...(viewer.allowed_chat_ids || [])],
    }
    viewerFormError.value = ''
    chatPickerSearch.value = ''
    showViewerForm.value = true
}

const saveViewer = async () => {
    viewerFormError.value = ''
    savingViewer.value = true
    try {
        const isEdit = !!editingViewer.value
        const url = isEdit
            ? `/api/admin/viewers/${editingViewer.value.id}`
            : '/api/admin/viewers'
        const method = isEdit ? 'PUT' : 'POST'

        const body = {
            allowed_chat_ids: viewerForm.value.allowed_chat_ids,
        }
        if (!isEdit) {
            body.username = viewerForm.value.username
        }
        if (viewerForm.value.password) {
            body.password = viewerForm.value.password
        }
        if (!isEdit && !body.password) {
            viewerFormError.value = 'Password is required for new users'
            return
        }

        const res = await fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify(body),
        })

        if (!res.ok) {
            const err = await res.json().catch(() => ({}))
            viewerFormError.value = err.detail || 'Failed to save'
            return
        }

        showViewerForm.value = false
        await loadViewerAccounts()
    } catch (e) {
        viewerFormError.value = 'Unexpected error'
    } finally {
        savingViewer.value = false
    }
}

const confirmDeleteViewer = (viewer) => {
    deletingViewer.value = viewer
}

const deleteViewer = async () => {
    if (!deletingViewer.value) return
    try {
        const res = await fetch(`/api/admin/viewers/${deletingViewer.value.id}`, {
            method: 'DELETE',
            credentials: 'include',
        })
        if (res.ok) {
            deletingViewer.value = null
            await loadViewerAccounts()
        }
    } catch (e) {
        console.error('Delete failed:', e)
    }
}

const toggleChat = (chatId) => {
    const idx = viewerForm.value.allowed_chat_ids.indexOf(chatId)
    if (idx >= 0) {
        viewerForm.value.allowed_chat_ids.splice(idx, 1)
    } else {
        viewerForm.value.allowed_chat_ids.push(chatId)
    }
}

const filteredPickerChats = computed(() => {
    const q = chatPickerSearch.value.toLowerCase()
    if (!q) return allChats.value
    return allChats.value.filter(c =>
        (c.title || '').toLowerCase().includes(q) ||
        String(c.id).includes(q)
    )
})

const performLogout = async () => {
    try {
        await fetch('/api/logout', { method: 'POST', credentials: 'include' })
    } catch (e) { /* ignore */ }
    isAuthenticated.value = false
    userRole.value = null
    userName.value = ''
    showSettings.value = false
}

// ── Audit Log API ──
const loadAuditLogs = async () => {
    auditLoading.value = true
    try {
        let url = '/api/admin/audit?limit=100'
        if (auditFilterViewer.value) url += `&viewer_id=${auditFilterViewer.value}`
        const res = await fetch(url, { credentials: 'include' })
        if (res.ok) {
            const data = await res.json()
            auditLogs.value = data.logs || []
        }
    } catch (e) {
        console.error('Failed to load audit logs:', e)
    } finally {
        auditLoading.value = false
    }
}

const switchSettingsTab = async (tab) => {
    settingsTab.value = tab
    if (tab === 'activity') await loadAuditLogs()
}
```
<!-- Updated: Validation Session 1 - Added audit log API functions -->

### Step 4: Add Cog Icon in Sidebar Header

In the sidebar header div (~line 431), add cog icon after the stats dropdown:

```html
<!-- Settings (admin only) -->
<button v-if="userRole === 'master'"
    @click="openSettings"
    class="p-1.5 text-tg-muted hover:text-white hover:bg-gray-700/50 rounded transition"
    title="Settings">
    <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
        <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
</button>

<!-- Logout button -->
<button v-if="isAuthenticated && authRequired"
    @click="performLogout"
    class="p-1.5 text-tg-muted hover:text-red-400 hover:bg-gray-700/50 rounded transition"
    title="Logout">
    <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
    </svg>
</button>
```

### Step 5: Add Settings Panel HTML

Add BEFORE the chat list content in the sidebar, using `v-if="showSettings"` to conditionally replace the chat list:

```html
<!-- Settings Panel (replaces chat list for admin) -->
<div v-if="showSettings" class="flex-1 flex flex-col overflow-hidden">
    <!-- Settings Header -->
    <div class="p-4 border-b border-gray-700 flex items-center gap-3">
        <button @click="closeSettings" class="text-tg-muted hover:text-white transition">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7" />
            </svg>
        </button>
        <h2 class="text-lg font-semibold">Settings</h2>
    </div>

    <div class="flex-1 overflow-y-auto p-4 space-y-4">
        <!-- User Info -->
        <div class="bg-gray-800/50 rounded-lg p-3 flex items-center justify-between">
            <div>
                <span class="text-sm text-tg-muted">Logged in as</span>
                <p class="font-medium">{{ userName }}
                    <span class="text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 ml-1">admin</span>
                </p>
            </div>
        </div>

        <!-- Tab Switcher -->
        <div class="flex border-b border-gray-700">
            <button @click="switchSettingsTab('viewers')"
                :class="settingsTab === 'viewers' ? 'text-blue-400 border-blue-400' : 'text-tg-muted border-transparent hover:text-white'"
                class="px-4 py-2 text-sm font-medium border-b-2 transition">
                Viewers
            </button>
            <button @click="switchSettingsTab('activity')"
                :class="settingsTab === 'activity' ? 'text-blue-400 border-blue-400' : 'text-tg-muted border-transparent hover:text-white'"
                class="px-4 py-2 text-sm font-medium border-b-2 transition">
                Activity Log
            </button>
        </div>
        <!-- Updated: Validation Session 1 - Added tab switcher for viewers/activity -->

        <!-- Viewer Accounts Section (tab: viewers) -->
        <div v-if="settingsTab === 'viewers'">
            <div class="flex items-center justify-between mb-3">
                <h3 class="text-sm font-semibold text-tg-muted uppercase tracking-wider">Viewer Accounts</h3>
                <button @click="openAddViewer"
                    class="text-xs bg-blue-600 hover:bg-blue-500 text-white px-3 py-1.5 rounded-lg transition">
                    + Add User
                </button>
            </div>

            <!-- Loading -->
            <div v-if="loadingViewers" class="text-center text-tg-muted py-4">Loading...</div>

            <!-- Empty state -->
            <div v-else-if="viewerAccounts.length === 0" class="text-center text-tg-muted py-6 bg-gray-800/30 rounded-lg">
                <p class="text-sm">No viewer accounts yet</p>
                <p class="text-xs mt-1">Create accounts to share access to specific chats</p>
            </div>

            <!-- Accounts table -->
            <div v-else class="overflow-x-auto">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="text-tg-muted text-xs uppercase border-b border-gray-700">
                            <th class="text-left py-2 px-2">Username</th>
                            <th class="text-left py-2 px-2">Chats</th>
                            <th class="text-right py-2 px-2">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-for="viewer in viewerAccounts" :key="viewer.id"
                            class="border-b border-gray-700/50 hover:bg-gray-800/30">
                            <td class="py-2 px-2">
                                {{ viewer.username }}
                                <span v-if="!viewer.is_active" class="text-xs text-red-400 ml-1">(disabled)</span>
                            </td>
                            <td class="py-2 px-2 text-tg-muted">
                                {{ (viewer.allowed_chat_ids || []).length }} chats
                            </td>
                            <td class="py-2 px-2 text-right space-x-2">
                                <button @click="openEditViewer(viewer)"
                                    class="text-blue-400 hover:text-blue-300 text-xs">Edit</button>
                                <button @click="confirmDeleteViewer(viewer)"
                                    class="text-red-400 hover:text-red-300 text-xs">Delete</button>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Activity Log Section (tab: activity) -->
        <div v-if="settingsTab === 'activity'">
            <div class="flex items-center justify-between mb-3">
                <h3 class="text-sm font-semibold text-tg-muted uppercase tracking-wider">Activity Log</h3>
                <select v-model="auditFilterViewer" @change="loadAuditLogs"
                    class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1 text-tg-muted">
                    <option :value="null">All viewers</option>
                    <option v-for="v in viewerAccounts" :key="v.id" :value="v.id">{{ v.username }}</option>
                </select>
            </div>
            <div v-if="auditLoading" class="text-center text-tg-muted py-4">Loading...</div>
            <div v-else-if="auditLogs.length === 0" class="text-center text-tg-muted py-6 bg-gray-800/30 rounded-lg">
                <p class="text-sm">No activity recorded yet</p>
            </div>
            <div v-else class="overflow-x-auto">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="text-tg-muted text-xs uppercase border-b border-gray-700">
                            <th class="text-left py-2 px-2">Time</th>
                            <th class="text-left py-2 px-2">User</th>
                            <th class="text-left py-2 px-2">Endpoint</th>
                            <th class="text-left py-2 px-2">Chat</th>
                            <th class="text-left py-2 px-2">IP</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-for="log in auditLogs" :key="log.id"
                            class="border-b border-gray-700/50 hover:bg-gray-800/30">
                            <td class="py-2 px-2 text-xs text-tg-muted whitespace-nowrap">{{ new Date(log.timestamp).toLocaleString() }}</td>
                            <td class="py-2 px-2">{{ log.username }}</td>
                            <td class="py-2 px-2 text-xs font-mono text-tg-muted">{{ log.endpoint }}</td>
                            <td class="py-2 px-2 text-xs text-tg-muted">{{ log.chat_id || '-' }}</td>
                            <td class="py-2 px-2 text-xs text-tg-muted">{{ log.ip_address }}</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
        <!-- Updated: Validation Session 1 - Added activity log tab UI -->
    </div>

    <!-- Add/Edit Viewer Modal Overlay -->
    <div v-if="showViewerForm" class="absolute inset-0 bg-black/60 z-50 flex items-center justify-center p-4">
        <div class="bg-tg-sidebar border border-gray-700 rounded-xl w-full max-w-md max-h-[90vh] overflow-y-auto p-5 shadow-2xl">
            <h3 class="text-lg font-semibold mb-4">
                {{ editingViewer ? 'Edit Viewer' : 'Add Viewer' }}
            </h3>

            <div class="space-y-4">
                <!-- Username -->
                <div>
                    <label class="block text-sm text-tg-muted mb-1">Username</label>
                    <input v-model="viewerForm.username" type="text"
                        :disabled="!!editingViewer"
                        :class="{'opacity-50': !!editingViewer}"
                        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                        placeholder="viewer_username">
                </div>

                <!-- Password -->
                <div>
                    <label class="block text-sm text-tg-muted mb-1">
                        Password <span v-if="editingViewer" class="text-xs">(leave blank to keep current)</span>
                    </label>
                    <input v-model="viewerForm.password" type="password"
                        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                        :placeholder="editingViewer ? 'Leave blank to keep current' : 'Enter password'">
                </div>

                <!-- Chat Picker -->
                <div>
                    <label class="block text-sm text-tg-muted mb-1">
                        Allowed Chats
                        <span class="text-xs ml-1">({{ viewerForm.allowed_chat_ids.length }} / {{ allChats.length }} selected)</span>
                    </label>
                    <input v-model="chatPickerSearch" type="text"
                        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm mb-2 focus:outline-none focus:border-blue-500"
                        placeholder="Search chats...">
                    <div class="max-h-48 overflow-y-auto border border-gray-700 rounded-lg">
                        <label v-for="chat in filteredPickerChats" :key="chat.id"
                            class="flex items-center px-3 py-2 hover:bg-gray-800/50 cursor-pointer text-sm border-b border-gray-700/30 last:border-0">
                            <input type="checkbox"
                                :checked="viewerForm.allowed_chat_ids.includes(chat.id)"
                                @change="toggleChat(chat.id)"
                                class="mr-2 rounded">
                            <span class="truncate">{{ chat.title }}</span>
                            <span class="text-xs text-tg-muted ml-auto pl-2 whitespace-nowrap">{{ chat.type }}</span>
                        </label>
                        <div v-if="filteredPickerChats.length === 0" class="text-center text-tg-muted py-3 text-xs">
                            No chats found
                        </div>
                    </div>
                </div>

                <!-- Error -->
                <p v-if="viewerFormError" class="text-sm text-red-400 bg-red-500/10 rounded-lg p-2 text-center">
                    {{ viewerFormError }}
                </p>

                <!-- Actions -->
                <div class="flex gap-2 justify-end pt-2">
                    <button @click="showViewerForm = false"
                        class="px-4 py-2 text-sm text-tg-muted hover:text-white bg-gray-800 rounded-lg transition">
                        Cancel
                    </button>
                    <button @click="saveViewer" :disabled="savingViewer"
                        class="px-4 py-2 text-sm bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded-lg transition">
                        {{ savingViewer ? 'Saving...' : 'Save' }}
                    </button>
                </div>
            </div>
        </div>
    </div>

    <!-- Delete Confirmation Modal -->
    <div v-if="deletingViewer" class="absolute inset-0 bg-black/60 z-50 flex items-center justify-center p-4">
        <div class="bg-tg-sidebar border border-gray-700 rounded-xl w-full max-w-sm p-5 shadow-2xl">
            <h3 class="text-lg font-semibold mb-2">Delete Viewer</h3>
            <p class="text-sm text-tg-muted mb-4">
                Are you sure you want to delete <strong class="text-white">{{ deletingViewer.username }}</strong>?
                This will immediately revoke their access.
            </p>
            <div class="flex gap-2 justify-end">
                <button @click="deletingViewer = null"
                    class="px-4 py-2 text-sm text-tg-muted hover:text-white bg-gray-800 rounded-lg transition">
                    Cancel
                </button>
                <button @click="deleteViewer"
                    class="px-4 py-2 text-sm bg-red-600 hover:bg-red-500 text-white rounded-lg transition">
                    Delete
                </button>
            </div>
        </div>
    </div>
</div>
```

### Step 6: Conditionally Hide Chat List When Settings Open

Wrap the existing chat list (the `<div>` containing search input and chat items) with `v-if="!showSettings"`:

The sidebar currently has structure like:
```html
<div class="bg-tg-sidebar flex flex-col ...">
    <!-- Header (line ~430) -->
    <div class="p-4 border-b ..."> ... </div>
    <!-- Search + chat list -->
    <div class="..."> ... search input ... </div>
    <div class="flex-1 overflow-y-auto"> ... chat items ... </div>
</div>
```

Add `v-if="!showSettings"` to the search section and chat list section. The settings panel (Step 5) will show in their place.

### Step 7: Register Return Values

Add to the `return` block (~line 3708):

```javascript
// Settings / Admin
userRole,
userName,
showSettings,
settingsTab,
viewerAccounts,
allChats,
loadingViewers,
showViewerForm,
editingViewer,
viewerForm,
viewerFormError,
chatPickerSearch,
deletingViewer,
savingViewer,
openSettings,
closeSettings,
openAddViewer,
openEditViewer,
saveViewer,
confirmDeleteViewer,
deleteViewer,
toggleChat,
filteredPickerChats,
performLogout,
// Audit Log
auditLogs,
auditLoading,
auditFilterViewer,
loadAuditLogs,
switchSettingsTab,
```

## Todo List

- [x] Add `userRole`, `userName` refs and populate from auth check + login responses
- [x] Add all settings-related state refs (showSettings, viewerAccounts, etc.)
- [x] Add audit log state refs (auditLogs, auditLoading, auditFilterViewer, settingsTab)
- [x] Add API functions: loadViewerAccounts, loadAllChatsAdmin, saveViewer, deleteViewer
- [x] Add audit log API functions: loadAuditLogs, switchSettingsTab
- [x] Add cog icon button in sidebar header (master only)
- [x] Add logout button in sidebar header
- [x] Add settings panel HTML with tab switcher (Viewers / Activity Log)
- [x] Add viewer accounts table in Viewers tab
- [x] Add activity log table in Activity Log tab with viewer filter dropdown
- [x] Add add/edit viewer modal with chat picker
- [x] Add delete confirmation modal
- [x] Hide chat list when settings panel is open
- [x] Register all new refs and functions in template return block
- [x] Add `filteredPickerChats` computed

## Success Criteria
- Cog icon appears only for master user
- Settings panel opens/closes, shows viewer accounts list
- Add user form creates account with selected chats
- Edit user form allows password change and chat modification
- Delete confirmation dialog works, account removed
- Chat picker search filters chats correctly
- Logout button clears session and shows login screen
- All UI is responsive on mobile

## Risk Assessment
- **Large index.html file**: Already 3700+ lines. Adding ~200 lines of HTML + ~150 lines of JS. Consider extracting settings into a separate `<script>` tag or composable later if it gets unwieldy.
- **Modal z-index conflicts**: Settings panel is inside sidebar. Use `absolute inset-0` on the sidebar container + `z-50` for modals.
- **Chat picker performance**: Loading all chats is fine for typical deployments (<1000 chats). If thousands, add virtual scrolling later.

## Security Considerations
- Cog icon hidden via `v-if` is cosmetic only; real security is backend `require_admin`
- Password field uses `type="password"` — never displayed in UI
- Admin API calls use `credentials: 'include'` for cookie auth

## Next Steps
- Phase 4: Testing and migration verification
