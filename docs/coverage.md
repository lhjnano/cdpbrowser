# CdpBrowser — Test Coverage Heatmap

Layer × method matrix · combined pytest + Robot branch coverage · gap classification & cross-validation findings.

- **14** modules measured — **87%** combined branch coverage
- **28** Robot acceptance tests passing
- **11** bugs found via cross-validation (each pinned by a regression test)

> Regenerate with `make coverage` (runs pytest and the Robot suite under coverage, combines, and renders this file plus `docs/coverage.html`).

## 1. Module × test-method matrix

| Module | unit-pure | unit-mock | real-chrome | robot-atest |
| --- | :-: | :-: | :-: | :-: |
| `cdp/transport.py` | · | ✅ | ✅ | ✅ |
| `cdp/chrome.py` | ✅ | · | ✅ | ✅ |
| `cdp/errors.py` | ✅ | ✅ | ✅ | — |
| `locators.py` | ✅ | · | ✅ | ✅ |
| `keys.py` | ✅ | · | ✅ | ✅ |
| `polling.py` | ✅ | · | ✅ | ✅ |
| `promises.py` | · | ✅ | ✅ | ✅ |
| `page.py` | ◐ | ◐ | ✅ | ✅ |
| `bridge.js` | · | · | ✅ | ✅ |
| `recorder.js` | · | · | ✅ | · |
| `recorder.py` | ✅ | · | ✅ | ✅ |
| `library.py` | ◐ | ✅ | ✅ | ✅ |
| `listener.py` | ◐ | · | · | ✅ |
| `visual.py` | ✅ | · | ✅ | ✅ |

✅ exercised · ◐ partial · · not used by that method · — n/a.
`bridge.js` / `recorder.js` have no line coverage of their own — they are exercised through the Python layers that inject them.

## 2. Combined branch coverage (pytest + Robot under coverage)

| Module | Stmts | Miss | Branches | Partial | Cover |
| --- | ---: | ---: | ---: | ---: | :-: |
| `__init__.py` | 24 | 5 | 4 | 1 | 🔴 79% |
| `listener.py` | 43 | 5 | 4 | 1 | 🔴 83% |
| `cdp/chrome.py` | 258 | 38 | 92 | 17 | 🔴 84% |
| `page.py` | 830 | 100 | 264 | 69 | 🔴 84% |
| `cdp/transport.py` | 183 | 25 | 34 | 5 | 🟡 86% |
| `library.py` | 541 | 54 | 134 | 17 | 🟡 89% |
| `promises.py` | 155 | 10 | 44 | 9 | 🟡 90% |
| `visual.py` | 133 | 8 | 46 | 6 | 🟡 91% |
| `recorder.py` | 90 | 5 | 46 | 6 | 🟡 92% |
| `polling.py` | 48 | 1 | 20 | 1 | 🟢 97% |
| `locators.py` | 62 | 1 | 28 | 1 | 🟢 98% |
| `cdp/__init__.py` | 4 | 0 | 0 | 0 | 🟢 100% |
| `cdp/errors.py` | 15 | 0 | 2 | 0 | 🟢 100% |
| `keys.py` | 28 | 0 | 12 | 0 | 🟢 100% |

The Robot suite runs under coverage as well, so lines exercised only through keywords are counted.

## 3. Remaining gaps — classified, with reasons

| Class | Where | Why it is not covered / why that is acceptable |
| --- | --- | --- |
| ✅ filled | `__init__.py guards` | Robot-missing import guards verified by subprocess tests (test_packaging.py) — isolated processes, so their lines are outside the combined measurement by design. |
| 🟡 env-constrained | `chrome.py kill fallbacks (SIGKILL path, stderr diagnostics 408-441)` | Forcing a Chrome process to ignore SIGTERM deterministically is not reproducible in CI; covered by defensive coding + manual verification. |
| 🟡 env-constrained | `chrome.py needs_no_sandbox under root` | Requires euid 0; CI never runs as root. Pure function is unit-tested for non-root. |
| ⬜ defensive-only | `transport.py close-error branches (234-264)` | Unsubscribes/cleanups when the loop is already dead — defensive paths that only trigger on transport-internal races. |
| ⬜ defensive-only | `promises.py dispatcher matcher/response exception swallowing` | Logged-not-raised paths; the error-completion contract itself IS tested (test_gapfill_core). |
| ⬜ defensive-only | `library.py evidence artifact fallbacks (940-953, 1087)` | Artifact-save failures must never flip a verdict — deliberately quiet, exercised only with a poisoned filesystem. |
| ⬜ subprocess-isolated | `__init__.py / CdpBrowser.py shim guard lines` | The guards run in blocked-robot subprocesses (test_packaging.py); coverage of subprocesses is intentionally not wired. |
| ⬜ defensive-only | `page.py frame-context race retries (1386-1430)` | Short race windows after scope entry — polling retries whose happy path is covered; the retry branch needs an artificially slowed browser. |
| 🟡 env-constrained | `listener.py failure-capture paths (90-112)` | Only reachable when a Robot test FAILS with a live browser; atest is green by definition. Verified manually by breaking a suite once. |

## 4. Bugs found through cross-validation

Defects found while building the suite. Each row is pinned by a regression test.

| Area | Bug | Symptom → cause | Fix |
| --- | --- | --- | --- |
| Recorder | **Runtime.addBinding was callable but silent** | bindingCalled events are only delivered after Runtime.enable — recording saw nothing until the domain was enabled at start. | `start_recording sends Runtime.enable first` |
| Recorder | **Arm flag died across navigation** | window.__cdpbRecOn was set on the current document only; the first recorded navigation silently stopped recording. | `flag registered as its own tracked new-document script, removed on stop` |
| Recorder | **Duplicate fill after ENTER** | The change event fired on Enter-commit re-emitted the identical fill. | `lastEmittedFill key dedupe in flushFill` |
| Recorder | **Robot cell quoting corrupted values** | Values with spaces were wrapped in double quotes — Robot keeps quotes literal, so replays filled "Jane Doe" with quotes. | `escape only Robot syntax characters ($ @ & # \), never quote` |
| Recorder | **Programmatic blob-anchor double recording** | downloadBlob helpers click a transient <a download> — the synthetic click was recorded with a dead structural selector. | `recorder skips blob:/data: download anchors` |
| Dialogs | **clearBrowserCoins absent at browser endpoint** | Network.clearBrowserCookies exists only via page session on Chrome 131 — browser-level send raised -32601. | `routed through the session` |
| Downloads | **Broker session binding hid browser-level events** | Browser.downloadProgress arrives without sessionId; a session-bound broker arm never matched. | `download broker is created unbound` |
| Shadow DOM | **ShadowRoot has no evaluate()** | Deep XPath first called root.evaluate (absent), then document.evaluate with a #document-fragment context (NotSupportedError). | `context = root.firstElementChild + // → .// rewrite` |
| Scrolling | **Wheel applied asynchronously** | Reading scrollY right after dispatch saw the pre-scroll value — one-event-stale reads. | `scroll_by polls briefly for the metrics to settle` |
| Keys | **Special keys ignored (rawKeyDown) + snake_case wire fields** | Chrome ignores keyDown without text for special keys, and the CDP wire needs camelCase field names. | `down-type rule + transport-boundary rename` |
| Dialogs | **Lazy coordinator lost un-armed dialogs** | Dialog subscription was created on first arm — dialogs opened without one were lost and the backstop never fired. | `coordinator starts eagerly at attach + prime()` |
