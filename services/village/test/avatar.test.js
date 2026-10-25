// The default avatar: the same pattern as the onboarding service's PNG (belfry_onboarding/avatar.py).
import assert from "node:assert/strict"
import { test } from "node:test"
import { avatarPattern, avatarSvg, avatarUrl } from "../web/shared/avatar.js"

const KEY = "a3f1" + "07" + "00".repeat(29)

test("pattern matches the server's vector", () => {
  const { cells, tint } = avatarPattern(KEY)
  assert.equal(tint, "#f89b0e")
  // the same vector is checked in services/onboarding/tests/test_avatar.py
  assert.deepEqual(cells, [
    [0, 0], [0, 4], [1, 0], [1, 1], [1, 2], [1, 3], [1, 4], [2, 3],
    [4, 0], [4, 4], [3, 0], [3, 1], [3, 2], [3, 3], [3, 4],
  ])
})

test("svg and data URL", () => {
  assert.match(avatarSvg(KEY), /^<svg [^>]*viewBox="-1.5 -1.5 8 8"/)
  assert.match(avatarSvg(KEY.toUpperCase()), /fill="#f89b0e"/)
  assert.ok(avatarUrl(KEY).startsWith("data:image/svg+xml"))
  assert.equal(avatarPattern("not a key").cells.length, 0)
})
