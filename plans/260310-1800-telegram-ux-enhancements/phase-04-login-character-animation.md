# Phase 4: Animated Login Character (SVG)

**Priority:** Medium | **Effort:** Medium | **Backend:** None

## Context

- Current login has a static FontAwesome `fa-telegram-plane` icon in a white circle
- Telegram Web uses a Lottie-animated monkey that covers eyes on password focus
- We'll use inline SVG + CSS keyframes (no external libraries)

## Requirements

- Replace static icon with animated SVG character above login form
- Character has "hands" that cover "eyes" when password field is focused
- On password blur, hands move away
- On username focus, character's eyes slightly track toward input
- Idle state: subtle breathing animation (scale pulse)
- Must work across all themes (use CSS variables for colors)

## Design: Simple Geometric Character

A circle-based character (similar to a simple robot/owl):

```
     .-""-.
    / O  O \      ← circle face, dot eyes
   |  ----  |     ← arc smile
    \      /
     '----'
   [  ][  ]       ← two rectangle "hands"
```

SVG elements:
- `<circle>` — face (fill: var(--tg-accent))
- 2x `<circle>` — eyes (fill: white, smaller)
- 2x `<circle>` — pupils (fill: var(--tg-bg), even smaller)
- `<path>` — smile arc (stroke: white)
- 2x `<rect rx="8">` — hands (fill: var(--tg-accent), darker shade)

## Related Code Files

- `src/web/templates/index.html` lines 439-496 (login form)
- Lines ~18 (Google Fonts link)
- Lines ~82 (font-family CSS)

## Implementation Steps

### 1. Vue State (~5 lines)
```javascript
const loginCharState = ref('idle') // 'idle' | 'username' | 'password' | 'peek'
```

### 2. Input Event Bindings
On username input: `@focus="loginCharState = 'username'"` `@blur="loginCharState = 'idle'"`
On password input: `@focus="loginCharState = 'password'"` `@blur="loginCharState = 'idle'"`

### 3. SVG Character (~60 lines)
```html
<div class="login-character" :class="'char-' + loginCharState">
  <svg viewBox="0 0 120 120" width="120" height="120">
    <!-- Face -->
    <circle cx="60" cy="50" r="40" fill="var(--tg-accent)" class="char-face"/>

    <!-- Eyes -->
    <g class="char-eyes">
      <circle cx="45" cy="42" r="6" fill="white"/>
      <circle cx="75" cy="42" r="6" fill="white"/>
      <circle cx="45" cy="42" r="3" fill="var(--tg-bg)" class="char-pupil-l"/>
      <circle cx="75" cy="42" r="3" fill="var(--tg-bg)" class="char-pupil-r"/>
    </g>

    <!-- Smile -->
    <path d="M 45 60 Q 60 72 75 60" stroke="white" stroke-width="2.5"
          fill="none" stroke-linecap="round" class="char-smile"/>

    <!-- Hands -->
    <rect x="18" y="75" width="22" height="14" rx="7"
          fill="var(--tg-accent)" filter="brightness(0.85)" class="char-hand-l"/>
    <rect x="80" y="75" width="22" height="14" rx="7"
          fill="var(--tg-accent)" filter="brightness(0.85)" class="char-hand-r"/>
  </svg>
</div>
```

### 4. CSS Animations (~40 lines)
```css
/* Idle: subtle breathing */
.login-character svg {
  animation: charBreathe 3s ease-in-out infinite;
}
@keyframes charBreathe {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.03); }
}

/* Username focus: eyes look down-left */
.char-username .char-pupil-l,
.char-username .char-pupil-r {
  transform: translate(-1px, 2px);
  transition: transform 0.3s ease;
}

/* Password focus: hands cover eyes */
.char-password .char-hand-l {
  transform: translate(14px, -38px);
  transition: transform 0.3s ease;
}
.char-password .char-hand-r {
  transform: translate(-14px, -38px);
  transition: transform 0.3s ease;
}

/* Default hand position transition */
.char-hand-l, .char-hand-r {
  transition: transform 0.3s ease;
}
.char-pupil-l, .char-pupil-r {
  transition: transform 0.3s ease;
}
```

### 5. Replace Static Icon
Remove the existing `<div>` with FontAwesome `fa-telegram-plane` and replace with the SVG character.

### 6. Theme Compatibility
Character uses `var(--tg-accent)` for face/hands and `var(--tg-bg)` for pupils. Works across all themes automatically. The white eyes/smile work on all accent colors.

## Success Criteria

- [ ] Character visible above login form
- [ ] Hands cover eyes on password focus (0.3s transition)
- [ ] Hands uncover on password blur
- [ ] Eyes shift on username focus
- [ ] Idle breathing animation
- [ ] Works across all 6 themes
- [ ] No external dependencies loaded
- [ ] Character looks clean and intentional (not janky)

## Risk

- **Visual quality**: The SVG must look polished. Test across themes — some accent colors may make the character less visible. Mitigation: use white elements for contrast.
- **Hand positioning**: The translate values for "covering eyes" need precise tuning. Test in browser dev tools before committing.
