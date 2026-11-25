---
name: ux-audit
description: >-
  UX & Google Best Practices specialist audit for Authenix (frontend & Chrome
  extension). Evaluates Core Web Vitals (LCP/INP/CLS), Material/Google UX
  heuristics, WCAG 2.1 AA accessibility, Mobile-First responsiveness, dark-mode
  design-token hygiene, and async feedback states. Use during /swarm reviews,
  UI/UX improvement passes, or PR reviews touching frontend/ or extension/.
---

# UX & Google Best Practices Audit — Authenix

Specializes in auditing and improving user experience across `satoricheck/frontend/` and `satoricheck/extension/` using **Google UX Best Practices** (Core Web Vitals, Material Design touch/ergonomics, WCAG 2.1 AA, Google HEART framework) and **Authenix Frontend Invariants** (Vanilla ES6+ / HTML5 / 7-1 CSS3, Mobile-First, dark glassmorphism, `0.2s–0.3s` transitions, no React/Tailwind).

## Launch & Hunt

Run from the repository root (or from `satoricheck/` with `../` prefixes):

```bash
# Full frontend & extension UX scan
.venv/bin/python .agents/skills/ux-audit/scripts/ux_hunt.py

# Changed/untracked files only (PRs and swarm diff reviews)
.venv/bin/python .agents/skills/ux-audit/scripts/ux_hunt.py --diff

# Optional CI-strict exit (returns 1 if any leads are printed)
.venv/bin/python .agents/skills/ux-audit/scripts/ux_hunt.py --strict
```

## 6 Google UX & Authenix Auditing Lenses

Triage every surfaced lead (and manually inspect user flows) through these six lenses:

1. **Core Web Vitals & Perceived Speed (LCP / INP / CLS)**:
   - **CLS**: Reserve space for async content, images, badges, and skeletons (`aspect-ratio`, explicit `width`/`height`, min-heights). Never animate layout-triggering CSS properties (`top`, `left`, `width`, `height`, `margin`)—use `transform` and `opacity` only.
   - **INP**: Keep click/input handlers lean; give immediate visual feedback (`disabled`/loading state, optimistic UI, or spinner) within `100ms` before awaiting network calls.
   - **LCP**: Avoid render-blocking `<script>` tags in `<head>` (use `defer`/`type="module"`).

2. **Visibility of System Status & Error Recovery (Google Material / Nielsen #1 & #9)**:
   - Every async action (`fetch`, file upload, media check, billing checkout, extension sync) must have clear **loading**, **success**, **empty**, and **recoverable error** states.
   - Never swallow errors with `console.error` alone or use blocking native `alert()`/`confirm()`/`prompt()` dialogs—surface actionable in-app toasts/banners with a retry or next step.
   - Dynamic status regions (toasts, verdict cards, progress) should use `aria-live="polite"` (or `"assertive"` for critical errors).

3. **Mobile-First Ergonomics & Viewport Safety (Google Mobile UX)**:
   - **Touch Targets**: Interactive controls (`button`, `a`, icon buttons, toggles) must have at least `44x44px` (`48x48px` Material ideal) hit area on touch devices.
   - **Viewport & Overflow**: Design for `320px–375px` first; avoid fixed `width >= 300px` without `max-width: 100%`; prefer `100dvh` (with `100vh` fallback) so mobile browser chrome never clips bottom actions; keep consolidated breakpoints in `css/responsive/_mobile.css`.

4. **Accessibility & Keyboard Navigation (WCAG 2.1 AA)**:
   - Never strip focus rings (`outline: none` / `outline: 0`) without a visible `:focus-visible` indicator.
   - Use semantic `<button>` / `<a href>` instead of clickable `<div>`/`<span>`; icon-only buttons require `aria-label`; form inputs require `<label for>` or `aria-label`; images require `alt`.
   - Respect `@media (prefers-reduced-motion: reduce)` for animations/transitions.

5. **Cognitive Load, Clarity & Progressive Disclosure**:
   - **One primary CTA per view/card**; keep copy concise, scannable, and action-oriented (avoid jargon like raw error codes or internal backend terms).
   - **Progressive disclosure**: Hide advanced/secondary options behind clean toggles, accordions, or modals rather than crowding the primary workflow.
   - **Consistent micro-interactions**: Honor Authenix's `0.2s–0.3s` transition timing and dark glassmorphism hierarchy.

6. **Design Token & 7-1 CSS Hygiene**:
   - Reuse CSS custom properties (`var(--...)`) in `frontend/css/abstracts/_variables.css` rather than hardcoding hex colors in component/page stylesheets.
   - Respect the `frontend/css/main.css` 7-1 import order and avoid `!important` outside `utilities/_helpers.css`.

## Output Format (For Swarm & Review Reports)

When reporting UX findings (especially in a `/swarm` review):
- Group findings by **Severity** (`P0 Blocker` -> `P1 High-Impact Friction` -> `P2 Polish`) and cite exact `file:line`.
- For each finding, state **User Impact** (why it hurts the user), **Google UX Principle** (e.g., INP, CLS, WCAG 2.1, Touch Target), and a **Concrete Fix**.
- Do **not** propose React/Tailwind/framework migrations—keep all recommendations strictly within Authenix's Vanilla JS/HTML5/7-1 CSS3 architecture.
