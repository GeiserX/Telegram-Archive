# Phase 1: Settings Panel Shell & Theme System

**Priority:** HIGH — Foundation for all other phases
**Status:** TODO
**Effort:** Medium
**Files:** `src/web/templates/index.html`

---

## Overview

Build the settings panel modal (full-screen overlay with tab navigation) and implement CSS-variable-based theme system with 6 presets. This phase is frontend-only — no backend changes.

---

## Key Insights

- Current app uses hardcoded Tailwind `tg-*` colors in `tailwind.config` (line 357-374)
- Must convert to CSS variables so themes can swap at runtime
- Existing modal pattern: `fixed inset-0 bg-black/70 backdrop-blur-sm z-50`
- No admin-only UI exists yet — need role check (`userRole === 'master'`)
- Login response returns `role: "master"` which must be stored in Vue state
- Theme must apply BEFORE Vue mount to prevent flash (inline `<script>` in `<head>`)

---

## Implementation Steps

### 1. Add theme CSS variables to `<style>` section

After existing `:root` block (line 25), add theme variable definitions:

```css
/* Theme system — CSS variables */
:root, .theme-midnight {
  --tg-bg: #0f172a;
  --tg-sidebar: #1e293b;
  --tg-hover: #334155;
  --tg-active: #2b5278;
  --tg-text: #e2e8f0;
  --tg-muted: #94a3b8;
  --tg-own: #2b5278;
  --tg-other: #182533;
  --tg-accent: #3b82f6;
  --tg-border: #374151;
  --tg-input: #111827;
}
.theme-dark { /* ... values from plan.md table */ }
.theme-nord { /* ... */ }
.theme-solarized { /* ... */ }
.theme-oled { /* ... */ }
.theme-light { /* ... requires removing dark class */ }
```

### 2. Update Tailwind config to use CSS variables

Change `tailwind.config` (line 357-374) from hardcoded hex to `var()`:

```javascript
tailwind.config = {
    darkMode: 'class',
    theme: {
        extend: {
            colors: {
                tg: {
                    bg: 'var(--tg-bg)',
                    sidebar: 'var(--tg-sidebar)',
                    hover: 'var(--tg-hover)',
                    active: 'var(--tg-active)',
                    text: 'var(--tg-text)',
                    muted: 'var(--tg-muted)',
                    own: 'var(--tg-own)',
                    other: 'var(--tg-other)',
                }
            }
        }
    }
}
```

### 3. Add early theme loader in `<head>`

Before Vue/Tailwind scripts, add inline script to prevent flash:

```html
<script>
(function(){
  var t = localStorage.getItem('tg-theme') || 'midnight';
  document.documentElement.className = (t === 'light' ? '' : 'dark') + ' theme-' + t;
})();
</script>
```

### 4. Store user role in Vue state

After login response, store role:

```javascript
const userRole = ref(null)  // 'master' or 'viewer'

// In performLogin success handler:
userRole.value = data.role

// In checkAuth (onMounted):
// GET /api/auth/check returns role
userRole.value = data.role
```

### 5. Add gear icon button in header

In the chat header area (line ~822), add settings gear visible only to admin:

```html
<button v-if="userRole === 'master'" @click="showSettings = true"
    class="p-2 text-tg-muted hover:text-tg-text transition" title="Settings">
    <svg class="w-5 h-5" ...><!-- gear icon --></svg>
</button>
```

### 6. Build settings modal

After existing modals (line ~1463), add:

```html
<div v-if="showSettings" class="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4"
    @click.self="showSettings = false">
  <div class="bg-tg-sidebar rounded-2xl shadow-2xl w-full max-w-3xl max-h-[90vh] border border-gray-700 flex flex-col overflow-hidden">
    <!-- Header -->
    <div class="flex items-center justify-between px-6 py-4 border-b border-gray-700">
      <h2 class="text-xl font-bold text-tg-text">Settings</h2>
      <button @click="showSettings = false" class="text-gray-400 hover:text-white p-1">✕</button>
    </div>
    <!-- Tab bar -->
    <div class="flex gap-1 px-4 py-2 bg-tg-bg/50 border-b border-gray-700 overflow-x-auto">
      <button v-for="tab in settingsTabs" :key="tab.id"
        @click="settingsTab = tab.id"
        :class="settingsTab === tab.id ? 'bg-tg-active text-white' : 'text-tg-muted hover:text-tg-text hover:bg-tg-hover'"
        class="px-3 py-1.5 rounded-lg text-sm font-medium transition whitespace-nowrap">
        {{ tab.label }}
      </button>
    </div>
    <!-- Tab content (scrollable) -->
    <div class="flex-1 overflow-y-auto p-6">
      <!-- Phase 2: Account tab content -->
      <!-- Phase 3: Users tab content -->
      <!-- Phase 3: Tokens tab content -->
      <!-- Appearance tab (this phase) -->
      <!-- Phase 4: General tab content -->
    </div>
  </div>
</div>
```

### 7. Appearance tab — Theme picker

```html
<div v-if="settingsTab === 'appearance'" class="space-y-6">
  <h3 class="text-lg font-semibold text-tg-text">Theme</h3>
  <div class="grid grid-cols-2 sm:grid-cols-3 gap-3">
    <button v-for="theme in themes" :key="theme.id"
      @click="setTheme(theme.id)"
      :class="currentTheme === theme.id ? 'ring-2 ring-blue-500' : ''"
      class="rounded-xl p-3 border border-gray-700 transition hover:border-gray-500">
      <!-- Theme preview swatch -->
      <div class="flex gap-1 mb-2 h-8 rounded-lg overflow-hidden">
        <div :style="{backgroundColor: theme.bg}" class="flex-1"></div>
        <div :style="{backgroundColor: theme.sidebar}" class="flex-1"></div>
        <div :style="{backgroundColor: theme.accent}" class="w-4"></div>
      </div>
      <span class="text-sm text-tg-text">{{ theme.name }}</span>
    </button>
  </div>
</div>
```

### 8. Vue state & methods for theme

```javascript
const showSettings = ref(false)
const settingsTab = ref('appearance')
const currentTheme = ref(localStorage.getItem('tg-theme') || 'midnight')
const settingsTabs = [
  { id: 'account', label: 'Account' },
  { id: 'users', label: 'Users' },
  { id: 'tokens', label: 'Tokens' },
  { id: 'appearance', label: 'Appearance' },
  { id: 'general', label: 'General' },
]
const themes = [
  { id: 'midnight', name: 'Midnight', bg: '#0f172a', sidebar: '#1e293b', accent: '#3b82f6' },
  { id: 'dark', name: 'Dark', bg: '#111827', sidebar: '#1f2937', accent: '#6366f1' },
  { id: 'nord', name: 'Nord', bg: '#2e3440', sidebar: '#3b4252', accent: '#88c0d0' },
  { id: 'solarized', name: 'Solarized', bg: '#002b36', sidebar: '#073642', accent: '#2aa198' },
  { id: 'oled', name: 'OLED', bg: '#000000', sidebar: '#0a0a0a', accent: '#3b82f6' },
  { id: 'light', name: 'Light', bg: '#f8fafc', sidebar: '#ffffff', accent: '#2563eb' },
]

function setTheme(themeId) {
  currentTheme.value = themeId
  localStorage.setItem('tg-theme', themeId)
  const el = document.documentElement
  // Remove old theme classes
  el.className = el.className.replace(/theme-\w+/g, '').trim()
  // Add new theme class, toggle dark/light
  if (themeId === 'light') {
    el.classList.remove('dark')
  } else {
    el.classList.add('dark')
  }
  el.classList.add('theme-' + themeId)
}
```

### 9. Update hardcoded colors

Must update custom CSS that uses hardcoded hex values to use CSS variables:
- Scrollbar track: `#1e293b` → `var(--tg-sidebar)`
- Scrollbar thumb: `#475569` → `var(--tg-muted)`
- Flatpickr overrides: replace hardcoded colors with variables
- Any inline `style` attributes with hardcoded colors

### 10. Update return statement

Add to Vue return object: `showSettings`, `settingsTab`, `settingsTabs`, `currentTheme`, `themes`, `setTheme`, `userRole`

---

## Todo

- [ ] Define all 6 theme CSS variable sets
- [ ] Convert Tailwind `tg-*` from hex to `var()`
- [ ] Add `<head>` theme loader script (prevent flash)
- [ ] Store `userRole` from auth check response
- [ ] Add gear icon in header (admin-only)
- [ ] Build settings modal shell with tab navigation
- [ ] Build Appearance tab with theme picker grid
- [ ] Update hardcoded CSS colors to use variables
- [ ] Test theme switching (all 6 presets)
- [ ] Test Light theme (removes `dark` class)
- [ ] Test persistence across page reload
- [ ] Verify mobile responsive layout of modal

---

## Success Criteria

- Settings gear visible only to admin
- Modal opens with tabbed navigation
- 6 theme presets render correctly
- Theme persists after page reload (no flash)
- Light theme properly inverts colors
- All existing UI elements use CSS variables (no hardcoded colors left in customizable spots)

---

## Security Considerations

- Settings modal only shown when `userRole === 'master'`
- Theme data is localStorage only — no sensitive data
- No backend calls needed for theming
