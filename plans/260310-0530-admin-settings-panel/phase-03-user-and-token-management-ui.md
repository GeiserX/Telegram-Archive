# Phase 3: User & Token Management UI

**Priority:** HIGH
**Status:** TODO
**Effort:** Large (most frontend work)
**Files:** `src/web/templates/index.html`

---

## Overview

Build admin UI for managing viewer accounts and share tokens. All backend APIs already exist (`/api/admin/viewers` CRUD, `/api/admin/tokens` CRUD, `/api/admin/chats` for picker). This phase is frontend-only.

---

## Key Insights

- **Existing APIs (no backend changes needed):**
  - `GET /api/admin/viewers` — list all viewer accounts
  - `POST /api/admin/viewers` — create viewer (username, password, allowed_chat_ids)
  - `PUT /api/admin/viewers/{id}` — update (password, allowed_chat_ids, is_active)
  - `DELETE /api/admin/viewers/{id}` — delete
  - `GET /api/admin/chats` — list ALL chats for scope picker
  - `POST /api/admin/tokens` — create token (chat_ids, label, expires_hours)
  - `GET /api/admin/tokens` — list all tokens
  - `DELETE /api/admin/tokens/{id}` — revoke
- Chat picker needed: searchable multi-select of all available chats
- Token creation shows plaintext ONCE — must display copy-to-clipboard
- Share URL format: `https://{host}/?token={plaintext}`

---

## Implementation Steps

### 1. Reusable Chat Picker Component

In-template component pattern (no separate file):

```html
<!-- Inline chat picker: multi-select with search -->
<div class="space-y-2">
  <div class="relative">
    <input v-model="chatSearch" type="text" placeholder="Search chats..."
      class="w-full bg-tg-input text-tg-text rounded-lg px-4 py-2 border border-gray-700 focus:border-blue-500 focus:outline-none text-sm">
  </div>
  <!-- Selected chips -->
  <div v-if="selectedChats.length" class="flex flex-wrap gap-1.5">
    <span v-for="chat in selectedChats" :key="chat.id"
      class="inline-flex items-center gap-1 bg-blue-600/30 text-blue-300 text-xs px-2 py-1 rounded-full">
      {{ chat.title }}
      <button @click="removeChat(chat.id)" class="hover:text-white">&times;</button>
    </span>
  </div>
  <!-- Chat list (filterable) -->
  <div class="max-h-40 overflow-y-auto space-y-1 bg-tg-bg rounded-lg p-2 border border-gray-700">
    <label v-for="chat in filteredAvailableChats" :key="chat.id"
      class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-tg-hover cursor-pointer text-sm">
      <input type="checkbox" :value="chat.id" v-model="selectedChatIds"
        class="rounded border-gray-600 bg-tg-input">
      <span class="text-tg-text truncate">{{ chat.title }}</span>
      <span class="text-tg-muted text-xs ml-auto">{{ chat.id }}</span>
    </label>
    <div v-if="!filteredAvailableChats.length" class="text-tg-muted text-sm px-2 py-1">
      No chats found
    </div>
  </div>
  <p class="text-tg-muted text-xs">{{ selectedChatIds.length }} chat(s) selected. Empty = access to all chats.</p>
</div>
```

### 2. Users Tab

```html
<div v-if="settingsTab === 'users'" class="space-y-6">
  <!-- Create user form (collapsible) -->
  <div class="bg-tg-bg rounded-xl border border-gray-700">
    <button @click="showCreateUser = !showCreateUser"
      class="w-full flex items-center justify-between px-4 py-3 text-tg-text hover:bg-tg-hover rounded-xl transition">
      <span class="font-semibold">Create Viewer Account</span>
      <span class="text-tg-muted">{{ showCreateUser ? '−' : '+' }}</span>
    </button>
    <div v-if="showCreateUser" class="px-4 pb-4 space-y-3">
      <input v-model="newViewerUsername" type="text" placeholder="Username"
        class="w-full bg-tg-input text-tg-text rounded-lg px-4 py-2 border border-gray-700">
      <input v-model="newViewerPassword" type="password" placeholder="Password (min 4 chars)"
        class="w-full bg-tg-input text-tg-text rounded-lg px-4 py-2 border border-gray-700">
      <!-- Chat picker here -->
      <div v-if="viewerCreateError" class="text-red-400 text-sm">{{ viewerCreateError }}</div>
      <button @click="createViewer" :disabled="creatingViewer"
        class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg transition disabled:opacity-50">
        {{ creatingViewer ? 'Creating...' : 'Create Account' }}
      </button>
    </div>
  </div>

  <!-- Viewer accounts list -->
  <div class="space-y-2">
    <h3 class="text-sm font-semibold text-tg-muted uppercase tracking-wider">Viewer Accounts</h3>
    <div v-if="!viewers.length" class="text-tg-muted text-sm py-4 text-center">No viewer accounts</div>
    <div v-for="viewer in viewers" :key="viewer.id"
      class="bg-tg-bg rounded-xl p-4 border border-gray-700 flex items-center justify-between gap-3">
      <div class="min-w-0">
        <div class="text-tg-text font-medium">{{ viewer.username }}</div>
        <div class="text-tg-muted text-xs">
          {{ viewer.allowed_chat_ids ? viewer.allowed_chat_ids.length + ' chats' : 'All chats' }}
          · {{ viewer.is_active ? 'Active' : 'Disabled' }}
        </div>
      </div>
      <div class="flex gap-2 shrink-0">
        <button @click="toggleViewerActive(viewer)"
          :class="viewer.is_active ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-green-600 hover:bg-green-700'"
          class="px-3 py-1.5 text-white text-xs rounded-lg transition">
          {{ viewer.is_active ? 'Disable' : 'Enable' }}
        </button>
        <button @click="deleteViewer(viewer)"
          class="px-3 py-1.5 bg-red-600 hover:bg-red-700 text-white text-xs rounded-lg transition">
          Delete
        </button>
      </div>
    </div>
  </div>
</div>
```

### 3. Tokens Tab

```html
<div v-if="settingsTab === 'tokens'" class="space-y-6">
  <!-- Create token form (collapsible) -->
  <div class="bg-tg-bg rounded-xl border border-gray-700">
    <button @click="showCreateToken = !showCreateToken"
      class="w-full flex items-center justify-between px-4 py-3 text-tg-text hover:bg-tg-hover rounded-xl transition">
      <span class="font-semibold">Create Share Token</span>
      <span class="text-tg-muted">{{ showCreateToken ? '−' : '+' }}</span>
    </button>
    <div v-if="showCreateToken" class="px-4 pb-4 space-y-3">
      <input v-model="newTokenLabel" type="text" placeholder="Label (optional, e.g. 'For client X')"
        class="w-full bg-tg-input text-tg-text rounded-lg px-4 py-2 border border-gray-700">
      <select v-model="newTokenExpiry"
        class="w-full bg-tg-input text-tg-text rounded-lg px-4 py-2 border border-gray-700">
        <option value="">No expiry</option>
        <option value="1">1 hour</option>
        <option value="24">24 hours</option>
        <option value="168">7 days</option>
        <option value="720">30 days</option>
      </select>
      <!-- Chat picker (REQUIRED for tokens, at least 1 chat) -->
      <div v-if="tokenCreateError" class="text-red-400 text-sm">{{ tokenCreateError }}</div>
      <button @click="createToken" :disabled="creatingToken"
        class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg transition disabled:opacity-50">
        {{ creatingToken ? 'Creating...' : 'Create Token' }}
      </button>
    </div>
  </div>

  <!-- Created token display (shows once, copy-to-clipboard) -->
  <div v-if="createdTokenInfo" class="bg-green-900/30 border border-green-700 rounded-xl p-4 space-y-2">
    <div class="text-green-300 font-semibold text-sm">Token created! Copy it now — it won't be shown again.</div>
    <div class="flex gap-2">
      <input :value="createdTokenInfo.token" readonly
        class="flex-1 bg-tg-input text-green-300 font-mono text-xs rounded-lg px-3 py-2 border border-green-700">
      <button @click="copyToClipboard(createdTokenInfo.token)" class="px-3 py-2 bg-green-700 hover:bg-green-600 text-white text-xs rounded-lg">
        Copy
      </button>
    </div>
    <div class="flex gap-2">
      <input :value="createdTokenInfo.share_url" readonly
        class="flex-1 bg-tg-input text-green-300 text-xs rounded-lg px-3 py-2 border border-green-700">
      <button @click="copyToClipboard(createdTokenInfo.share_url)" class="px-3 py-2 bg-green-700 hover:bg-green-600 text-white text-xs rounded-lg">
        Copy URL
      </button>
    </div>
    <button @click="createdTokenInfo = null" class="text-tg-muted text-xs hover:text-white">Dismiss</button>
  </div>

  <!-- Active tokens list -->
  <div class="space-y-2">
    <h3 class="text-sm font-semibold text-tg-muted uppercase tracking-wider">Share Tokens</h3>
    <div v-if="!tokens.length" class="text-tg-muted text-sm py-4 text-center">No share tokens</div>
    <div v-for="token in tokens" :key="token.id"
      class="bg-tg-bg rounded-xl p-4 border border-gray-700">
      <div class="flex items-center justify-between gap-3">
        <div class="min-w-0">
          <div class="text-tg-text font-medium">{{ token.label || 'Token #' + token.id }}</div>
          <div class="text-tg-muted text-xs space-x-2">
            <span>{{ token.allowed_chat_ids.length }} chat(s)</span>
            <span>· Used {{ token.use_count }}x</span>
            <span v-if="token.expires_at">· Expires {{ formatDate(token.expires_at) }}</span>
            <span v-else>· No expiry</span>
            <span v-if="token.is_revoked" class="text-red-400">· Revoked</span>
          </div>
        </div>
        <button v-if="!token.is_revoked" @click="revokeToken(token)"
          class="px-3 py-1.5 bg-red-600 hover:bg-red-700 text-white text-xs rounded-lg transition shrink-0">
          Revoke
        </button>
      </div>
    </div>
  </div>
</div>
```

### 4. Vue state and API methods

```javascript
// Users
const viewers = ref([])
const showCreateUser = ref(false)
const newViewerUsername = ref('')
const newViewerPassword = ref('')
const newViewerChatIds = ref([])
const viewerCreateError = ref('')
const creatingViewer = ref(false)

// Tokens
const tokens = ref([])
const showCreateToken = ref(false)
const newTokenLabel = ref('')
const newTokenExpiry = ref('')
const newTokenChatIds = ref([])
const tokenCreateError = ref('')
const creatingToken = ref(false)
const createdTokenInfo = ref(null)

// Chat picker (shared)
const adminChats = ref([])
const chatSearch = ref('')

// Load data when settings opens
watch(showSettings, async (val) => {
  if (val && userRole.value === 'master') {
    await Promise.all([loadViewers(), loadTokens(), loadAdminChats()])
  }
})

async function loadViewers() { /* GET /api/admin/viewers */ }
async function loadTokens() { /* GET /api/admin/tokens */ }
async function loadAdminChats() { /* GET /api/admin/chats */ }
async function createViewer() { /* POST /api/admin/viewers */ }
async function toggleViewerActive(viewer) { /* PUT /api/admin/viewers/{id} */ }
async function deleteViewer(viewer) { /* DELETE /api/admin/viewers/{id} + confirm */ }
async function createToken() { /* POST /api/admin/tokens */ }
async function revokeToken(token) { /* DELETE /api/admin/tokens/{id} + confirm */ }
function copyToClipboard(text) { navigator.clipboard.writeText(text) }
```

---

## Todo

- [ ] Build chat picker component (search + multi-select + chips)
- [ ] Build Users tab (create form + list with toggle/delete)
- [ ] Build Tokens tab (create form + copy-to-clipboard + list with revoke)
- [ ] Add API methods (loadViewers, loadTokens, loadAdminChats, create*, delete*, toggle*)
- [ ] Add Vue state for all forms
- [ ] Confirm dialog for destructive actions (delete user, revoke token)
- [ ] Handle empty states gracefully
- [ ] Test create user → appears in list
- [ ] Test create token → plaintext displayed → copy works
- [ ] Test revoke token → token marked as revoked
- [ ] Test delete user → sessions invalidated
- [ ] Mobile responsive layout for all elements

---

## Success Criteria

- Admin can create viewer accounts with chat scope
- Admin can enable/disable/delete viewer accounts
- Admin can create tokens with label, expiry, chat scope
- Plaintext token shown once with copy-to-clipboard
- Share URL generated and copyable
- Admin can revoke tokens
- Chat picker works with search and multi-select
- All operations give immediate feedback (loading states, errors, success)
