// Open Source Village branding for the cashu.me build (run in the source tree before the build):
// app name and colours in the PWA manifest and index.html, the home-screen icons from
// brand/apps/wallet-* (copied to public/belfry/ by the Dockerfile) and the Open Source Village
// app bar (brand/osv-bar.js). Args: the apps page URL and the app list (Name|URL|Description;...).
import {readFileSync, writeFileSync} from "node:fs"

const [home = "", apps = ""] = process.argv.slice(2)
const BG = "#2b3a31"

const manifest = JSON.parse(readFileSync("src-pwa/manifest.json", "utf8"))
Object.assign(manifest, {
  name: "Open Source Village wallet",
  short_name: "Wallet",
  description: "Your Open Source Village tokens: receive them and pass them on.",
  background_color: BG,
  theme_color: BG,
  icons: [
    {src: "belfry/wallet-192.png", sizes: "192x192", type: "image/png", purpose: "any"},
    {src: "belfry/wallet-512.png", sizes: "512x512", type: "image/png", purpose: "any"},
    {src: "belfry/wallet-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable"},
  ],
})
writeFileSync("src-pwa/manifest.json", JSON.stringify(manifest, null, 2) + "\n")

const pkg = JSON.parse(readFileSync("package.json", "utf8"))
pkg.productName = "Open Source Village wallet"
pkg.description = "Your Open Source Village tokens"
writeFileSync("package.json", JSON.stringify(pkg, null, 2) + "\n")

const esc = s => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;")
let html = readFileSync("index.html", "utf8")
html = html.replaceAll("#000000", BG)
const head = [
  `<link rel="apple-touch-icon" href="belfry/wallet-180.png" />`,
  `<link rel="stylesheet" href="/belfry/fonts.css" />`,
  `<script src="/osv-bar.js" data-home="${esc(home)}" data-apps="${esc(apps)}"></script>`,
].join("\n    ")
if (!html.includes("</head>")) throw new Error("index.html has no </head>")
html = html.replace("</head>", `    ${head}\n  </head>`)
writeFileSync("index.html", html)
