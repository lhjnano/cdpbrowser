# CdpBrowser

CdpBrowser is a browser automation library that speaks the Chrome DevTools
Protocol directly from Python. It has no driver binaries, no Node runtime,
and no Selenium. The core is framework independent; a Robot Framework
keyword adapter is available as an optional extra.

```bash
pip install cdpbrowser                  # core only
pip install "cdpbrowser[robot]"         # Robot Framework keywords
pip install "cdpbrowser[visual]"        # Pillow, for visual regression
pip install "cdpbrowser[dev,robot,visual]"  # development
```

Without robotframework installed, `cdpbrowser.CdpBrowser` raises an
`ImportError` that names the `[robot]` extra. The core (`PageSession` and
the modules under `cdpbrowser.*`) needs no extras.

## Design

- **Direct CDP.** One background asyncio loop per library, one WebSocket to
  Chrome. Nothing in between.
- **Poll by default.** Interaction and assertion keywords retry their
  condition until the deadline set by `Set Timeout`. A missing element is a
  retry, not an error.
- **Atomic bridge.** bridge.js runs find, visibility, and enabled checks in
  a single step inside the browser. Python receives plain JSON.
- **Evidence.** Screenshots accumulate under
  `{outputdir}/evidence/{suite}/{test}/` with per-test sequence numbers. A
  listener saves the last screen on failure.
- **Optional adapter.** The core imports nothing from Robot. Only
  `library.py` and `listener.py` do.

## Quick start, Robot Framework

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

Chrome launches lazily on the first keyword. `headless` defaults to `auto`:
headless when `CI` is set or `DISPLAY` is missing. `--no-sandbox` is added
under root, in detected CI environments, or when
`CDPBROWSER_NO_SANDBOX=1`; `--disable-dev-shm-usage` is added when
`/dev/shm` is under 1 GiB.

### Import arguments

| Argument | Default | Description |
| --- | --- | --- |
| `chrome_path` | auto-detected | Chrome executable path |
| `headless` | `auto` | `True`, `False`, or `auto` |
| `timeout` | `5s` | Default polling deadline (Robot time string) |
| `poll_interval` | `0.1s` | Polling retry interval |
| `baseline_dir` | `visual-baselines/` | Root for visual-regression baselines |

## Using the core from Python

The core is a synchronous Python API. The test suite drives it without
Robot everywhere.

```python
from cdpbrowser import ChromeProcess, CdpConnection, PageSession, find_chrome

chrome = ChromeProcess(find_chrome(), headless=True)
chrome.start()
try:
    session = PageSession(CdpConnection(chrome.ws_url)).attach()
    session.navigate("https://example.com")

    session.call("click", {"s": "id", "v": "submit"})   # testid: selector
    text = session.call("getText", {"s": "id", "v": "result"})

    handle = session.arm_download()
    session.call("click", {"s": "id", "v": "download-link"})
    saved = session.wait_download(handle, 10.0)

    # Frames, viewport, cookies, and real key/mouse input are also plain
    # methods. See the PageSession docstrings.
finally:
    session.close()
    chrome.stop()
```

For waits, `cdpbrowser.polling.poll_until` retries any callable until a
deadline. It is the same primitive the Robot adapter uses.

## Concepts

### Dialogs: arm before the trigger

While a JS dialog (alert, confirm, prompt) is open, the CDP session blocks
other commands, including the evaluation inside `Click`. Reserve the dialog
first, then trigger it, then collect the result:

```robotframework
${handle}=    Promise Next Alert    action=ACCEPT
Click    testid:submit
${text}=     Wait For    ${handle}
Should Be Equal As Strings    ${text}    Passwords don't match
```

A dialog opened without an arm is dismissed automatically after
`Set Timeout` elapses, with a warning. This prevents deadlocks.

`Handle Alert` combines the three steps for clicks:
`Handle Alert    ACCEPT    Click    testid:delete`.

### Downloads

Downloads do not block the session, so the order matters less, but the
convention is the same:

```robotframework
${promise}=    Promise Next Download
Click    testid:download-link
${file}=      Wait For    ${promise}
File Should Exist    ${file}
```

The first `Promise Next Download` configures Chrome to save under
`results/downloads/{suite}/{test}/`. `Wait For` returns the saved file's
full path.

### Fast and real input tracks

`Click` and `Fill Text` simulate input in JavaScript. `Type Text`,
`Press Keys`, and the mouse keywords send real input events through CDP.
Use the real track when the application checks `isTrusted` or listens for
keydown. ASCII goes through per-key events; non-ASCII such as Hangul falls
back to `Input.insertText` and is documented as such.

### Visual regression

`Page Should Match Baseline` compares the viewport against a committed PNG.
Tolerances are a per-channel delta (`pixel_delta`) and a mismatch-ratio cap
(`max_mismatch_ratio`). Failures write `.actual.png` and `.diff.png`
artifacts and describe the change in words: one localized region, a whole
image shift, or scattered changes. Fix the viewport with
`Set Viewport Size` before comparing. Without Pillow the comparison is
exact-match only.

### Recording and replay

`Start Recording` captures real input on the current tab: manual driving,
real-mouse keywords, or any keyword that produces DOM events. Typing
collapses into one fill per element. Observed dialogs and downloads are
attached to the action that triggered them.

```robotframework
Start Recording
Go To    https://app.example.com/
# interact, by hand or with keywords
Save Recording    ${CURDIR}${/}recorded.robot
```

The generated Robot test uses poll-by-default keywords, so replays absorb
timing differences. `Save Recording` with `format=Python` writes a
plain-Python replay against the core instead. Recording covers the main
frame only.

## Keyword reference

Unless noted, interaction and assertion keywords poll until the deadline.

### Session

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Open Browser` | | Explicit launch. No-op when already open. |
| `Close Browser` | | Closes all tabs, the connection, and Chrome. Idempotent. |
| `Go To` | `url` | Navigates, waits for load, returns the final URL. |
| `Get Title` | | `document.title` |
| `Get Current Url` | | `location.href` |

### Interaction

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Click` | `selector` | Atomic find, visibility, and enabled checks, then click. |
| `Fill Text` | `selector`, `text` | Native setter plus input and change events. |
| `Type Text` | `selector`, `text` | Real key events. Appends; does not clear. Non-ASCII uses `insertText`. |
| `Press Keys` | `*keys` | Special keys (`ENTER`, `TAB`, `ARROW_*`, ...) as real events. |
| `Click With Real Mouse` | `selector` | Scrolls into view, clicks the center with real events. Returns `"x,y"`. |
| `Click At Coordinates` | `x`, `y` | Real click at viewport coordinates. For canvas. |
| `Hover` | `selector` | Real pointer move. Triggers CSS `:hover`. |
| `Drag From To` | `source`, `target` | Real press, move, release. No HTML5 drag events. |
| `Scroll By` | `x=0`, `y=0` | Real wheel scroll in pixels. Returns the final scrollY. |
| `Scroll To Element` | `selector` | Scrolls the element into the viewport. |
| `Upload File` | `selector`, `*paths` | Sets files directly with `DOM.setFileInputFiles`. Absolute paths; `css=` or `testid:` selectors only. |

### Assertions

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Element Should Be Visible` | `selector` | Waits for visibility. |
| `Element Should Exist` | `selector` | Waits for DOM presence. |
| `Element Should Not Exist` | `selector` | Confirms presence first, then waits for removal. |

### Reading state

| Keyword | Arguments | Returns | Description |
| --- | --- | --- | --- |
| `Get Text` | `selector` | `str` | `textContent`. Does not read input values. |
| `Get Attribute` | `selector`, `name` | `str` or `None` | A missing attribute is `None`, not a failure. |
| `Get Element Count` | `selector` | `int` | Snapshot. Zero is valid; precede with `Element Should Exist` to wait. |

### Frames

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Switch To Frame` | `frame` | Numeric index, `name`, `id`, or a CSS selector for the iframe. Calls stack. |
| `Reset Frame` | `scope=` | Pops one level, or all with `scope=ALL`. |

Same-site frames only. Cross-site iframes (OOPIF) run in a separate process
and have no execution context in this session.

### Dialogs and downloads

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Promise Next Alert` | `action`, `prompt_text=` | Arms the next dialog. Returns a handle. |
| `Promise Next Download` | | Arms the next download completion. Returns a handle. |
| `Wait For` | `handle`, `timeout=` | Waits for an armed promise. Returns the dialog text or the saved file path. |
| `Handle Alert` | `action`, `*trigger`, `text=` | Arm, run trigger keyword, wait, and optionally assert the text. |

### Tabs

| Keyword | Arguments | Description |
| --- | --- | --- |
| `New Tab` | `url=` | Creates a tab, switches to it, returns the 0-based index. |
| `Switch To Tab` | `tab` | 0-based index, or a title or URL substring. |
| `Close Tab` | `tab=` | Default current. Refuses the last tab; use `Close Browser`. |

Dialog arms, downloads, and frame scope are isolated per tab.

### Cookies

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Get Cookies` | `urls=` | Comma-separated URLs narrow the result. Use http(s) origins. |
| `Set Cookie` | `url`, `name`, `value`, ... | Optional `expires`, `http_only`, `secure`, `same_site`. |
| `Delete Cookie` | `name`, `url=` | Deletes by name, optionally per origin. |
| `Delete All Cookies` | | Browser-global. |

### Viewport

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Set Viewport Size` | `width`, `height`, `device_scale_factor=1.0`, `mobile=False` | Per tab. `mobile=True` enables touch emulation. |
| `Reset Viewport` | | Clears the override. |

### Visual regression

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Page Should Match Baseline` | `name`, `pixel_delta=0`, `max_mismatch_ratio=0.0`, `mask=` | Compares against `{baseline_dir}/{name}.png`. |
| `Update Baseline` | `name`, `mask=` | Recreates a baseline. Use the same mask as the comparison. |

`mask` takes comma-separated selectors and blacks out matching elements
before capture, for volatile areas such as timestamps. Mask boxes follow
element rects, so keep length-changing content in a fixed-width container.
Set `CDPBROWSER_UPDATE_BASELINES=1` to rewrite baselines instead of
failing.

### Recording

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Start Recording` | | Records real input on the current tab. |
| `Stop Recording` | `name=` | Stops and returns a Robot replay script. |
| `Save Recording` | `path`, `name=`, `format=Robot` | Stops and writes the script. `format=Python` for a core script. |

### Escape hatches

| Keyword | Arguments | Description |
| --- | --- | --- |
| `Run Javascript` | `expression` | Evaluation with JSON return and awaited promises. Respects frame scope. |
| `Execute CDP Command` | `method`, `params_json=` | Raw CDP. `Browser.*` and `Target.*` route at browser level. |
| `Insert Text` | `text` | `Input.insertText` into the focused element. The IME bypass. |

## Selectors

| Syntax | Strategy | Description |
| --- | --- | --- |
| `.nav > a` | css | Default when unprefixed. |
| `testid:submit` | testid | Matches `data-testid`. |
| `text:Log in` | text | Exact match first, then case-insensitive partial. |
| `x://button[1]` | xpath | XPath. |
| `css:#id` | css | Explicit prefix, for values that collide with the reserved ones. |
| `role:button Save` | role | Explicit `role` attribute, then implicit roles. Optional accessible name after the first space. Case-insensitive. |
| `label:User name` | label | Accessible name, simplified accname. Case-insensitive. |
| `deep:...` | modifier | Pierces open shadow DOM. Opt-in. Closed roots are inaccessible. |
| `ext:...` | ext | Ext JS ComponentQuery when `window.Ext` is exposed. DOM-independent. |

Selector stability, best to worst: explicit `data-testid`, accessibility
anchors, explicit id or form name, stable text, structural CSS or XPath.
Never anchor on generated ids such as `ext-gen123` or `:r1:`.

## Scope

Controllable: what the browser exposes about the page, meaning the DOM,
accessibility tree, events, downloads, uploads, tabs, and network. Not
controllable: the OS, browser chrome, and physical devices.

| Level | Scope |
| --- | --- |
| Full | Apps whose elements live in the DOM or accessibility tree, including shadow DOM via `deep:` and same-site iframes. |
| Partial | Canvas and WebGL. No element anchors; use `Click At Coordinates` with visual assertions. |
| Unsupported | OS-native dialogs, desktop-to-browser drags, real IME composition (use `Insert Text`), anti-bot evasion. |

## Evidence

Captures land in `{outputdir}/evidence/{suite}/{test}/NNNN-{label}.png`.
Numbering resets per test and is shared between `Numbered Screenshot` and
automatic captures. `Set Evidence Mode` selects `off`, `on-failure`
(default), or `every-step`.

## Environment variables

| Variable | Effect |
| --- | --- |
| `CDPBROWSER_CHROME_PATH` | Chrome path, highest detection priority. |
| `CHROME_PATH` | Fallback path. |
| `CDPBROWSER_NO_SANDBOX` | `1` forces `--no-sandbox`. |
| `CDPBROWSER_DISABLE_DEV_SHM` | `1` forces `--disable-dev-shm-usage`. |
| `CDPBROWSER_UPDATE_BASELINES` | `1` rewrites visual baselines instead of failing. |
| `CDPBROWSER_OUTPUT_DIR` | Overrides the artifact outputdir. |
| `CI` | Set means headless by default. |

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,robot,visual]"

make test        # pytest
make atest       # robot acceptance suite
make coverage    # combined branch coverage plus docs/coverage.md
```

Real-Chrome tests find the binary through `find_chrome()` and skip when
absent. Unit tests use a mock CDP WebSocket server (`tests/cdp_mock.py`).
Packaging guards run the core with robotframework import-blocked. Robot
fixtures are plain HTML files under `atest/fixtures/`.

## Status and coverage

Implemented: the full keyword set above, including recording and replay,
visual regression, and the `ext:` registry locator. Planned: an external
locator-registry plugin API, element-scoped visual assertions, OOPIF
support via child-target attach, iframe-interior recording.

The suite is 403 pytest tests plus 28 Robot acceptance tests, measured
together at 87% branch coverage. Details, including the gap classification
and a ledger of bugs found during development, are in
[docs/coverage.md](docs/coverage.md). An HTML rendering of the same report
sits at [docs/coverage.html](docs/coverage.html).

## Verified environments

| Component | Version |
| --- | --- |
| Python | 3.10 or newer; 3.12 verified |
| Robot Framework | 7.4.2 |
| websockets | 17.1 |
| Chrome | Chrome for Testing 131 locally, Chrome stable 152 on CI |
| Pillow, optional | 12.x |
