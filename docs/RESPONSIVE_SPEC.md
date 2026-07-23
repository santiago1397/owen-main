# Responsive spec — iPhone support across all modules

Target: the OWEN frontend renders and works correctly on iPhone, down to a 375px viewport,
across all 13 modules. Desktop is unchanged.

Decisions below were settled in a design interview; each one is a closed question, not a
suggestion. Where a cheaper option was rejected, the reason is recorded so we don't relitigate.

## 1. Scope: which modules get real mobile UX

Two tiers, because the app has two different kinds of surface.

**Comms tier — real mobile UX.** Dashboard, Calls, Callers, Inbox, Messages, and the softphone.
These are things an operator plausibly does from a phone, away from a desk. They get purpose-built
mobile interaction patterns, not just non-broken CSS.

**Config tier — readable and unbroken, desktop job.** Numbers, NumberDetail, Flows, FlowEditor,
Agents, Emails, Settings. These must never horizontally scroll the page, clip content, or become
illegible, but they are not redesigned for touch. Editing flows and agent configs remains a
desktop activity.

## 2. Technical approach

Extend the existing hand-written `src/styles.css` with media queries. **No CSS framework.**

Tailwind was rejected: the app has 276 inline `style={{}}` blocks and zero tests, on a live
phone system. Introducing a second styling system — or rewriting all 13 pages — buys ergonomics
we don't need at a regression risk we can't detect.

Inline styles are migrated into CSS classes **only where they must respond** (fixed widths, fixed
grid column counts). Purely cosmetic inline styles (colors, gaps, font sizes) are left alone.

## 3. Breakpoints

Floor: **375px** — covers every iPhone on a supported iOS, including SE 2/3 and 13 mini.
320px (original SE) is not supported; it tops out at iOS 15.

Two breakpoints:

| Query | Purpose |
|---|---|
| `max-width: 900px` | Structure. Sidebar becomes drawer, split panes go single-pane, tables get scroll wrappers, FlowEditor goes read-only. |
| `max-width: 560px` | Phone density. Grids collapse to one column, padding tightens, comms tables become cards, bottom tabs appear. |

Landscape iPhone (667–932px wide) lands inside the 900px query and therefore gets the drawer
shell — correct, since landscape phone height is only ~375–430px.

## 4. Desktop is pixel-identical

Every rule lives inside a `max-width` media query. At >900px the app renders exactly as it does
today. This makes the blast radius of the whole project "phones only" — any desktop regression
report is definitively not from this work.

Honest caveat: the drawer shell (§5) and the URL-state change (§7) necessarily touch shared code.
Both are written to be purely additive so desktop *renders* identically, but those two files are
genuinely modified and get careful review.

## 5. App shell / navigation

The fixed 210px sidebar (`styles.css:11`) is the single highest-leverage fix — it affects all 13
routes.

**Phase 1:** top bar with hamburger → slide-in drawer that reuses the existing `<aside>` markup
verbatim, preserving the Attribution/Platform/System grouping. All 13 routes work immediately.

**Phase 5:** iOS-style bottom tab bar for the comms routes (Dashboard, Calls, Inbox, Messages),
with the drawer retained for the long tail.

Rejected — icon-only rail: 11 nav items as icons need a labeling system and still eat horizontal
space on a 390px screen.

## 6. Tables

Effort follows the tier split from §1.

- **Calls (7 col), Callers (7 col)** — comms tier. Become stacked card rows below 560px: caller
  and time prominent, remaining fields as secondary lines.
- **Numbers (8 col), Emails (6 col), Flows (4 col)** — config tier. Get a `.tablewrap
  { overflow-x: auto }` wrapper. Honest, unbroken, cheap.

Rejected — priority columns with expandable rows: hides data in a way that surprises people, and
adds per-page interaction state to test.

## 7. Split panes — Inbox and Messages

Below 900px these show one pane at a time: list → tap → conversation.

Selection currently lives in local `useState` (`Inbox.tsx:314`, `Messages.tsx:151`), which means
an iOS edge-swipe-back would exit the module entirely instead of returning to the list. So
**selection moves into the URL** via `useSearchParams`:

- `/inbox?c=<caller_id>`
- `/messages?t=<thread_key>`

Browser back and iOS swipe-back then work for free, and conversations become deep-linkable —
which we will want the first time a push notification needs to open a thread. The desktop 3-pane
layout is unaffected.

### Inbox contact panel (third pane)

`.quo-side` (300px, `styles.css:177`) becomes a slide-over triggered by tapping the contact name
in the conversation header. It reuses the existing `.drawer` pattern (`styles.css:46`), which is
already the one mobile-safe component in the codebase at `max-width: 92vw`.

## 8. FlowEditor

Below 900px: palette and node panel hide, canvas fills the screen with pan/zoom (xyflow already
handles touch), tapping a node opens its config **read-only**, and a banner explains that editing
requires a larger screen.

This keeps the genuinely useful phone case — "why did this call route that way?" — without
pretending a node graph is thumb-editable, and without risking fat-fingered edits to live call
routing.

## 9. iOS-specific correctness

Separate from layout. The app can be perfectly responsive and still feel wrong without these.

1. **16px inputs below 900px.** `input, select, button` is currently `font-size: 13px`
   (`styles.css:38`); anything under 16px makes iOS Safari auto-zoom on every field focus.
   Raised inside the mobile query only, so desktop keeps its 13px density.
   *Not* fixed with `user-scalable=no` — that breaks pinch-zoom accessibility and modern iOS
   ignores it anyway.
2. **`100vh` → `100dvh`** in all 5 occurrences (`styles.css:10,46,51,64,116`). On iOS Safari
   `100vh` includes the URL bar, so the Inbox composer and FlowEditor canvas get cut off below
   the fold. Fallback line retained for older browsers.
3. **`env(safe-area-inset-*)` padding** on the top bar, bottom tab bar, and Inbox composer, so
   they don't run under the notch/Dynamic Island or the home indicator.
4. **44px minimum tap targets** (Apple's minimum) inside the mobile query. Current buttons are
   ~8–10px padding at 13px font; Quo filter chips and table row actions are smaller still.

Out of scope for now: PWA / add-to-home-screen (`apple-touch-icon`, `theme-color`, manifest).

## 10. Verification

Playwright added as a **devDependency** (never ships in the prod bundle), using the **WebKit**
engine — same engine family as Safari, so it catches `dvh` and safe-area behavior that Chromium
would paper over.

A script loads all 13 routes at 375 / 390 / 430px and writes PNGs for review. Without this the
CSS is written blind.

**Environment:** local `npm run dev` pointed at the production API
(`https://api.owen.santiagoproperties.uk`), so screenshots exercise real data lengths — real
caller names, real message threads — which is where responsive bugs actually hide.
Navigation and screenshots only; **no mutating actions** (send/save/delete) against the live
system.

Requires a login credential or a token to seed into localStorage.

## 11. Sequencing

Branch `responsive`, one commit per phase, single deploy after screenshot review. Production is
never left in a half-migrated state, and a bad outcome is one revert.

| Phase | Content |
|---|---|
| 0 | Playwright harness + screenshot script (baseline of current breakage) |
| 1 | Foundations: drawer shell, breakpoint tokens, `.tablewrap`, Login width clamp, all four §9 iOS fixes |
| 2 | Tables → cards/wrappers; Agents form grids to single column |
| 3 | Split panes: Inbox + Messages URL state, single-pane, contact slide-over |
| 4 | FlowEditor read-only mobile mode |
| 5 | Bottom tab bar for comms routes |

Departure from the usual trunk-based flow is deliberate and justified by the size of the change
and the live-calls risk.

## Known issues surfaced during design, NOT in this scope

- **No global in-call bar.** `IncomingCallModal` is global (`App.tsx:59`) so a call can be
  *answered* from any route, but `InCallBar` is mounted inline only on `Calls.tsx:236` and
  `Inbox.tsx:432`. On a phone, once a call is live, hold/transfer/hangup exist only if you happen
  to be on those two routes. This is a real mobile gap but it is a feature change, not a
  responsive one. Recommended as a follow-up.
- **Production is one commit behind.** Server is at `4dd4e83`; `b4e1ff8` ("Default call handling
  for unassigned DIDs") is on `origin/main` but not deployed.
