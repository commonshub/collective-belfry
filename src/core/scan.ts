// Reading QR codes from the camera: the browser's BarcodeDetector where there is one (Chrome on
// Android, Safari 17+), else jsQR (a pure-JS decoder, loaded only then, as its own chunk).

type Detector = { detect: (src: CanvasImageSource) => Promise<{ rawValue: string }[]> }
type DetectorCtor = { new (o: { formats: string[] }): Detector; getSupportedFormats?: () => Promise<string[]> }

async function nativeDetector(): Promise<Detector | null> {
  const BD = (globalThis as unknown as { BarcodeDetector?: DetectorCtor }).BarcodeDetector
  if (!BD) return null
  try {
    const formats = (await BD.getSupportedFormats?.()) ?? ['qr_code']
    return formats.includes('qr_code') ? new BD({ formats: ['qr_code'] }) : null
  } catch { return null }
}

/** A function that reads one frame of VIDEO and gives the QR's text, or null. */
export async function qrReader(video: HTMLVideoElement): Promise<() => Promise<string | null>> {
  const native = await nativeDetector()
  if (native) return async () => { try { return (await native.detect(video))[0]?.rawValue ?? null } catch { return null } }
  const { default: jsQR } = await import('jsqr')
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  return async () => {
    const w = video.videoWidth, h = video.videoHeight
    if (!ctx || !w || !h) return null
    // a smaller copy decodes faster and still reads a phone-sized code
    const scale = Math.min(1, 640 / Math.max(w, h))
    canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale)
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height)
    const img = ctx.getImageData(0, 0, canvas.width, canvas.height)
    return jsQR(img.data, img.width, img.height, { inversionAttempts: 'dontInvert' })?.data ?? null
  }
}
