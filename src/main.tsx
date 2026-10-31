import { createRoot } from 'react-dom/client'
import { App } from './ui/App'
import './styles.css'

createRoot(document.getElementById('root')!).render(<App />)

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  navigator.serviceWorker.register('./sw.js').catch(() => { /* offline shell is a bonus */ })
}
