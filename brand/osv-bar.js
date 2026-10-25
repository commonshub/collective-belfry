/* Open Source Village app bar: a thin strip on top of every community app with the way back to
 * the apps page and links to the other apps. One file for all apps; each app's build copies it
 * next to its index.html (same origin, so a strict script-src 'self' allows it) and loads it in
 * <head>, without defer, so the offset is known before the app lays out:
 *
 *   <script src="/osv-bar.js" data-home="https://apps.<domain>"
 *           data-apps="Chat|https://chat.<domain>|…;Calendar|https://calendar.<domain>|…"></script>
 *
 * data-apps has the format of Keycast's BELFRY_COMMUNITY_APPS (Name|URL|Description;…). The bar
 * is fixed at the top and sets --osv-bar-h on <html> (with the class osv-bar); each app moves its
 * own fixed and sticky parts down by that much (the chat through Coracle's
 * --safe-area-inset-top, the calendar through its --safe-area-top) and may set --osv-bar-z to put
 * the bar above its navigation and below its dialogs (default 1250: above MUI's app bar and
 * drawers, below its modals). Colours from tokens.css. */
(function () {
  var script = document.currentScript;
  if (!script || window.__osvBar) return;
  window.__osvBar = true;

  var home = script.getAttribute("data-home") || "";
  var apps = (script.getAttribute("data-apps") || "")
    .split(";")
    .map(function (entry) {
      var parts = entry.split("|");
      return { name: (parts[0] || "").trim(), url: (parts[1] || "").trim() };
    })
    .filter(function (app) {
      return app.name && /^https?:\/\//.test(app.url);
    });

  var root = document.documentElement;
  root.classList.add("osv-bar");
  root.style.setProperty("--osv-bar-h", "calc(40px + env(safe-area-inset-top, 0px))");

  var css =
    "#osv-bar{position:fixed;top:0;left:0;right:0;z-index:var(--osv-bar-z,1250);box-sizing:border-box;" +
    "height:var(--osv-bar-h);padding:env(safe-area-inset-top,0px) max(8px,env(safe-area-inset-right,0px)) 0 max(8px,env(safe-area-inset-left,0px));" +
    "display:flex;align-items:center;gap:4px;background:#1a231d;border-bottom:1px solid #2b3a31;" +
    "font:500 14px/1 Geist,ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;" +
    "overflow-x:auto;scrollbar-width:none;-webkit-font-smoothing:antialiased}" +
    "#osv-bar::-webkit-scrollbar{display:none}" +
    "#osv-bar a{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 10px;" +
    "border-radius:8px;color:#c9d1cb;text-decoration:none;white-space:nowrap;flex-shrink:0}" +
    "#osv-bar a:hover{background:#243128;color:#fff}" +
    "#osv-bar a:focus-visible{outline:2px solid #d6ff52;outline-offset:-2px}" +
    "#osv-bar a[aria-current=page]{color:#d6ff52;background:#243128}" +
    "#osv-bar .osv-home{color:#fff;font-weight:600;margin-right:auto;padding-left:4px}" +
    "#osv-bar .osv-home svg{width:20px;height:20px;flex-shrink:0}" +
    "@media (max-width:359px){#osv-bar .osv-home span{display:none}}" +
    "@media print{#osv-bar{display:none}}";

  // An app-grid icon, inline so the bar needs no request.
  var mark =
    '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2">' +
    '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>' +
    '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5" fill="#d6ff52" stroke="#d6ff52"/></svg>';

  function link(href, className) {
    var a = document.createElement("a");
    a.href = href;
    if (className) a.className = className;
    var here;
    try {
      here = new URL(href, location.href).origin === location.origin;
    } catch (e) {
      here = false;
    }
    if (here) a.setAttribute("aria-current", "page");
    return a;
  }

  function mount() {
    if (document.getElementById("osv-bar")) return;
    var style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);

    var nav = document.createElement("nav");
    nav.id = "osv-bar";
    nav.setAttribute("aria-label", "Open Source Village apps");
    if (home) {
      var a = link(home, "osv-home");
      a.removeAttribute("aria-current");
      a.innerHTML = mark + "<span>Apps</span>";
      a.title = "All Open Source Village apps";
      nav.appendChild(a);
    }
    apps.forEach(function (app) {
      // the feedback page learns which page the feedback is about
      var feedback = /\/feedback\/?$/.test(app.url);
      var a = link(feedback ? app.url + "?from=" + encodeURIComponent(location.href) : app.url);
      if (feedback) a.removeAttribute("aria-current");
      a.textContent = app.name;
      nav.appendChild(a);
    });
    document.body.insertBefore(nav, document.body.firstChild);
    // On a phone the list scrolls sideways: bring the current app into view.
    // (after layout, and again once fonts and styles have loaded and widths changed)
    var current = nav.querySelector("[aria-current=page]");
    function reveal() {
      if (current && current.offsetLeft + current.offsetWidth > nav.clientWidth) {
        nav.scrollLeft = current.offsetLeft - 48;
      }
    }
    if (current) {
      requestAnimationFrame(reveal);
      window.addEventListener("load", reveal);
    }
  }

  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
