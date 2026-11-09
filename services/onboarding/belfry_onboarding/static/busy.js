// Every form on the belfry's pages: a tap is answered at once and can't be sent twice. The
// submit button locks, dims and says it is working; it comes back if the page is shown again
// (the back button) or nothing happened after 20 seconds.
(function () {
  var style = document.createElement('style');
  style.textContent = 'button[aria-busy=true],input[aria-busy=true]{opacity:.6;pointer-events:none;cursor:progress}' +
    'button[aria-busy=true]::before{content:"";display:inline-block;width:.8em;height:.8em;margin-right:.5em;vertical-align:-.08em;' +
    'border:2px solid currentColor;border-top-color:transparent;border-radius:99px;animation:belfry-spin .8s linear infinite}' +
    '@keyframes belfry-spin{to{transform:rotate(360deg)}}' +
    'button:active,.btn:active,.button:active{opacity:.8}';
  document.head.appendChild(style);
  function free(form) {
    form.removeAttribute('data-busy');
    form.querySelectorAll('[aria-busy=true]').forEach(function (b) { b.removeAttribute('aria-busy'); });
  }
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !form.querySelectorAll) return;
    if (form.hasAttribute('data-busy')) { e.preventDefault(); e.stopPropagation(); return; }
    // after the page's own handlers: one that cancelled the submit and works by itself is left alone
    setTimeout(function () {
      if (e.defaultPrevented) return;
      form.setAttribute('data-busy', '');
      var hit = e.submitter || form.querySelector('button:not([type=button]),input[type=submit]');
      if (hit) hit.setAttribute('aria-busy', 'true');
      setTimeout(function () { free(form); }, 20000);
    }, 0);
  }, true);
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-busy]').forEach(free);
  });
})();
