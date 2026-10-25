// Invite page: join with a Nostr key one already has. The visitor's signer (a NIP-07 browser
// extension) shares the public key when the box is ticked and signs a NIP-98 event for this page
// when the form is sent; the server checks it (models/nostr_proof.py). No private key ever
// reaches this page.
(function () {
  "use strict"
  const box = document.getElementById("belfry_own_key")
  const form = document.getElementById("belfry_invite_form")
  if (!box || !form) return
  const pubkeyInput = document.getElementById("belfry_nostr_pubkey")
  const proofInput = document.getElementById("belfry_nostr_proof")
  const status = document.getElementById("belfry_own_key_status")
  const say = (text) => { status.textContent = text }

  const BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
  function npub(hex) {
    const data = []
    let acc = 0, bits = 0
    for (let i = 0; i < hex.length; i += 2) {
      acc = ((acc << 8) | parseInt(hex.slice(i, i + 2), 16)) & 0xfff
      bits += 8
      while (bits >= 5) { bits -= 5; data.push((acc >> bits) & 31) }
    }
    if (bits) data.push((acc << (5 - bits)) & 31)
    const gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    const polymod = (values) => {
      let chk = 1
      for (const v of values) {
        const top = chk >>> 25
        chk = (((chk & 0x1ffffff) << 5) ^ v) >>> 0
        for (let i = 0; i < 5; i++) if ((top >> i) & 1) chk = (chk ^ gen[i]) >>> 0
      }
      return chk
    }
    const hrp = "npub"
    const expanded = [...hrp].map((c) => c.charCodeAt(0) >> 5).concat([0], [...hrp].map((c) => c.charCodeAt(0) & 31))
    const mod = polymod(expanded.concat(data, [0, 0, 0, 0, 0, 0])) ^ 1
    const checksum = [0, 1, 2, 3, 4, 5].map((i) => (mod >>> (5 * (5 - i))) & 31)
    return hrp + "1" + data.concat(checksum).map((d) => BECH32[d]).join("")
  }
  const short = (n) => n.slice(0, 14) + "…" + n.slice(-6)

  function reset(text) {
    box.checked = false
    pubkeyInput.value = ""
    proofInput.value = ""
    say(text || "")
  }

  box.addEventListener("change", async () => {
    if (!box.checked) return reset()
    if (!window.nostr) {
      return reset("No Nostr signer was found in this browser. Install a signer extension, or join " +
        "without one: you can move to your own key later on your account page.")
    }
    try {
      const pubkey = await window.nostr.getPublicKey()
      if (!/^[0-9a-f]{64}$/.test(pubkey)) throw new Error("not a public key")
      pubkeyInput.value = pubkey
      say("Your key: " + short(npub(pubkey)) + ". Your signer asks you to sign when you send the form.")
    } catch {
      reset("Your signer did not share a key.")
    }
  })

  form.addEventListener("submit", async (event) => {
    if (!box.checked || form.dataset.signed) return
    event.preventDefault()
    const pubkey = pubkeyInput.value
    if (!pubkey || !window.nostr) return reset("Tick the box again to connect your signer.")
    try {
      const signed = await window.nostr.signEvent({
        kind: 27235,
        created_at: Math.floor(Date.now() / 1000),
        tags: [["u", location.origin + location.pathname], ["method", "POST"]],
        content: "Join Open Source Village with this key",
        pubkey,
      })
      if (!signed || signed.pubkey !== pubkey) return reset("Your signer signed with another key. Please try again.")
      proofInput.value = JSON.stringify(signed)
      form.dataset.signed = "1"
      form.submit()
    } catch {
      say("Signing was cancelled. Send the form again to sign, or untick the box to join without your own key.")
    }
  })
})()
