/*
 * cdpbrowser page bridge.
 *
 * Injected automatically into every new document via
 * Page.addScriptToEvaluateOnNewDocument; global pollution is limited to the
 * single window.__cdpb property. Every function runs synchronously and
 * returns only JSON-serializable values, so Runtime.evaluate
 * (returnByValue=true) results come back into Python as-is.
 *
 * The locator argument matches the shape of locators.py's to_bridge_arg()
 * output, {"s": "css"|"id"|"t"|"x", "v": string}:
 *   css — document.querySelector
 *   id  — exact match on the data-testid attribute (compared by iteration
 *         to avoid value-escaping issues)
 *   t   — text matching: on whitespace-normalized textContent, exact match
 *         first, then a case-insensitive substring fallback. With nesting
 *         (button>span), prefers the element with the shortest text (the
 *         leaf-most one).
 *   x   — XPath via document.evaluate FIRST_ORDERED_NODE
 *   r   — role matching: explicit role attribute first, then the implicit
 *         role map, case-insensitive exact match, first in document order.
 *         The value splits into role/name at the first space, as in
 *         "button" or "button Save" (name is a case-insensitive exact match
 *         against the accessible name).
 *   l   — label matching: case-insensitive exact match against the
 *         accessible name (simplified accname), first in document order.
 *
 * The accessible name is a **simplified accname** — aria-label →
 * aria-labelledby → label[for] → wrapping label → name-from-content
 * (button/link only) → placeholder → img alt → title, in that order.
 * Boundaries: aria-describedby, recursion exclusion for purely decorative
 * elements, and name-from-content extensions (headings etc.) are not
 * implemented (the full two-pass accname algorithm is out of scope).
 *
 * Atomic primitives (click/setValue) run the find→visible→enabled check in
 * a single step and return {"ok": true} or
 * {"ok": false, "reason": ...[, "describe"]}. reason is "not-found" |
 * "not-visible" | "not-enabled"; describe is failure diagnostics attached
 * only when the element exists.
 */
(function () {
  "use strict";

  var TESTID_ATTR = "data-testid";

  // Non-display elements excluded from text matching.
  var SKIP_TAGS = {
    SCRIPT: true,
    STYLE: true,
    HEAD: true,
    TITLE: true,
    META: true,
    LINK: true,
    NOSCRIPT: true,
    TEMPLATE: true,
  };

  function normText(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/\s+/g, " ")
      .trim();
  }

  // Direct text: collect only the element's own text nodes and normalize (for failure diagnostics).
  function directText(el) {
    var text = "";
    for (var node = el.firstChild; node; node = node.nextSibling) {
      if (node.nodeType === 3) {
        text += node.nodeValue;
      }
    }
    return normText(text);
  }

  function allElements() {
    var root = document.body || document.documentElement;
    if (!root || !root.querySelectorAll) {
      return [];
    }
    return root.querySelectorAll("*");
  }

  // deep: modifier — pierce open shadow DOM (opt-in).
  // Scans the document root plus every open shadowRoot found recursively.
  // closed shadowRoots are unreachable in the first place (boundary).
  function deepRoots() {
    var roots = [document];
    var queue = [document];
    while (queue.length) {
      var current = queue.shift();
      var elements;
      try {
        elements = current.querySelectorAll("*");
      } catch (err) {
        continue;
      }
      for (var i = 0; i < elements.length; i++) {
        var sr = elements[i].shadowRoot;
        if (sr) {
          roots.push(sr);
          queue.push(sr);
        }
      }
    }
    return roots;
  }

  function deepElements() {
    var roots = deepRoots();
    var pool = [];
    for (var i = 0; i < roots.length; i++) {
      var body = roots[i].body || roots[i];
      var list;
      try {
        list = body.querySelectorAll("*");
      } catch (err) {
        continue;
      }
      for (var j = 0; j < list.length; j++) {
        pool.push(list[j]);
      }
    }
    return pool;
  }

  function findByTestId(value) {
    var candidates = document.querySelectorAll("[" + TESTID_ATTR + "]");
    for (var i = 0; i < candidates.length; i++) {
      if (candidates[i].getAttribute(TESTID_ATTR) === value) {
        return candidates[i];
      }
    }
    return null;
  }

  function findByXPath(expression) {
    var result = document.evaluate(
      expression,
      document,
      null,
      XPathResult.FIRST_ORDERED_NODE_TYPE,
      null
    );
    var node = result.singleNodeValue;
    return node && node.nodeType === 1 ? node : null;
  }

  function findByText(value, pool) {
    var wanted = normText(value);
    if (!wanted) {
      return null;
    }
    var lower = wanted.toLowerCase();
    var elements = pool || allElements();
    var bestExact = null;
    var bestExactLen = 0;
    var bestPartial = null;
    var bestPartialLen = 0;
    for (var i = 0; i < elements.length; i++) {
      var el = elements[i];
      if (SKIP_TAGS[el.tagName]) {
        continue;
      }
      var text = normText(el.textContent);
      if (!text) {
        continue;
      }
      if (text === wanted) {
        // Exact match: prefer the element with the shortest text (closest to
        // the leaf), ties broken by document order first. Stable when parent
        // and child share the same text (button>span).
        if (bestExact === null || text.length < bestExactLen) {
          bestExact = el;
          bestExactLen = text.length;
        }
      } else if (text.toLowerCase().indexOf(lower) !== -1) {
        if (bestPartial === null || text.length < bestPartialLen) {
          bestPartial = el;
          bestPartialLen = text.length;
        }
      }
    }
    return bestExact !== null ? bestExact : bestPartial;
  }

  // ----------------------------------------------------------------------
  // ARIA — implicit role + simplified accessible name (see boundary note above)
  // ----------------------------------------------------------------------

  function implicitRole(el) {
    switch (el.tagName) {
      case "A":
        return el.hasAttribute("href") ? "link" : null;
      case "BUTTON":
        return "button";
      case "INPUT": {
        var type = (el.getAttribute("type") || "text").toLowerCase();
        if (type === "checkbox") return "checkbox";
        if (type === "radio") return "radio";
        if (type === "submit" || type === "button") return "button";
        if (type === "hidden") return null;
        return "textbox";
      }
      case "SELECT":
        return "combobox";
      case "TEXTAREA":
        return "textbox";
      case "H1":
      case "H2":
      case "H3":
      case "H4":
      case "H5":
      case "H6":
        return "heading";
      case "UL":
        return "list";
      case "LI":
        return "listitem";
      case "NAV":
        return "navigation";
      case "MAIN":
        return "main";
      case "TABLE":
        return "table";
      case "IMG":
        return "img";
      case "FORM":
        return "form";
      default:
        return null;
    }
  }

  // Effective role list: explicit role attribute tokens first, then the implicit role.
  function effectiveRoles(el) {
    var roles = [];
    var explicit = el.getAttribute("role");
    if (explicit) {
      var tokens = normText(explicit).toLowerCase().split(" ");
      for (var i = 0; i < tokens.length; i++) {
        if (tokens[i] && roles.indexOf(tokens[i]) === -1) {
          roles.push(tokens[i]);
        }
      }
    }
    var implicit = implicitRole(el);
    if (implicit && roles.indexOf(implicit) === -1) {
      roles.push(implicit);
    }
    return roles;
  }

  // Tags where name-from-content applies (simplified: button/link only).
  function hasNameFromContent(el) {
    var tag = el.tagName;
    return tag === "BUTTON" || (tag === "A" && el.hasAttribute("href"));
  }

  function attributeValueSelector(attr, value) {
    // Escape the attribute value string — CSS.escape is for identifiers, unfit for quoted values.
    var escaped = value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    return "[" + attr + '="' + escaped + '"]';
  }

  function accessibleName(el) {
    var label = normText(el.getAttribute("aria-label"));
    if (label) {
      return label;
    }
    var labelledby = normText(el.getAttribute("aria-labelledby"));
    if (labelledby) {
      var parts = [];
      var ids = labelledby.split(" ");
      for (var i = 0; i < ids.length; i++) {
        var target = document.getElementById(ids[i]);
        if (target) {
          parts.push(target.textContent || "");
        }
      }
      var joined = normText(parts.join(" "));
      if (joined) {
        return joined;
      }
    }
    if (el.id) {
      var forLabel = document.querySelector(
        "label" + attributeValueSelector("for", el.id)
      );
      var forText = forLabel ? normText(forLabel.textContent) : "";
      if (forText) {
        return forText;
      }
    }
    // Wrapping label. Exclude the case where the element itself is a label —
    // closest("label") returns the element itself, so without the exclusion
    // a <label> element would shadow its associated input and findByLabel
    // would return the label instead of the form control.
    var wrapping =
      el.closest && el.tagName !== "LABEL" ? el.closest("label") : null;
    var wrappingText = wrapping ? normText(wrapping.textContent) : "";
    if (wrappingText) {
      return wrappingText;
    }
    if (hasNameFromContent(el)) {
      var content = normText(el.textContent);
      if (content) {
        return content;
      }
    }
    var placeholder = normText(el.getAttribute("placeholder"));
    if (placeholder) {
      return placeholder;
    }
    if (el.tagName === "IMG") {
      var alt = normText(el.getAttribute("alt"));
      if (alt) {
        return alt;
      }
    }
    return normText(el.getAttribute("title"));
  }

  function matchesName(el, wantedName) {
    return accessibleName(el).toLowerCase() === wantedName.toLowerCase();
  }

  function findByRole(value, pool) {
    var elements = pool || allElements();
    var spec = normText(value);
    if (!spec) {
      return null;
    }
    var sep = spec.indexOf(" ");
    var wantedRole = (sep === -1 ? spec : spec.slice(0, sep)).toLowerCase();
    var wantedName = sep === -1 ? null : spec.slice(sep + 1);
    for (var i = 0; i < elements.length; i++) {
      var el = elements[i];
      if (SKIP_TAGS[el.tagName]) {
        continue;
      }
      if (effectiveRoles(el).indexOf(wantedRole) === -1) {
        continue;
      }
      if (wantedName !== null && !matchesName(el, wantedName)) {
        continue;
      }
      return el;
    }
    return null;
  }

  function findByLabel(value, pool) {
    var wanted = normText(value);
    if (!wanted) {
      return null;
    }
    var elements = pool || allElements();
    for (var i = 0; i < elements.length; i++) {
      var el = elements[i];
      if (SKIP_TAGS[el.tagName]) {
        continue;
      }
      var name = accessibleName(el);
      if (name && name.toLowerCase() === wanted.toLowerCase()) {
        return el;
      }
    }
    return null;
  }

  // Locator resolution. Unknown strategies/empty values are treated as "not
  // found" (null). Invalid CSS/XPath syntax is intentionally rethrown as-is
  // — the browser's SyntaxError must surface as exceptionDetails so it stays
  // diagnosable from Python.
  // ext: strategy — looks up framework components from the registry via Ext
  // JS ComponentQuery and returns the rendered DOM (el.dom). Bypasses DOM
  // selectors, anchoring on the app code's stable component structure
  // (itemId/reference/text configs). Throws an explicit error when Ext is
  // absent rather than failing silently with not-found.
  function extComponents(selector) {
    if (
      typeof window.Ext !== "object" ||
      window.Ext === null ||
      !window.Ext.ComponentQuery ||
      typeof window.Ext.ComponentQuery.query !== "function"
    ) {
      throw new Error(
        "ext: selector requires Ext JS — window.Ext.ComponentQuery not found"
      );
    }
    var matches = window.Ext.ComponentQuery.query(String(selector)) || [];
    var rendered = [];
    for (var i = 0; i < matches.length; i++) {
      var comp = matches[i];
      var dom =
        (comp.el && comp.el.dom) ||
        (comp.element && comp.element.dom) ||
        null; // classic: el.dom / modern: element.dom
      if (dom && dom.nodeType === 1) {
        rendered.push(dom);
      }
    }
    return rendered;
  }

  function findByExt(value) {
    var all = extComponents(value);
    return all.length ? all[0] : null;
  }

  function resolve(locator) {
    if (!locator || typeof locator !== "object") {
      return null;
    }
    if (locator.p) {
      return resolveDeep(locator);
    }
    var value = locator.v;
    if (typeof value !== "string") {
      return null;
    }
    switch (locator.s) {
      case "css":
        return document.querySelector(value);
      case "id":
        return findByTestId(value);
      case "t":
        return findByText(value);
      case "x":
        return findByXPath(value);
      case "r":
        return findByRole(value);
      case "l":
        return findByLabel(value);
      case "e":
        return findByExt(value);
      default:
        return null;
    }
  }

  function isVisible(el) {
    if (!el || typeof el.getBoundingClientRect !== "function") {
      return false;
    }
    var style = window.getComputedStyle(el);
    if (
      style.display === "none" ||
      style.visibility === "hidden" ||
      style.visibility === "collapse"
    ) {
      return false;
    }
    var box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  }

  // :disabled covers both form controls and controls under a disabled
  // fieldset. Every other element never matches :disabled, hence enabled=true.
  function isEnabled(el) {
    return !el.matches(":disabled");
  }

  function state(el) {
    return { visible: isVisible(el), enabled: isEnabled(el) };
  }

  function status(locator) {
    var el = resolve(locator);
    if (!el) {
      return null;
    }
    var s = state(el);
    return { exists: true, visible: s.visible, enabled: s.enabled };
  }

  function describe(el) {
    return {
      tag: el.tagName.toLowerCase(),
      text: directText(el),
      testid: el.getAttribute(TESTID_ATTR),
      id: el.id || null,
      classes: Array.prototype.slice.call(el.classList || []),
    };
  }

  function failure(el, reason) {
    var result = { ok: false, reason: reason };
    if (el) {
      result.describe = describe(el);
    }
    return result;
  }

  // Atomic precondition check: find→visible→enabled. On failure, returns a result carrying the reason.
  function checkActionable(locator) {
    var el = resolve(locator);
    if (!el) {
      return { el: null, error: failure(null, "not-found") };
    }
    var s = state(el);
    if (!s.visible) {
      return { el: el, error: failure(el, "not-visible") };
    }
    if (!s.enabled) {
      return { el: el, error: failure(el, "not-enabled") };
    }
    return { el: el, error: null };
  }

  function click(locator) {
    var check = checkActionable(locator);
    if (check.error) {
      return check.error;
    }
    check.el.click();
    return { ok: true };
  }

  // React-compatible value setting: the prototype native setter must be used
  // so React's value tracking hook (trigger) fires. input/change events are
  // bubbled afterwards.
  function setElementValue(el, value) {
    var stringValue = value === null || value === undefined ? "" : String(value);
    var proto = null;
    if (typeof HTMLInputElement !== "undefined" && el instanceof HTMLInputElement) {
      proto = HTMLInputElement.prototype;
    } else if (
      typeof HTMLTextAreaElement !== "undefined" &&
      el instanceof HTMLTextAreaElement
    ) {
      proto = HTMLTextAreaElement.prototype;
    }
    var applied = false;
    if (proto) {
      var descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor && typeof descriptor.set === "function") {
        descriptor.set.call(el, stringValue);
        applied = true;
      }
    }
    if (!applied) {
      try {
        el.value = stringValue;
      } catch (err) {
        // Element without a value property — still proceed through event dispatch.
      }
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setValue(locator, value) {
    var check = checkActionable(locator);
    if (check.error) {
      return check.error;
    }
    setElementValue(check.el, value);
    return { ok: true };
  }

  function getText(locator) {
    var el = resolve(locator);
    if (!el) {
      return { ok: false, reason: "not-found" };
    }
    var text = el.textContent;
    return { ok: true, text: text === null || text === undefined ? "" : String(text) };
  }

  function getAttr(locator, name) {
    var el = resolve(locator);
    if (!el) {
      return { ok: false, reason: "not-found" };
    }
    var value = el.getAttribute(String(name));
    return { ok: true, value: value === null ? null : String(value) };
  }

  // Multi-match across all strategies. For count snapshots only — unlike
  // findOne's tiebreak (shortest text first, etc.), returns the full
  // document-order list as-is.
  // A ShadowRoot is ineligible as a context node for document.evaluate
  // (#document-fragment). Inside a shadow root, the first child element is
  // used as the context and "//x" expressions are rewritten to the relative
  // ".//x". Absolute paths starting with "/" are only meaningful from the
  // document root, so they are skipped inside shadows.
  function evaluateXPathInRoot(expression, root, resultType) {
    var context = root;
    var expr = expression;
    if (root !== document) {
      context = root.firstElementChild;
      if (!context) {
        return null;
      }
      if (expression.indexOf("//") === 0) {
        expr = "." + expression;
      } else if (expression.charAt(0) === "/") {
        return null;
      }
    }
    try {
      return document.evaluate(expr, context, null, resultType, null);
    } catch (err) {
      throw err; // syntax errors propagate as-is (same contract as shallow)
    }
  }

  // deep: modifier lookup — sweeps the document plus every open shadowRoot
  // in document order. closed roots are unreachable (boundary). CSS/XPath
  // syntax errors are rethrown as-is, same as shallow, to stay diagnosable.
  function resolveDeep(locator) {
    var value = locator.v;
    if (typeof value !== "string") {
      return null;
    }
    switch (locator.s) {
      case "css":
        var roots = deepRoots();
        for (var i = 0; i < roots.length; i++) {
          var hit = roots[i].querySelector(value);
          if (hit) {
            return hit;
          }
        }
        return null;
      case "id":
        var poolId = deepElements();
        for (var a = 0; a < poolId.length; a++) {
          if (poolId[a].getAttribute(TESTID_ATTR) === value) {
            return poolId[a];
          }
        }
        return null;
      case "t":
        return findByText(value, deepElements());
      case "x":
        var rootsX = deepRoots();
        for (var b = 0; b < rootsX.length; b++) {
          var result = evaluateXPathInRoot(
            value,
            rootsX[b],
            XPathResult.FIRST_ORDERED_NODE_TYPE
          );
          var node = result && result.singleNodeValue;
          if (node && node.nodeType === 1) {
            return node;
          }
        }
        return null;
      case "r":
        return findByRole(value, deepElements());
      case "l":
        return findByLabel(value, deepElements());
      case "e":
        return findByExt(value);
      default:
        return null;
    }
  }

  function resolveAllDeep(locator) {
    var value = locator.v;
    var out = [];
    var roots = deepRoots();
    switch (locator.s) {
      case "css":
        for (var i = 0; i < roots.length; i++) {
          out = out.concat(
            Array.prototype.slice.call(roots[i].querySelectorAll(value))
          );
        }
        return out;
      case "x":
        for (var j = 0; j < roots.length; j++) {
          var snap = evaluateXPathInRoot(
            value,
            roots[j],
            XPathResult.ORDERED_NODE_SNAPSHOT_TYPE
          );
          if (!snap) {
            continue;
          }
          for (var k = 0; k < snap.snapshotLength; k++) {
            var node = snap.snapshotItem(k);
            if (node && node.nodeType === 1) {
              out.push(node);
            }
          }
        }
        return out;
      default:
        // id/t/r/l — flatten the deep pool in document order, apply the same predicate.
        return resolveAll({
          s: locator.s,
          v: value,
          p: 0,
          __deepPool: deepElements(),
        });
    }
  }

  function resolveAll(locator) {
    if (!locator || typeof locator !== "object") {
      return [];
    }
    if (locator.p) {
      return resolveAllDeep(locator);
    }
    var value = locator.v;
    if (typeof value !== "string") {
      return [];
    }
    var out = [];
    switch (locator.s) {
      case "css":
        return Array.prototype.slice.call(document.querySelectorAll(value));
      case "id":
        var candidates = locator.__deepPool || document.querySelectorAll(
          "[" + TESTID_ATTR + "]"
        );
        for (var i = 0; i < candidates.length; i++) {
          if (candidates[i].getAttribute(TESTID_ATTR) === value) {
            out.push(candidates[i]);
          }
        }
        return out;
      case "x":
        var snap = document.evaluate(
          value,
          document,
          null,
          XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
          null
        );
        for (var j = 0; j < snap.snapshotLength; j++) {
          var node = snap.snapshotItem(j);
          if (node && node.nodeType === 1) {
            out.push(node);
          }
        }
        return out;
      case "t":
        var wanted = normText(value);
        var lowerT = wanted.toLowerCase();
        var elementsT = locator.__deepPool || allElements();
        for (var k = 0; k < elementsT.length; k++) {
          var elT = elementsT[k];
          if (SKIP_TAGS[elT.tagName]) {
            continue;
          }
          var textT = normText(elT.textContent);
          if (!textT) {
            continue;
          }
          if (textT === wanted || textT.toLowerCase().indexOf(lowerT) !== -1) {
            out.push(elT);
          }
        }
        return out;
      case "r":
        var spec = normText(value);
        var sepR = spec.indexOf(" ");
        var wantedRole = (sepR === -1 ? spec : spec.slice(0, sepR)).toLowerCase();
        var wantedNameR = sepR === -1 ? null : spec.slice(sepR + 1);
        var elementsR = locator.__deepPool || allElements();
        for (var m = 0; m < elementsR.length; m++) {
          var elR = elementsR[m];
          if (SKIP_TAGS[elR.tagName]) {
            continue;
          }
          if (effectiveRoles(elR).indexOf(wantedRole) === -1) {
            continue;
          }
          if (wantedNameR !== null && !matchesName(elR, wantedNameR)) {
            continue;
          }
          out.push(elR);
        }
        return out;
      case "l":
        var wantedL = normText(value).toLowerCase();
        var elementsL = locator.__deepPool || allElements();
        for (var n = 0; n < elementsL.length; n++) {
          var elL = elementsL[n];
          if (SKIP_TAGS[elL.tagName]) {
            continue;
          }
          var nameL = accessibleName(elL);
          if (nameL && nameL.toLowerCase() === wantedL) {
            out.push(elL);
          }
        }
        return out;
      case "e":
        return extComponents(value);
      default:
        return [];
    }
  }

  function count(locator) {
    var n = resolveAll(locator).length;
    return { count: n };
  }

  function focus(locator) {
    var el = resolve(locator);
    if (!el) {
      return { ok: false, reason: "not-found" };
    }
    if (typeof el.focus !== "function") {
      return { ok: false, reason: "not-focusable" };
    }
    el.focus();
    return { ok: true };
  }

  function rect(locator) {
    var el = resolve(locator);
    if (!el) {
      return { ok: false, reason: "not-found" };
    }
    var r = el.getBoundingClientRect();
    return {
      ok: true,
      x: r.left,
      y: r.top,
      width: r.width,
      height: r.height,
    };
  }

  function scrollIntoView(locator) {
    var el = resolve(locator);
    if (!el) {
      return { ok: false, reason: "not-found" };
    }
    if (typeof el.scrollIntoViewIfNeeded === "function") {
      el.scrollIntoViewIfNeeded(true);
    } else if (typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "center", inline: "center" });
    }
    return { ok: true };
  }

  // Visual regression masking — lays a fixed black overlay over each matched
  // element. Using the same mask on both baseline and actual excludes
  // volatile content (timestamps etc.) from the comparison. position:fixed
  // does not trigger reflow.
  var MASK_CLASS = "__cdpb_mask__";

  function mask(locators) {
    if (!Array.isArray(locators)) {
      locators = [locators];
    }
    var count = 0;
    for (var i = 0; i < locators.length; i++) {
      var matches = resolveAll(locators[i]);
      for (var j = 0; j < matches.length; j++) {
        var el = matches[j];
        var r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) {
          continue; // hidden elements have no surface to mask.
        }
        var box = document.createElement("div");
        box.className = MASK_CLASS;
        box.setAttribute("data-testid", "__cdpb_mask__");
        box.style.position = "fixed";
        box.style.left = r.left + "px";
        box.style.top = r.top + "px";
        box.style.width = r.width + "px";
        box.style.height = r.height + "px";
        box.style.background = "#000";
        box.style.zIndex = "2147483647";
        document.documentElement.appendChild(box);
        count++;
      }
    }
    return { ok: true, masked: count };
  }

  function unmask() {
    var all = document.querySelectorAll("." + MASK_CLASS);
    for (var i = 0; i < all.length; i++) {
      all[i].remove();
    }
    return { ok: true, removed: all.length };
  }

  window.__cdpb = {
    findOne: resolve,
    _state: state,
    _describe: describe,
    exists: status,
    visible: status,
    enabled: status,
    click: click,
    setValue: setValue,
    getText: getText,
    getAttr: getAttr,
    focus: focus,
    rect: rect,
    scrollIntoView: scrollIntoView,
    mask: mask,
    unmask: unmask,
    count: count,
  };
})();
