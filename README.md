# CdpBrowser

A **framework-independent, pure-CDP** browser automation core for Python,
with an optional Robot Framework adapter. It talks to the Chrome DevTools
Protocol directly with nothing but Python + `websockets` — no Playwright,
no Node, no Selenium.

Two ways to use it:

- **Plain Python** — scripts, pytest, any tool: `pip install cdpbrowser`
- **Robot Framework** — keywords with lazy launch, evidence chains:
  `pip install "cdpbrowser[robot]"`

## Philosophy — 5 principles

1. **Pure CDP** — no driver binaries, no Node runtime, no Selenium grid.
   Plain Python + `websockets` speaking straight to Chrome.
2. **Poll by default** — a missing element is not an error, it is a *retry*.
   Every interaction/assertion polls its condition until the deadline,
   absorbing races with async UIs.
3. **Atomic JS bridge** — find → visible → enabled checks complete in a
   single bridge.js step. `Runtime.evaluate` (`awaitPromise` +
   `returnByValue`) returns JSON straight to Python.
4. **Evidence chain** — every capture lands under
   `{outputdir}/evidence/{suite}/{test}/` with `NNNN` sequence numbers.
   On failure a listener auto-saves the last screen.
5. **Framework decoupling** — the core knows nothing about Robot; the
   keyword adapter (`[robot]` extra) is a thin layer on top. In Robot,
   `Library CdpBrowser` is all it takes.

## Installation

```bash
pip install cdpbrowser                     # core only (plain Python)
pip install "cdpbrowser[robot]"            # + Robot Framework adapter
pip install "cdpbrowser[visual]"           # + Pillow (visual regression)
pip install "cdpbrowser[dev,robot,visual]" # everything, for development
```

Without robotframework, `cdpbrowser.CdpBrowser` (and `from cdpbrowser
import *`) raises an `ImportError` pointing at the `[robot]` extra — the
core (`PageSession` & friends) works with no extras at all.

## Using from plain Python (no Robot)

The core is a synchronous Python API — this is exactly what the pytest
suite (344 tests) drives:

```python
from cdpbrowser import ChromeProcess, CdpConnection, PageSession, find_chrome

chrome = ChromeProcess(find_chrome(), headless=True)
chrome.start()
try:
    session = PageSession(CdpConnection(chrome.ws_url)).attach()
    session.navigate("https://example.com")

    # Atomic bridge primitives — poll-friendly building blocks.
    session.call("click", {"s": "id", "v": "submit"})       # testid: selector
    text = session.call("getText", {"s": "id", "v": "result"})

    # Dialogs/downloads use the same arm -> trigger -> wait pattern.
    handle = session.arm_download()
    session.call("click", {"s": "id", "v": "download-link"})
    saved_path = session.wait_download(handle, 10.0)

    # Frame scope, viewport, cookies, real key/mouse input are also
    # available as plain methods (see PageSession docstrings).
finally:
    session.close()
    chrome.stop()
```

Higher-level waits: `cdpbrowser.polling.poll_until` retries any callable
until a deadline — pair it with the bridge calls, or copy the pattern the
Robot adapter uses.

## Quick start — Robot Framework

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,robot]"   # websockets + robotframework (+ pytest)
```

```robotframework
*** Settings ***
Library           CdpBrowser

*** Test Cases ***
Hello CdpBrowser
    Go To    file:///tmp/demo.html
    ${title}=    Get Title
    Should Be Equal    ${title}    Demo
    Click    testid:submit-button
    Element Should Be Visible    testid:done-panel
    [Teardown]    Close Browser
```

Chrome launches lazily on the first keyword call — no import-time settings
needed. `headless` defaults to `auto`: headless when the `CI` env var is set
or `DISPLAY` is missing; under root (euid 0) `--no-sandbox` is added
automatically.

### Import arguments

| Argument | Default | Description |
| --- | --- | --- |
| `chrome_path` | auto-detected | Path to the Chrome executable (see env vars below) |
| `headless` | `auto` | `True`/`False`/`auto` — `auto` means headless under CI or without DISPLAY |
| `timeout` | `5s` | Default deadline for polling keywords (Robot time string) |
| `poll_interval` | `0.1s` | Polling retry interval |
| `baseline_dir` | `visual-baselines/` | Root directory for visual-regression baselines |

## Keyword reference (Robot adapter)

### Session

| Keyword | Args | Description |
| --- | --- | --- |
| `Open Browser` | — | Explicit launch (no-op when already open; lazy auto-launch is the default) |
| `Close Browser` | — | Tears down all tabs, the CDP connection, and the Chrome process (idempotent) |
| `Go To` | `url` | Navigates and waits for load; returns the final URL |
| `Get Title` | — | `document.title` |
| `Get Current Url` | — | `location.href` |

### Interaction — poll by default

| Keyword | Args | Description |
| --- | --- | --- |
| `Click` | `selector` | Atomic find→visible→enabled→click in one bridge step. Retries until the deadline |
| `Fill Text` | `selector`, `text` | React-compatible value set (native setter + input/change events) |

### Assertions — poll by default

| Keyword | Args | Description |
| --- | --- | --- |
| `Element Should Be Visible` | `selector` | Waits for visibility. Reasons: `not-found` / `not-visible` |
| `Element Should Exist` | `selector` | Waits for DOM presence |
| `Element Should Not Exist` | `selector` | **Two-phase**: observes presence first, then waits for removal (no vacuous pass) |

### Reading state

| Keyword | Args | Returns | Description |
| --- | --- | --- | --- |
| `Get Text` | `selector` | `str` | `textContent` — polls until the element appears (does not read input values) |
| `Get Attribute` | `selector`, `name` | `str \| None` | Attribute value — polls to the element; a missing attribute is `None` (not a failure) |
| `Get Element Count` | `selector` | `int` | **Snapshot** of match count (0 is valid). To wait, precede with `Element Should Exist`. Supports all strategies |

### Frame scope

| Keyword | Args | Description |
| --- | --- | --- |
| `Switch To Frame` | `frame` | Enters frame scope — subsequent keywords run inside that frame. `frame` is resolved against the current scope: numeric = document-order index of the frame element; string = `name` attribute, then `id` attribute, then a CSS selector (matching the iframe itself). Calls stack for nested frames. Raises `FrameNotFoundError` (AssertionError in Robot) |
| `Reset Frame` | `scope=` | Pops to the parent frame. `scope=ALL` clears the stack back to the main frame. No-op on the main frame. `Go To` also resets to main |

Frame scope is implemented via the `Page.getFrameTree` frameId →
`Runtime.executionContextCreated` (`auxData.frameId`) →
`Runtime.evaluate(contextId=...)` path, injecting the bridge into the frame
document once on entry when needed. **Boundary**: only same-process
(same-site) frames are supported — cross-site iframes (**OOPIF**) render in
a separate process, expose no execution context to this session, and fail
on entry.

### JS dialogs — the arm-before-trigger contract

| Keyword | Args | Description |
| --- | --- | --- |
| `Promise Next Alert` | `action=ACCEPT\|DISMISS`, `prompt_text=None` | Reserves (**arms**) the next JS dialog (alert/confirm/prompt); returns a handle immediately. `Wait For` performs the response |
| `Wait For` | `handle`, `timeout=None` | Waits for the armed promise; returns the dialog text. Omitting `timeout` uses `Set Timeout` |
| `Handle Alert` | `action`, `*trigger`, `text=`, `prompt_text=` | One-liner — arm → run the trigger keyword → wait (+ assert `text`). The trigger is positional: `Handle Alert    ACCEPT    Click    testid:delete` |

⚠️ **Arm strictly before the trigger** — while `javascriptDialogOpening` is
open, other CDP commands on that session (including the evaluate inside
`Click`) are blocked. A dialog opened without an arm is auto-dismissed by
the backstop after `Set Timeout` elapses, leaving a warning (deadlock
prevention).

```robotframework
${handle}=    Promise Next Alert    action=ACCEPT
Click    testid:submit
${text}=     Wait For    ${handle}
Should Be Equal As Strings    ${text}    Passwords don't match
```

### File download

| Keyword | Args | Description |
| --- | --- | --- |
| `Promise Next Download` | — | Reserves the next download **completion**; returns a handle. The first call configures `Browser.setDownloadBehavior(allowAndName)` to save into `results/downloads/{suite}/{test}/` |
| `Wait For` | `handle`, `timeout=None` | With a `download-*` handle, waits for completion and returns the saved file's **full path**. Names finalize to suggestedFilename (duplicates uniquified as `name (1).ext`) |

```robotframework
${promise}=    Promise Next Download
Click    testid:download-link
${file}=      Wait For    ${promise}
File Should Exist    ${file}
```

Downloads do not block the session, so arming first is less strict than for
dialogs — but the same arm → trigger → wait convention applies.

### Real key input — Input.dispatchKeyEvent

| Keyword | Args | Description |
| --- | --- | --- |
| `Type Text` | `selector`, `text` | Focus the element, then type with **real key events** — for keydown listeners / `isTrusted`-checking apps. ASCII is typed per-key; Hangul and other non-ASCII fall back to `insertText` automatically (documented boundary). Appends (does not clear) |
| `Press Keys` | `*keys` | Special keys (`ENTER`/`TAB`/`BACKSPACE`/`ARROW_*`/…) or character sequences as real keys. Modifier chords are not provided — compose them via `Execute CDP Command    Input.dispatchKeyEvent` |

`Fill Text` is the fast JS-simulation track; `Type Text` is the real-input
(realistic) track.

### Real mouse track — Input.dispatchMouseEvent

| Keyword | Args | Description |
| --- | --- | --- |
| `Click With Real Mouse` | `selector` | scrollIntoView → real click at the element center — the `isTrusted`/occlusion path. Returns the click coordinates as `"x,y"` |
| `Click At Coordinates` | `x`, `y` | Real click at viewport coordinates — **the canvas partial-support path**. What was clicked must be recorded by the app to be verifiable |
| `Hover` | `selector` | Moves the real pointer to trigger a genuine CSS `:hover` — impossible on the fast track |
| `Drag From To` | `source`, `target` | Raw mouse drag (press → waypoint moves → release). HTML5 DnD synthesis is out of scope |

### Scrolling · file upload

| Keyword | Args | Description |
| --- | --- | --- |
| `Scroll By` | `x=0`, `y=0` | Real mouse-wheel scroll (pixels, positive = down/right). Returns the final scrollY. Virtualized grids: `Scroll By` → polling assertion |
| `Scroll To Element` | `selector` | Scrolls the element into the viewport (all strategies) |
| `Upload File` | `selector`, `*paths` | Sets files on an input directly (`DOM.setFileInputFiles`) — no dialog, `change` fires automatically. **Absolute paths**; `css=`/`testid:` strategies only |

### Tab management

| Keyword | Args | Description |
| --- | --- | --- |
| `New Tab` | `url=None` | Creates a tab, **switches to it**; returns the 0-based index. Dialog arms, downloads, and frame scope are isolated per tab |
| `Switch To Tab` | `tab` | 0-based index, or a title/URL substring (first match) |
| `Close Tab` | `tab=None` | Closes a tab (default: current). **Refuses the last tab** — use `Close Browser` for full teardown. Closing the current tab switches to the previous index |

### Cookies

| Keyword | Args | Description |
| --- | --- | --- |
| `Get Cookies` | `urls=""` | List of cookie dicts. Comma-separated URLs narrow the result. `file://` origins are special-cased by Chrome — use http(s) |
| `Set Cookie` | `url`, `name`, `value`, `expires=None`, `http_only=False`, `secure=False`, `same_site=None` | `Network.setCookie` — invalid origins fail |
| `Delete Cookie` | `name`, `url=None` | Deletes by name (optionally per origin) |
| `Delete All Cookies` | — | Deletes every cookie in the browser (browser-global) |

### Viewport / mobile emulation

| Keyword | Args | Description |
| --- | --- | --- |
| `Set Viewport Size` | `width`, `height`, `device_scale_factor=1.0`, `mobile=False` | Per-tab viewport override. `mobile=${TRUE}` also enables touch emulation. **Recommended before visual regression** |
| `Reset Viewport` | — | Clears the override and restores the default (waits for application) |

### Visual regression

| Keyword | Args | Description |
| --- | --- | --- |
| `Page Should Match Baseline` | `name`, `pixel_delta=0`, `max_mismatch_ratio=0.0`, `mask=""` | Compares the viewport screenshot against `{baseline_dir}/{name}.png`. Per-channel delta tolerance + mismatch-pixel-ratio cap. On failure, leaves `.actual.png`+`.diff.png` artifacts in `{outputdir}/visual/{suite}/{test}/` plus a **text diagnosis** (localized / global-shift / scattered — the cause family in words) |
| `Update Baseline` | `name`, `mask=""` | (Re)creates a baseline from the current screen — use the same mask as the comparison |

- `mask`: comma-separated selectors — black overlays over all matching
  elements before capture. Excludes volatile areas such as timestamps.
  All strategies + `deep:` supported. **Note**: mask boxes follow element
  rects, so wrap length-changing content in a fixed-width container
- `baseline_dir` is an import argument (default `visual-baselines/`) —
  point it at `${CURDIR}` in suites
- **Fix the viewport with `Set Viewport Size` before comparing** —
  rendering determinism depends on it
- A missing baseline fails with creation guidance. Update mode:
  `CDPBROWSER_UPDATE_BASELINES=1` env or the `Update Baseline` keyword
- **Canvas verification path**: this keyword + `Click At Coordinates`
  (no element anchors)
- Pillow is optional (`pip install 'cdpbrowser[visual]'`) — without it the
  comparison degrades to exact-match, and tolerances fail with guidance

### Recording — record & replay

| Keyword | Args | Description |
| --- | --- | --- |
| `Start Recording` | — | Records user/agent input on the current tab — manual driving of a headed browser, `Click With Real Mouse`, `Type Text`, anything producing real DOM events |
| `Stop Recording` | `name=Recorded Flow` | Stops and returns a replayable Robot script as text |
| `Save Recording` | `path`, `name=Recorded Flow`, `format=Robot\|Python` | Stops and writes the replay script (`Robot` test or plain-Python core script); returns the absolute path |

```robotframework
Start Recording
Go To    https://app.example.com/
# ...interact — by hand, or with any keywords...
${script}=    Stop Recording
Save Recording    ${CURDIR}${/}recorded.robot
```

- Recorded kinds: `navigate` → `Go To`, `click` → `Click`, typing → one
  debounced `Fill Text` per element (final value), standalone special keys →
  `Press Keys`
- Replays are resilient by construction — the generated test uses
  poll-by-default keywords, so timing differences are absorbed
- Selector derivation mirrors the bridge priorities: `testid` > unique `id` >
  `aria-label` > `[name]` > unique short text > structural path
- v1 boundaries: main frame only; dialogs/downloads during recording are
  handled by the standard backstop on replay

### Escape hatches — direct CDP/JS access

| Keyword | Args | Description |
| --- | --- | --- |
| `Run Javascript` | `expression` | Arbitrary JS evaluation — JSON-serialized return, promise awaited, page exceptions promoted. Respects frame scope |
| `Execute CDP Command` | `method`, `params_json=""` | Raw CDP command — the gateway to everything not yet keyword-ized. `Browser.*`/`Target.*` route at browser level |
| `Insert Text` | `text` | Inserts composed text into the focused element (`Input.insertText`) — **the Hangul/IME bypass path**. Focus first |

## Selector DSL

| Syntax | Strategy | Meaning |
| --- | --- | --- |
| `.nav > a` | css | No prefix → CSS selector (default) |
| `testid:submit` | testid | Matches the `data-testid` attribute |
| `text:Log in` | text | Text match (exact/partial decided by the bridge) |
| `x://button[1]` | xpath | XPath |
| `css:#id` | css | Explicit CSS escape hatch |
| `role:button Save` | role | ARIA role match — explicit `role` attribute first, then the implicit role map (button, input, select, h1..h6, …); case-insensitive exact match. The value may be two parts split on the first space (the second is the accessible name) |
| `label:User name` | label | Accessible-name match (simplified accname), case-insensitive exact |
| `deep:...` | (modifier) | Prefixes any other strategy to pierce **open shadow DOM** (opt-in) — e.g. `deep:testid:submit`, `deep:css:.btn > span`. Closed roots are inaccessible (boundary) |
| `ext:...` | ext | Resolves through an Ext JS-style component registry (`window.Ext.ComponentQuery.query`) — DOM-selector bypass anchoring on the app's stable component structure (`ext:grid[itemId=users] button[text=Save]`). Requires the framework to expose `Ext` globally; a clear error is raised otherwise |

## Support boundary

Principle: **controllable = everything the browser exposes about the page**
(DOM, accessibility tree, events, downloads/uploads, tabs, network).
**Not controllable = anything outside the page** (OS, browser chrome UI,
physical input devices).

| Level | Scope | Notes |
| --- | --- | --- |
| ✅ Full | Every web app whose elements live in the DOM/accessibility tree — static, SSR, SPA, widget frameworks, CSS-in-JS, web components | Shadow DOM via `deep:` piercing (open roots — closed is out). iframes via same-site frame scope — cross-site OOPIF unsupported |
| ⚠️ Partial | Canvas/WebGL | No element anchors. Click At Coordinates (viewport x,y) + visual assertions form the degraded mode |
| ❌ Unsupported | OS-native UI (print dialogs etc.), desktop→browser real drags, real IME composition (Hangul), anti-bot/automation detection | IME gets the `Input.insertText` bypass — **not** physical-key reproduction. Detection evasion is out of warranty |

## Framework compatibility matrix

| # | Rendering approach | Examples | Viable selectors | Status |
| --- | --- | --- | --- | --- |
| A1 | Static / SSR | JSP, PHP, Express+EJS | Semantic structure as-is | ✅ |
| A2 | SPA (vdom) | React, Vue, Angular, Svelte | testid/role/label | ✅ |
| A3 | SSR + hydration | Next, Nuxt, Remix | Same as A1+A2 | ✅ |
| A4 | Widget frameworks | Ext JS, Dojo, GWT, JSF, UI5 | text/role; auto-IDs (`ext-gen…`) are unstable | ⚠️ Partial — `ext:` registry strategy when the app exposes `Ext` |
| A5 | CSS-in-JS hashes | styled-components, emotion | Only testid/role are stable | ✅ |
| A6 | Web components / shadow DOM | Lit, Stencil, Fast | Piercing traversal needed | ✅ (`deep:` opt-in — open roots, closed is a boundary) |
| A7 | Nested iframes | Rich editors, portals | Frame scope needed | ✅ (same-site — OOPIF unsupported) |
| A8 | Canvas | Docs-style apps, web games | Coordinates + visual only | ⚠️ Partial (`Click At Coordinates` + visual regression) |

Anchor availability spectrum: **B1** explicit `data-testid` (best) → **B2**
accessibility anchors (role+name — framework-agnostic) → **B3** explicit
id / form names → **B4** stable text → **B5** structural CSS/XPath
(brittle, last resort) → **B6** auto-generated ids (`ext-gen`, `ember-N`,
`:r1:`) — **never anchor on these**.

## Evidence chain

- Every capture lands in `{outputdir}/evidence/{suite}/{test}/NNNN-{label}.png`
- `NNNN` **resets per test**; `Numbered Screenshot` and auto-capture share one sequence
- Modes (`Set Evidence Mode`): `off` / `on-failure` (default) / `every-step`

## Environment variables

| Variable | Role |
| --- | --- |
| `CDPBROWSER_CHROME_PATH` | Chrome executable path (highest priority for detection) |
| `CHROME_PATH` | Fallback Chrome path |
| `CDPBROWSER_UPDATE_BASELINES` | `1` = update visual baselines on mismatch/absence instead of failing |
| `CDPBROWSER_OUTPUT_DIR` | Overrides the outputdir used for evidence/visual artifacts |
| `CI` | Set → headless by default |

## Development guide

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,robot,visual]"
make test      # .venv/bin/pytest tests/ -q
make atest     # .venv/bin/robot --outputdir results atest/
```

- Real-Chrome tests locate the binary via `find_chrome()` and skip when absent
- Unit tests use `tests/cdp_mock.py` (a mock CDP WebSocket server)
- Packaging guards (`tests/test_packaging.py`) run the core with robotframework
  import-blocked to pin the option-B contract
- Fixtures live in `atest/fixtures/*.html`, served via `file://`

## Roadmap

* **P1** — ✅ **done**: `role:`/`label:` accessibility locators (ARIA
  role·name computation engine), iframe scope (Switch To Frame — same-site,
  OOPIF is a boundary), dialogs (Promise Next Alert / Wait For /
  Handle Alert — the arm-before-trigger contract, un-armed backstop
  auto-dismiss), downloads (Promise Next Download — allowAndName +
  suggestedFilename finalization), Get Attribute / Get Element Count
* **P2** — ✅ **done**: escape hatches (Run Javascript /
  Execute CDP Command / Insert Text — IME bypass), real key input
  (Type Text / Press Keys — Input.dispatchKeyEvent, ASCII real keys +
  Hangul insertText fallback), real mouse track (Click With Real Mouse /
  **Click At Coordinates — the canvas partial-support path** / Hover /
  Drag From To), scrolling (Scroll By real wheel / Scroll To Element),
  file upload (Upload File — change auto-fires), tab management
  (New Tab / Switch To Tab / Close Tab — per-tab state isolation),
  shadow DOM piercing (`deep:` modifier — recursive open-root traversal,
  opt-in)
* **P3** — ✅ **done**: cookies (Get/Set/Delete Cookies), viewport /
  mobile emulation (Set Viewport Size + touch emulation), visual regression
  (Page Should Match Baseline + Update Baseline — tolerance, selector
  masking, text diagnosis, canvas verification path), `ext:` framework
  registry locator
* **Recording (record & replay)** — ✅ done: `Start Recording` /
  `Stop Recording` / `Save Recording` — records real input (any driving
  style) through a CDP binding, correlates observed dialogs/downloads to
  their triggering action, and emits replayable Robot or plain-Python
  scripts; replays are resilient by construction (poll-by-default keywords)
* **Future** — external locator-registry plugin API (beyond the built-in
  `ext:`), element-scoped visual assertions, OOPIF support via child-target
  attach, iframe-interior recording

## Test coverage

The full suite: 399 pytest tests (pure units, mock-CDP-server units,
real-Chrome integrations, packaging guards, record→replay proofs) plus 28
Robot acceptance tests — measured **together** in
[docs/coverage.md](docs/coverage.md) (combined branch coverage,
layer × method matrix, gap classification, and the bug ledger from
cross-validation; a styled [HTML version](docs/coverage.html) is also
generated). Regenerate with `make coverage`.

## Verified environments

| Component | Version |
| --- | --- |
| Robot Framework | 7.4.2 |
| Python | ≥3.10 (3.12 verified) |
| Chrome | 131.0.6778.204 (Chrome for Testing) |
| websockets | 17.1 |
| Pillow (optional, `[visual]`) | 12.x |
