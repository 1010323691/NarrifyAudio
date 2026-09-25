# NarrifyAudio Design System — MASTER

Source of truth for all UI work in this repo. Created 2026-09-26 during the
SaaS light-glassmorphism redesign. Page-specific overrides go in `pages/`.

**Direction (user decision):** SaaS 网站，浅色玻璃拟态 (light glassmorphism).
The `ui-ux-pro-max` skill's flat-design/teal recommendation was evaluated and
**not** adopted; the user chose glassmorphism instead. Its glassmorphism style
card and the Micro-SaaS palette (indigo #6366F1 primary) are the basis below.

## Principles

- Frosted-glass surfaces (translucent white + backdrop blur) over an ambient
  color-wash background; depth from layering, not heavy shadows.
- Light mode is the default; dark mode is fully supported with a dark-glass
  variant (translucent charcoal).
- One accent hue (indigo) carries identity; semantic tones (emerald/amber/red)
  are reserved for status only.
- Every surface that blurs has an `@supports` opaque fallback.
- Motion: 150–300ms, ease, hover/press only. `prefers-reduced-motion` respected
  (global override already in `style.css`).
- Icons are lucide-vue-next only — no emoji/glyphs as icons (♫ in
  MyResources.vue was replaced with the `Music4` icon in the redesign).

## Tokens (HSL triples in `src/style.css` :root / .dark)

| Token | Light | Dark | Notes |
|---|---|---|---|
| background | `240 33% 98%` | `240 24% 9%` | near-white cool; the ambient wash comes from `body::before` |
| foreground | `242 22% 16%` | `220 40% 96%` | |
| primary | `239 84% 67%` (#6366F1) | `239 89% 72%` | brand indigo, matches app icon |
| primary-foreground | white | white | |
| accent | `239 90% 96%` | `240 20% 22%` | hover tint (shadcn semantics) |
| muted-foreground | `240 8% 45%` | `235 10% 66%` | ≥4.5:1 on background |
| border | `240 22% 88%` | `240 14% 24%` | |
| ring | = primary | = primary | |
| radius | 1rem | 1rem | |

Semantic status colors stay Tailwind utilities (emerald/amber/red) on
`StatusPill` / `Badge` — do not introduce new hex status colors.

## Glass variables (not HSL — rgba/length tokens)

```
--glass-blur        12px (14px dark)
--glass-tint        rgba(255,255,255,.68)   / rgba(30,33,48,.55)
--glass-tint-strong rgba(255,255,255,.88)   / rgba(28,30,44,.85)  ← sticky bars, popovers
--glass-border      rgba(255,255,255,.8)    / rgba(255,255,255,.14)
--glass-edge        rgba(99,102,241,.16)    / rgba(129,140,248,.22)  ← floating popovers only
--glass-highlight   inset 0 1px 0 rgba(255,255,255,.6)  (light source)
--glass-shadow      0 2px 10px rgba(56,64,120,.05)
--glass-shadow-strong  0 16px 48px rgba(56,64,120,.14)  ← dialogs/popovers only
```

Weight rule (2026-09-26 lightening pass): a glass surface is at most
**border + highlight + one soft shadow** — never a `0 0 0 1px` edge ring on
card surfaces (double outlines fog card grids into a flat gray). Blur stays
≤12px on content panels; stronger blur/shadow is reserved for floating
surfaces (dialogs, account popover, sticky bars).

Component classes in `src/style.css` (`@layer components`):

- `.glass-panel` — standard glass surface (Card, sidebar, dialogs).
- `.glass-strong` — strong glass (sticky table headers, account popover).
- `body::before` — fixed ambient wash: indigo (top-left), sky (top-right),
    emerald (bottom) radial glows, driven by `--ambient` (per mode).

## Typographic scale

- Font stack: `Plus Jakarta Sans` (Google, loaded non-blocking in
  `index.html`) → Inter → Segoe UI → system CJK (PingFang SC / Microsoft YaHei).
  Offline-first: if the webfont never loads, the system stack carries the UI.
- Page title: 24–34px, weight 750–800, letter-spacing −0.03em, preceded by a
  28×3px primary accent bar (`.page-header::before`) + an `.eyebrow`
  (10px, 800, 0.16em tracking, primary color, uppercase).
- Body 13–14px / line-height 1.5; tabular-nums for all metrics.

## Component conventions

- **Card** — `.glass-panel` via `src/components/ui/Card.vue`; `rounded-2xl`.
- **Button** — primary: indigo, soft glow shadow (`shadow-primary/25`),
  hover lift `translateY(-1px)`; active press down. Outline/ghost stay flat.
- **StatusPill** — full pill (`rounded-full`), 1.5px dot, soft 10% tinted bg.
- **Progress** — 2.5px height, gradient indicator (indigo-400 → primary).
- **Sidebar** — 248px glass column; active item = primary-tinted glass pill
  with a 3px primary indicator; mobile = horizontal glass nav bar.
- **Tables** — inside glass cards; sticky headers use `.glass-strong`.
- **Dialogs** — hand-rolled overlays keep `bg-background` (opaque is fine over
  the 50% scrim) + `shadow-2xl`; shared dialog host uses `.glass-strong`.

## Anti-patterns (this repo)

- No opaque `bg-card` on new surfaces that sit over the ambient background —
  use `.glass-panel`.
- No new fixed hex colors in scoped styles beyond the status triad; reference
  `hsl(var(--token))` or the glass variables.
- No emoji as icons; no full-color gradients on text.
- No `backdrop-filter` on elements with `overflow: hidden` scroll containers
  (blur + scrollbars is costly); it lives on the card shell, not inner lists.

## Pages

Per-page overrides (if any) live in `design-system/narrifyaudio/pages/<name>.md`.
None yet — every page currently follows Master.
