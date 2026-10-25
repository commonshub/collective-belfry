// The default avatar of a participant without a picture: a mirrored 5×5 pixel pattern in one of
// five village tints on the card colour, drawn from the public key (the layout of minidenticons,
// MIT). The onboarding service draws the same picture (belfry_onboarding/avatar.py) and puts its
// PNG in the starter profile, so outside clients show it too; keep the two in step.
export const AVATAR_BACKGROUND = "#1a231d"
export const AVATAR_TINTS = ["#d6ff52", "#a8d672", "#f89b0e", "#7fc4a0", "#e8f5b0"]

/** The cells [x, y] (x 0–4, y 0–4) and the tint for a 64-hex public key. */
export function avatarPattern(pubkey) {
  const hex = /^[0-9a-f]{64}$/i.test(pubkey || "") ? pubkey.toLowerCase() : "0".repeat(64)
  const bits = parseInt(hex.slice(0, 4), 16)
  const tint = AVATAR_TINTS[parseInt(hex.slice(4, 6), 16) % AVATAR_TINTS.length]
  const cells = []
  for (let i = 0; i < 25; i++) {
    if (!(bits & (1 << i % 15))) continue
    cells.push([i > 14 ? 7 - Math.floor(i / 5) : Math.floor(i / 5), i % 5])
  }
  return { cells, tint }
}

export function avatarSvg(pubkey) {
  const { cells, tint } = avatarPattern(pubkey)
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1.5 -1.5 8 8" shape-rendering="crispEdges">` +
    `<rect x="-1.5" y="-1.5" width="8" height="8" fill="${AVATAR_BACKGROUND}"/>` +
    `<g fill="${tint}">${cells.map(([x, y]) => `<rect x="${x}" y="${y}" width="1" height="1"/>`).join("")}</g></svg>`
  )
}

const cache = new Map()
/** The default avatar as a data: URL (img-src allows data: in every village app). */
export function avatarUrl(pubkey) {
  if (!cache.has(pubkey)) cache.set(pubkey, `data:image/svg+xml;charset=utf-8,${encodeURIComponent(avatarSvg(pubkey))}`)
  return cache.get(pubkey)
}
