// Recorder — turns real user/agent input into replayable events.
//
// Injected alongside bridge.js (Page.addScriptToEvaluateOnNewDocument +
// one manual evaluation for the current document). The page pushes events
// through the CDP binding `cdpbRecordEvent` (Runtime.addBinding), which the
// Python side receives as Runtime.bindingCalled events.
//
// Design constraints:
// - Single global (window.__cdpbRec), inert until __cdpbRecOn is true.
// - Main frame only (window.top === window) — v1 boundary; iframe documents
//   stay inert.
// - Selector derivation mirrors the bridge priorities: testid > id >
//   aria-label > [name] > unique short text > structural css path.
// - Input uses a 400ms debounce: a burst of keystrokes on one element
//   collapses into a single "fill" with the final value (blur also flushes).
// - Only standalone special keys become "press" events; regular typing is
//   covered by the debounced fill.
(function () {
  "use strict";

  var GLOBAL = "__cdpbRec";
  if (window[GLOBAL]) {
    return; // already installed
  }

  var SPECIAL_KEYS = {
    Enter: "ENTER",
    Tab: "TAB",
    Escape: "ESCAPE",
    Backspace: "BACKSPACE",
    Delete: "DELETE",
    ArrowUp: "ARROW_UP",
    ArrowDown: "ARROW_DOWN",
    ArrowLeft: "ARROW_LEFT",
    ArrowRight: "ARROW_RIGHT",
    Home: "HOME",
    End: "END",
    PageUp: "PAGE_UP",
    PageDown: "PAGE_DOWN",
  };

  function emit(event) {
    try {
      window.cdpbRecordEvent(JSON.stringify(event));
    } catch (err) {
      // Binding unavailable (not recording, cross-origin quirk) — drop.
    }
  }

  function normText(el) {
    return (el.textContent || "").replace(/\s+/g, " ").trim();
  }

  function uniqueCount(selector) {
    try {
      return document.querySelectorAll(selector).length;
    } catch (err) {
      return 0;
    }
  }

  function escapeCss(value) {
    return (window.CSS && CSS.escape) ? CSS.escape(value) : value;
  }

  // Structural path fallback: tag + nth-of-type chain, capped depth.
  function structuralPath(el) {
    var parts = [];
    var node = el;
    var depth = 0;
    while (node && node.nodeType === 1 && depth < 4) {
      var tag = node.tagName.toLowerCase();
      if (tag === "html" || tag === "body") {
        parts.unshift(tag);
        break;
      }
      var parent = node.parentElement;
      var nth = 1;
      if (parent) {
        var same = parent.children;
        for (var i = 0; i < same.length; i++) {
          if (same[i] === node) break;
          if (same[i].tagName === node.tagName) nth++;
        }
        parts.unshift(tag + ":nth-of-type(" + nth + ")");
      } else {
        parts.unshift(tag);
      }
      node = parent;
      depth++;
    }
    return parts.join(" > ");
  }

  // Element -> best replayable selector (bridge priority order).
  function describe(el) {
    if (!el || el.nodeType !== 1) return null;

    var testid = el.getAttribute("data-testid");
    if (testid) return { strategy: "testid", value: testid };

    var id = el.getAttribute("id");
    if (id && uniqueCount("#" + escapeCss(id)) === 1) {
      return { strategy: "css", value: "#" + escapeCss(id) };
    }

    var aria = el.getAttribute("aria-label");
    if (aria) return { strategy: "label", value: aria };

    var name = el.getAttribute("name");
    if (name && uniqueCount('[name="' + name + '"]') === 1) {
      return { strategy: "css", value: '[name="' + name + '"]' };
    }

    // Unique short text (buttons/links) — the recorder CAN afford the
    // uniqueness scan, unlike one-shot bridge lookups.
    var text = normText(el);
    if (text && text.length <= 40) {
      var candidates = [text, text.toLowerCase()];
      for (var c = 0; c < candidates.length; c++) {
        var found = [];
        try {
          found = Array.prototype.slice.call(document.querySelectorAll("a,button"));
        } catch (err) {
          found = [];
        }
        var matches = found.filter(function (n) {
          return normText(n) === candidates[c];
        });
        if (matches.length === 1 && matches[0] === el) {
          return { strategy: "text", value: text };
        }
      }
    }

    var path = structuralPath(el);
    if (path) return { strategy: "css", value: path };
    return null;
  }

  function selectorString(anchor) {
    return anchor ? anchor.strategy + ":" + anchor.value : null;
  }

  // ---- Debounced fill ----------------------------------------------------
  var pendingFill = null; // { element, selector, value, timer }
  var lastEmittedFill = null; // "selector\x00value" — dedupe change-after-commit

  function flushFill() {
    if (!pendingFill) return;
    clearTimeout(pendingFill.timer);
    var key = pendingFill.selector + "\x00" + pendingFill.value;
    var event = {
      type: "fill",
      selector: pendingFill.selector,
      value: pendingFill.value,
    };
    pendingFill = null;
    if (key === lastEmittedFill) {
      return; // e.g. a change event right after Enter committed the value
    }
    lastEmittedFill = key;
    emit(event);
  }

  function noteFill(el) {
    var anchor = describe(el);
    if (!anchor) return;
    var selector = selectorString(anchor);
    var value = el.value === undefined ? "" : String(el.value);
    if (pendingFill && pendingFill.element === el) {
      pendingFill.selector = selector;
      pendingFill.value = value; // same element — merge the burst
      clearTimeout(pendingFill.timer);
      pendingFill.timer = setTimeout(flushFill, 400);
      return;
    }
    flushFill(); // a different element — flush the previous one first
    pendingFill = {
      element: el,
      selector: selector,
      value: value,
      timer: setTimeout(flushFill, 400),
    };
  }

  // ---- Listeners (capture phase) -----------------------------------------
  function onClick(event) {
    if (!window.__cdpbRecOn) return;
    if (event.target && event.target.closest && event.target.closest("[data-cdpb-rec-ignore]")) {
      return;
    }
    // Skip programmatic download anchors (blob:/data: hrefs): downloadBlob
    // -style helpers click a transient <a download> — replaying that click
    // is both impossible (the node is gone) and unnecessary (the download
    // observation already decorates the real trigger action).
    var anchor = event.target && event.target.closest && event.target.closest("a[download]");
    if (anchor && /^(blob:|data:)/.test(anchor.href || "")) {
      return;
    }
    flushFill();
    var el = event.target;
    // Prefer the actionable ancestor (e.g. label wrapping an input).
    if (el && el.closest) {
      var actionable = el.closest("a,button,[role=button],input,select,textarea");
      if (actionable) el = actionable;
    }
    var anchor = describe(el);
    if (!anchor) return;
    emit({ type: "click", selector: selectorString(anchor) });
  }

  function onInput(event) {
    if (!window.__cdpbRecOn) return;
    var el = event.target;
    if (!el || el.nodeType !== 1) return;
    var tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      noteFill(el);
    }
  }

  function onBlur(event) {
    if (!window.__cdpbRecOn) return;
    if (pendingFill && event.target === pendingFill.element) {
      flushFill();
    }
  }

  function onKeyDown(event) {
    if (!window.__cdpbRecOn) return;
    var mapped = SPECIAL_KEYS[event.key];
    if (!mapped) return; // regular typing -> debounced fill
    flushFill();
    emit({ type: "press", key: mapped });
  }

  function install() {
    // Main frame only (v1 boundary): iframe documents stay inert —
    // replaying cross-frame selectors would need frame-scope reconstruction.
    if (window.top !== window) {
      return;
    }
    document.addEventListener("click", onClick, true);
    document.addEventListener("input", onInput, true);
    document.addEventListener("change", onInput, true);
    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("blur", onBlur, true);
  }

  window[GLOBAL] = {
    install: install,
    flush: flushFill,
    describe: describe,
  };

  install();
})();
