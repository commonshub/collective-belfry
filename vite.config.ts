import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

declare const process: { env: Record<string, string | undefined> }

// The Belfry look (VITE_THEME=belfry) is the community's chat, named "Chat": its own page title,
// install name, colours and icon. Any other build keeps Open Source Village's.
function belfryNames(): Plugin {
  const on = process.env.VITE_THEME === 'belfry'
  return {
    name: 'belfry-names',
    transformIndexHtml(html) {
      if (!on) return html
      return html
        .replace(/<title>[^<]*<\/title>/, '<title>Chat</title>')
        .replace(/(<meta name="theme-color" content=")[^"]*/, '$1#ffffff')
        .replace('./icons/apple-touch-icon.png', './icons/belfry/apple-touch-icon.png')
        .replace('./icons/icon-192.png', './icons/belfry/icon-192.png')
    },
    generateBundle() {
      if (!on) return
      this.emitFile({
        type: 'asset',
        fileName: 'manifest.webmanifest',
        source: JSON.stringify({
          name: 'Chat', short_name: 'Chat', start_url: './', scope: './', display: 'standalone',
          background_color: '#ffffff', theme_color: '#ffffff',
          icons: [
            { src: 'icons/belfry/icon-192.png', sizes: '192x192', type: 'image/png' },
            { src: 'icons/belfry/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any maskable' },
          ],
        }, null, 2),
      })
    },
  }
}

// base './' so the build works from any sub-path (e.g. /projects/osv-app/)
export default defineConfig({ base: './', plugins: [react(), belfryNames()] })
