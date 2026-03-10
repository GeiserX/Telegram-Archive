# Vue 3 CDN Admin Settings UI Patterns Research

**Date:** 2026-02-24 | **Max Lines:** 120

## 1. Vue 3 CDN + Composition API Pattern

**CDN Build:** Use global build from `unpkg.com/vue@3/dist/vue.global.js`

**CDN-Ready Composition API Pattern:**
```javascript
const { createApp, ref, computed } = Vue;

createApp({
  setup() {
    const users = ref([]);
    const selectedUser = ref(null);
    const newUser = ref({ username: '', password: '', chatIds: [] });

    const filteredChats = computed(() => availableChats.filter(...));
    const addUser = () => { users.value.push({...}) };
    const deleteUser = (id) => { users.value = users.value.filter(...) };

    return { users, selectedUser, newUser, addUser, deleteUser, filteredChats };
  },
  template: `<div><!-- template here --></div>`
}).mount('#app');
```

**Key:** No build step needed. Vue 3 global build exposes all Composition API functions directly. Works with inline templates.

## 2. CRUD Table UI Pattern (Tailwind CSS)

**Admin template benchmarks** (TailAdmin Vue, Admin One Vue Tailwind) show:
- Collapse/expand rows for edit mode
- Inline action buttons (Edit, Delete, Save, Cancel)
- Modal-based editing for complex forms
- Sticky header with horizontal scroll on mobile

**Minimal CRUD Pattern:**
```html
<div class="overflow-x-auto">
  <table class="w-full text-sm">
    <thead class="bg-gray-100 sticky top-0">
      <tr>
        <th class="px-4 py-2 text-left">Username</th>
        <th class="px-4 py-2">Actions</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="user in users" class="border-b hover:bg-gray-50">
        <td class="px-4 py-2">{{ user.username }}</td>
        <td class="px-4 py-2">
          <button @click="editUser(user)" class="text-blue-600">Edit</button>
          <button @click="deleteUser(user.id)" class="text-red-600">Delete</button>
        </td>
      </tr>
    </tbody>
  </table>
</div>
```

**Key:** Tailwind classes handle responsive design without build step (CDN Tailwind works fine).

## 3. Multi-Select Chat Picker UX

**Telegram Bot patterns inform design:**
- Checkbox list with count badge: "4 of 12 chats selected"
- Scrollable container (fixed height)
- Search/filter input above the list
- Visual feedback: highlight on hover, checkmark on select

**Implementation Pattern:**
```html
<div class="border rounded p-4">
  <input
    v-model="chatSearch"
    type="text"
    placeholder="Search chats..."
    class="w-full px-3 py-2 border rounded mb-3"
  />

  <div class="max-h-48 overflow-y-auto border rounded">
    <label v-for="chat in filteredChats" class="flex items-center p-2 hover:bg-gray-100 cursor-pointer">
      <input
        type="checkbox"
        :checked="selectedChats.includes(chat.id)"
        @change="toggleChat(chat.id)"
        class="mr-2"
      />
      <span>{{ chat.name }}</span>
    </label>
  </div>

  <div class="text-xs text-gray-600 mt-2">
    {{ selectedChats.length }} / {{ availableChats.length }} selected
  </div>
</div>
```

## 4. Admin Settings Panel Structure

**Telegram Web admin layout inspiration:**
- Sidebar menu with icons (Settings, Users, Chats, Logs)
- Main content area with form sections
- Settings grouped by category with collapsible sections
- Save/Cancel buttons at bottom

**Composable Pattern:**
```javascript
const useAdminSettings = () => {
  const settings = ref({
    general: { appName: '', maxUsers: 10 },
    security: { requirePassword: true, sessionTimeout: 30 }
  });

  const saveSettings = async () => { /* API call */ };
  const resetSettings = () => { /* reload */ };

  return { settings, saveSettings, resetSettings };
};
```

## 5. Key Findings

| Aspect | Pattern | Notes |
|--------|---------|-------|
| **CDN Build** | Global `vue.global.js` | All Composition API available, no build needed |
| **Table State** | Ref for data, computed for filtering | Reactive updates automatic |
| **Multi-Select** | Checkbox list with badge count | Telegram-inspired pattern |
| **Modal/Edit** | v-if toggle with overlay | Simpler than collapse for CDN |
| **Responsive** | Tailwind CDN classes | Works without build step |
| **Admin Panel** | Tab/sidebar navigation | Match Telegram Web structural patterns |

## 6. Recommended Stack

- **Vue 3 CDN** + Composition API (global build)
- **Tailwind CSS CDN** for styling
- **AlpineJS for enhancement** (lightweight, CDN-only)
- **Fetch API** for user/chat management endpoints
- **LocalStorage** for client-side state persistence

## 7. Unresolved Questions

- Should password editing use masked input with reveal toggle?
- Need API endpoint specs for user CRUD operations?
- Should deleted users be soft-deleted or hard-deleted?
- Multi-select: Allow drag-reorder of assigned chats?

## Sources

- [Vue 3 Composition API FAQ](https://vuejs.org/guide/extras/composition-api-faq.html)
- [TailAdmin Vue Dashboard Template](https://github.com/TailAdmin/vue-tailwind-admin-dashboard)
- [Admin One Vue 3 Tailwind Dashboard](https://github.com/justboil/admin-one-vue-tailwind)
- [Telegram Bot Multi-Select Patterns](https://medium.com/@moraneus/enhancing-user-engagement-with-multiselection-inline-keyboards-in-telegram-bots-7cea9a371b8d)
- [Telerik MultiSelect Component](https://www.telerik.com/design-system/docs/components/multiselect/)
