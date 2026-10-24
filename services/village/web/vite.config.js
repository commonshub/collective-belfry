// Two web apps from one tree: APP=people or APP=market picks the root (web/<app>); both import
// web/shared. Built into web/<app>/dist and served by server/index.js.
import { defineConfig } from "vite"

const app = process.env.APP
if (!["people", "market"].includes(app)) throw new Error("set APP=people or APP=market")

export default defineConfig({
  root: `${import.meta.dirname}/${app}`,
  envDir: import.meta.dirname,
  // Keycast clients belfry-people and belfry-market (registered and trusted by bootstrap/Compose)
  define: {
    "import.meta.env.VITE_APP_NAME": JSON.stringify(app),
    "import.meta.env.VITE_OAUTH_CLIENT_ID": JSON.stringify(`belfry-${app}`),
  },
  build: { outDir: "dist", emptyOutDir: true, target: "es2022" },
})
