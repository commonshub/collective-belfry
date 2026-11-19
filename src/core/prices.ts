// A ticket's price (kind 30402 tags) and the words for it; no other modules, so tools/vectors/check.mjs
// runs it against vectors/ticket-prices.json (the server's price_of is the reference).
/** A ticket's price (NIP-99 ["price", amount, currency]; Xavier, 2026-11-18): an ISO 4217 code (EUR),
 *  HOUR for time, or the host's own short label. No tag: free. A priced ticket is never self-served:
 *  people ask, the hosts mark it paid and admit them (the belfry agent's contract, belfry-sh). */
export interface Price { amount: string; currency: string; /** VAT rate in %, money only */ vat?: string; vatIncluded?: boolean; /** unix: this price ends then (or the next starts) */ until?: number }
/** One price of a ticket: without dates (["price", …]) or for a while (["price_at", amount, CUR, from, until]). */
export interface PriceRow { amount: string; currency: string; from?: number; until?: number }
export const PRICE_AMOUNT = /^\d{1,7}(\.\d{1,2})?$/
export const PRICE_LABEL = /^[\p{L}\p{N} ]{1,12}$/u
/** The words for a price (the server's Ticket.price_text, vectors/ticket-prices.json): "€15.50",
 *  "2 hours", "20 USD", "3 Time credits"; "Paid" for a price that can't be read. */
export function priceLabel(p: Price): string {
  if (!p.amount) return 'Paid'
  if (p.currency === 'EUR') return '€' + p.amount
  if (p.currency === 'HOUR') return `${p.amount} ${p.amount === '1' ? 'hour' : 'hours'}`
  return `${p.amount} ${p.currency}`
}
export const isMoney = (currency: string) => /^[A-Z]{3}$/.test(currency)
export const VAT_RATE = /^\d{1,2}(\.\d{1,2})?$/
/** "€50 + 21% VAT", "€50 incl. 21% VAT", "2 hours". */
export const priceFull = (p: Price) => priceLabel(p) + (p.vat && isMoney(p.currency) ? (p.vatIncluded ? ` incl. ${p.vat}% VAT` : ` + ${p.vat}% VAT`) : '')
const dayFmt = (t: number) => new Date(t * 1000).toLocaleDateString([], { day: 'numeric', month: 'long', year: 'numeric' })
/** Under a ticket's name: "€50 + 21% VAT · until 1 January 2027", or "Not on sale now". */
export const priceLine = (t: { price?: Price; priced?: boolean; onSale?: boolean }) =>
  t.priced && t.onSale === false ? 'Not on sale now' : t.price ? priceFull(t.price) + (t.price.until ? ` · until ${dayFmt(t.price.until)}` : '') : ''
const CURRENCY = /^[A-Za-z0-9][A-Za-z0-9 ]{0,11}$/
/** A readable [amount, currency] (the server's _amount): "007.10" → "7.10", "12.5" → "12.50", "eur" → "EUR", "hours" → "HOUR". */
function readAmount(raw = '', cur = ''): [string, string] | undefined {
  raw = raw.trim(); cur = cur.split(/\s+/).filter(Boolean).join(' ')
  if (!/^\d{1,7}(\.\d{1,2})?$/.test(raw) || !CURRENCY.test(cur)) return undefined
  const [whole, frac0 = ''] = raw.split('.')
  const frac = frac0.replace(/0+$/, '')
  const amount = String(Number(whole)) + (frac ? '.' + frac.padEnd(2, '0') : '')
  if (['HOUR', 'HOURS'].includes(cur.toUpperCase())) cur = 'HOUR'
  else if (/^[A-Za-z]{3}$/.test(cur)) cur = cur.toUpperCase()
  return [amount, cur]
}
const ts = (x = '') => (/^\d{9,11}$/.test(x) ? Number(x) : undefined)

/** What a ticket costs now: a port of the server's price_of (docs/checkin.md "Ticket prices"; checked
 *  against vectors/ticket-prices.json). The first dated price whose time it is, else the plain one;
 *  no price tag or an amount of zero now = free; a tag that can't be read still counts as priced
 *  (price.amount empty: "Paid"), so a ticket meant to be paid is never handed out for free; dated
 *  prices and none of them now, with no plain price = not on sale. */
export function pricesOf(tags: string[][], now = Date.now() / 1000): { price?: Price; priced: boolean; prices: PriceRow[]; onSale: boolean } {
  const base = tags.find((t) => t[0] === 'price')
  const dated = tags.filter((t) => t[0] === 'price_at')
  if (!base && !dated.length) return { priced: false, prices: [], onSale: true }
  let unreadable = false
  const rows: PriceRow[] = []
  for (const t of dated) {
    const got = readAmount(t[1], t[2])
    const start = (t[3] ?? '').trim(), end = (t[4] ?? '').trim()
    if (!got || (start && ts(start) === undefined) || (end && ts(end) === undefined)) { unreadable = true; continue }
    rows.push({ amount: got[0], currency: got[1], from: ts(start), until: ts(end) })
  }
  const plain = base ? readAmount(base[1], base[2]) : undefined
  if (base && !plain) unreadable = true
  if (plain) rows.push({ amount: plain[0], currency: plain[1] })
  if (unreadable) return { price: { amount: '', currency: '' }, priced: true, prices: rows, onSale: true }
  const cur = rows.find((r) => (r.from === undefined || r.from <= now) && (r.until === undefined || now < r.until))
  if (!cur) return { priced: true, prices: rows, onSale: false } // dated prices only, none of them now
  if (Number(cur.amount) === 0) return { priced: false, prices: rows, onSale: true }
  let until = cur.until
  if (until === undefined && plain && cur === rows[rows.length - 1]) {
    // the plain price holds until the next dated one starts
    const later = rows.slice(0, -1).map((r) => r.from).filter((f): f is number => f !== undefined && f > now)
    until = later.length ? Math.min(...later) : undefined
  }
  const price: Price = { amount: cur.amount, currency: cur.currency, until }
  const vat = tags.find((t) => t[0] === 'vat')
  if (vat && /^[A-Z]{3}$/.test(cur.currency)) {
    const rate = (vat[1] ?? '').trim()
    if (/^\d{1,2}(\.\d{1,2})?$/.test(rate)) { price.vat = rate.includes('.') ? rate.replace(/0+$/, '').replace(/\.$/, '') : rate; price.vatIncluded = vat[2] === 'included' }
  }
  return { price, priced: true, prices: rows, onSale: true }
}
