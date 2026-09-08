import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { pdfjs } from 'react-pdf'
// pdf.js 5.x worker：Vite 下以静态资源 URL 方式加载（react-pdf v10 捆绑 pdfjs-dist 5.4.296）
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import App from './App'
import './index.css'

pdfjs.GlobalWorkerOptions.workerSrc = workerSrc

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
