// Runs before the app (a classic script: the chat's CSP allows no inline script). If the app's own
// files can't load (a page or a tab from before a new build, a network hiccup), reload once; if it
// still hasn't started, fixed words and a Reload button instead of a blank page (Xavier,
// 2026-11-18: "sometimes when loading the web app the entire page blank until i refresh").
(function () {
  var KEY = 'osv-reloaded-at' // the same guard as the app's reloadOnce (ui/common.tsx)
  function fallback() {
    var root = document.getElementById('root')
    if (!root || root.childElementCount || window.__belfryStarted) return
    var box = document.createElement('div')
    box.setAttribute('role', 'alert')
    box.style.cssText = 'font:16px/1.4 system-ui,-apple-system,sans-serif;padding:64px 24px;text-align:center;color:inherit'
    var p = document.createElement('p')
    p.textContent = 'The chat could not start. Reloading usually fixes it.'
    var b = document.createElement('button')
    b.textContent = 'Reload'
    b.style.cssText = 'font:inherit;padding:10px 24px;border-radius:99px;border:1px solid currentColor;background:none;color:inherit;cursor:pointer'
    b.onclick = function () { location.reload() }
    box.appendChild(p); box.appendChild(b); root.appendChild(box)
  }
  function reloadOnce() {
    try {
      var at = Number(sessionStorage.getItem(KEY) || 0)
      if (Date.now() - at < 60000) return fallback()
      sessionStorage.setItem(KEY, String(Date.now()))
    } catch (e) { /* private mode: reload anyway */ }
    location.reload()
  }
  // a script or stylesheet of the app that didn't load (errors don't bubble: listen while capturing)
  window.addEventListener('error', function (e) {
    var t = e.target
    if (t && (t.tagName === 'SCRIPT' || t.tagName === 'LINK') && /\/assets\//.test(t.src || t.href || '')) reloadOnce()
  }, true)
  window.__belfryReloadOnce = reloadOnce
  // nothing started after 12 s: say so
  setTimeout(fallback, 12000)
})()
