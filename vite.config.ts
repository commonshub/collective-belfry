import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base './' so the build works from any sub-path (e.g. /projects/osv-app/)
export default defineConfig({ base: './', plugins: [react()] })
