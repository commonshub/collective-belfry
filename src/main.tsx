import { createRoot } from 'react-dom/client'
import { App } from './ui/App'
import './styles.css'
import { NAME, THEME } from './config'

if (THEME === 'belfry') {
  document.documentElement.dataset.theme = 'belfry'
  document.title = `Chat · ${NAME}`
  document.querySelector('meta[name=theme-color]')?.setAttribute('content', '#ffffff')
}

createRoot(document.getElementById('root')!).render(<App />)

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  navigator.serviceWorker.register('./sw.js').catch(() => { /* offline shell is a bonus */ })
}
