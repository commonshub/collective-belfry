// Village notes: serves the page and passes its API calls on to the Belfry.
//
// The Belfry's JSON APIs (pass.<domain>/api/*, /wallet/pass-tokens, the shop's invite API) only
// answer pages of origins it lists (API_CORS_ORIGINS), so the browser calls them through
// /proxy?url=<public URL>. The request goes out unchanged, Authorization included: the NIP-98
// events name the public URL in their "u" tag. Relays and the mint are reached directly.
//
//   BELFRY_DOMAIN=belfry.opensourcevillage.org PORT=8080 npm start

import http from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as esbuild from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const DOMAIN = process.env.BELFRY_DOMAIN || "belfry.opensourcevillage.org";
const PORT = Number(process.env.PORT || 8080);

/** What /.well-known/belfry.json would say, from the usual host names: for Belfries deployed
 *  before discovery existed. */
function conventional(domain) {
  const pass = `https://pass.${domain}`;
  return {
    belfry: 1,
    name: domain,
    domain,
    pass,
    shop: `https://shop.${domain}`,
    api: {
      me: `${pass}/api/me`,
      invite_offer: `https://shop.${domain}/belfry/api/invites/{token}`,
      wallet_pass_tokens: `${pass}/wallet/pass-tokens`,
    },
    relays: { community: `wss://relay.${domain}` },
    mint: { url: `https://mint.${domain}`, unit: "osv" },
    sign_in: { keycast: `https://apps.${domain}` },
    apps: [{ name: "Wallet", url: `https://wallet.${domain}` }],
  };
}

let wellKnown;
const discovery = await fetch(`https://${DOMAIN}/.well-known/belfry.json`);
if (discovery.ok) {
  wellKnown = await discovery.json();
  if (wellKnown.belfry !== 1) throw new Error(`${DOMAIN} is not a Belfry`);
} else {
  console.warn(`${DOMAIN} has no /.well-known/belfry.json (${discovery.status}): using the usual host names`);
  wellKnown = conventional(DOMAIN);
}

// Only the APIs the page needs, on the Belfry's own hosts.
const allowed = [
  `${wellKnown.pass}/api/me`,
  `${wellKnown.pass}/api/email-code`,
  `${wellKnown.pass}/wallet/pass-tokens`,
  `${wellKnown.shop}/belfry/api/invites/`,
];

const bundle = await esbuild.build({
  entryPoints: [join(here, "src/app.js")],
  bundle: true,
  format: "esm",
  target: "es2022",
  minify: true,
  write: false,
});
const appJs = bundle.outputFiles[0].text;

async function body(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return chunks.length ? Buffer.concat(chunks) : undefined;
}

async function proxy(req, res, target) {
  if (!allowed.some((a) => target === a || target.startsWith(a.endsWith("/") ? a : a + "/"))) {
    res.writeHead(403, { "content-type": "application/json" });
    return res.end(JSON.stringify({ error: "forbidden", message: "not a proxied API" }));
  }
  const headers = {};
  for (const h of ["authorization", "content-type", "accept"]) {
    if (req.headers[h]) headers[h] = req.headers[h];
  }
  try {
    const upstream = await fetch(target, {
      method: req.method,
      headers,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : await body(req),
      redirect: "manual",
    });
    res.writeHead(upstream.status, {
      "content-type": upstream.headers.get("content-type") || "application/octet-stream",
      "cache-control": "no-store",
    });
    res.end(Buffer.from(await upstream.arrayBuffer()));
  } catch (e) {
    res.writeHead(502, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "upstream", message: String(e) }));
  }
}

http
  .createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    if (url.pathname === "/proxy") return proxy(req, res, url.searchParams.get("url") || "");
    if (url.pathname === "/belfry.json") {
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({ domain: DOMAIN, ...wellKnown }));
    }
    if (url.pathname === "/app.js") {
      res.writeHead(200, { "content-type": "text/javascript" });
      return res.end(appJs);
    }
    if (["/", "/index.html", "/oauth/callback"].includes(url.pathname)) {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      return res.end(await readFile(join(here, "public/index.html")));
    }
    res.writeHead(404).end();
  })
  .listen(PORT, () => console.log(`village notes for ${DOMAIN} on http://localhost:${PORT}`));
