// Adds the Open Source Village app bar (brand/osv-bar.js, copied to public/) to index.html.
// Args: the apps page URL and the app list (Name|URL|Description;...), HTML-escaped here.
import {readFileSync, writeFileSync} from "node:fs"

const [home = "", apps = ""] = process.argv.slice(2)
const esc = s => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;")
const tag = `<script src="/osv-bar.js" data-home="${esc(home)}" data-apps="${esc(apps)}"></script>`
const html = readFileSync("index.html", "utf8")
if (!html.includes("</head>")) throw new Error("index.html has no </head>")
writeFileSync("index.html", html.replace("</head>", `  ${tag}\n  </head>`))
