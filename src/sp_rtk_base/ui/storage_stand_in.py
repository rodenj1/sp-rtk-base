"""A stand-in for ``sessionStorage`` and ``localStorage`` in a sandboxed frame.

The Portal shows a base inside ``<iframe sandbox>`` without
``allow-same-origin`` (ADR 0004), so the page has an opaque origin and
merely reading ``window.sessionStorage`` or ``window.localStorage`` throws
a ``SecurityError``.  NiceGUI's client reads ``sessionStorage`` on load,
so without a stand-in the page never starts.

The head script replaces each one, only when reading it throws, with an
in-memory object of the same shape that lasts as long as the page.  A base
opened directly (``:8080``) can read both, so it keeps the real ones.
"""

from __future__ import annotations

STORAGE_STAND_IN = """<script>
(function () {
  function standIn(name) {
    try { void window[name]; return; } catch (e) {}
    var d = Object.create(null);
    var s = {
      getItem: function (k) { return k in d ? d[k] : null; },
      setItem: function (k, v) { d[k] = String(v); },
      removeItem: function (k) { delete d[k]; },
      clear: function () { d = Object.create(null); },
      key: function (i) { var ks = Object.keys(d); return i < ks.length ? ks[i] : null; },
      get length() { return Object.keys(d).length; }
    };
    var p = new Proxy(s, {
      get: function (t, k) { return k in t ? t[k] : d[k]; },
      set: function (t, k, v) { d[k] = String(v); return true; },
      deleteProperty: function (t, k) { delete d[k]; return true; }
    });
    Object.defineProperty(window, name, { value: p, configurable: true });
  }
  standIn('sessionStorage');
  standIn('localStorage');
})();
</script>"""
"""Head script, added to every page (before NiceGUI's own scripts run)."""
