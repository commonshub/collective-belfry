// Replacing the page's address (no new history entry) without losing our count of where we are in
// the tab's history (ui/common.tsx back() / up() rely on history.state.i). A raw location.replace
// or history.replaceState(null, …) drops that number: the next screen was then counted one step
// too far, and ‹ went "back" to a screen that wasn't there: out of the app, or round in a loop
// (Xavier, 2026-11-18: "the back button … goes sometimes in loop").
/** Replace the address with HASH ("#/…") or URL, keeping this entry's place in the history. */
export function replaceHash(hashOrUrl: string) {
  // a bare "#/…" resolves against this page whatever its path ("//" on a test server made
  // pathname + search read as another host, and the replace threw: nav.cjs, 2026-11-19)
  const url = hashOrUrl
  const before = location.hash
  try { history.replaceState(history.state, '', url) } catch { location.replace(url); return }
  if (location.hash !== before) dispatchEvent(new HashChangeEvent('hashchange'))
}
