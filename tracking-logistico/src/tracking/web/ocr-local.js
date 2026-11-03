/* OCR gratuito en el navegador con PaddleOCR (modelos PP-OCRv3 en ONNX) y ONNX Runtime Web.
 *
 * Mucho más preciso que Tesseract con fotos de facturas (papel de color, marcas de agua, sellos):
 * 1. Detección: una red marca dónde hay texto; se agrupan los píxeles en cajas (una por frase).
 * 2. Reconocimiento: cada caja se lee con otra red (CTC) y se obtiene su texto y confianza.
 * El resultado (texto por líneas + cajas con posición) lo interpreta el servidor
 * (tracking/services/ocr_facturas.py). La imagen no sale del dispositivo hacia terceros.
 *
 * Los modelos (~13 MB) y el motor (~14 MB) se descargan una sola vez, en partes de 3 MB
 * (límite por respuesta de Vercel), y quedan en la caché del navegador.
 */
"use strict";
const OcrLocal = (() => {
  const BASE = "/static/vendor/ocr-paddle-v3";
  const ORT = "/static/vendor/onnxruntime-web-1.30.0";
  const DET_LADO = 1280;      // lado mayor de la imagen para detectar texto
  const DET_UMBRAL = 0.3, CAJA_UMBRAL = 0.55, UNCLIP = 1.6, MIN_LADO = 4;
  const REC_ALTO = 48, REC_LOTE = 8, TEXTO_MIN = 0.5;
  const MEDIA = [0.485, 0.456, 0.406], DESV = [0.229, 0.224, 0.225];
  let listoP = null, avance = null, partesListas = 0, partesTotal = 1;

  // ONNX Runtime calcula en el hilo principal: entre lote y lote se cede el turno para que la página
  // siga respondiendo (subir la foto, mostrar los datos parciales, dibujar el formulario).
  const ceder = () => new Promise((ok) => setTimeout(ok, 0));
  const conTiempo = (p, ms, msg) => Promise.race([p, new Promise((_, mal) => setTimeout(() => mal(new Error(msg)), ms))]);

  function cargarScript(src) {
    return new Promise((ok, mal) => {
      if (window.ort) return ok();
      const s = document.createElement("script");
      s.src = src; s.onload = () => ok(); s.onerror = () => mal(new Error("No se pudo cargar el motor de lectura"));
      document.head.appendChild(s);
    });
  }

  async function bajar(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`No se pudo descargar ${url.split("/").pop()} (HTTP ${r.status})`);
    const datos = new Uint8Array(await r.arrayBuffer());
    partesListas++;
    if (avance) try { avance(Math.min(1, partesListas / partesTotal)); } catch (_) { /* solo informativo */ }
    return datos;
  }

  async function bajarPartes(desc) {
    const partes = await Promise.all(desc.partes.map((p) => bajar(`${BASE}/${p}`)));
    const total = partes.reduce((n, p) => n + p.length, 0);
    if (total !== desc.bytes) throw new Error("Archivo del lector incompleto: recargue la página");
    const out = new Uint8Array(total);
    let o = 0;
    for (const p of partes) { out.set(p, o); o += p.length; }
    return out;
  }

  /** Descarga motor + modelos y crea las sesiones (llamar al abrir la pantalla).
   *  onAvance(0..1) informa la descarga (solo la primera vez pesa: luego sale de la caché). */
  function precalentar(onAvance) {
    if (onAvance) avance = onAvance;
    if (!listoP) {
      listoP = conTiempo((async () => {
        const rm = await fetch(`${BASE}/manifest.json`);
        if (!rm.ok) throw new Error(`faltan los archivos del lector en el servidor (HTTP ${rm.status})`);
        const man = await rm.json();
        partesListas = 0;
        partesTotal = man.wasm.partes.length + man.det.partes.length + man.rec.partes.length;
        const [wasm, det, rec, dictTxt] = await Promise.all([
          bajarPartes(man.wasm), bajarPartes(man.det), bajarPartes(man.rec),
          fetch(`${BASE}/dict.txt`).then((r) => r.text()), cargarScript(`${ORT}/ort.wasm.min.js`),
        ]);
        ort.env.wasm.wasmPaths = ORT + "/";
        ort.env.wasm.wasmBinary = wasm.buffer;
        // Varios hilos solo si la página está aislada (cabeceras COOP/COEP que pone el servidor)
        ort.env.wasm.numThreads = self.crossOriginIsolated ? Math.max(1, Math.min(4, navigator.hardwareConcurrency || 1)) : 1;
        const opts = { executionProviders: ["wasm"], graphOptimizationLevel: "all" };
        const sDet = await ort.InferenceSession.create(det, opts);
        const sRec = await ort.InferenceSession.create(rec, opts);
        // CTC: 0 = blank, luego el diccionario del modelo y el espacio al final
        const chars = ["", ...dictTxt.split("\n"), " "];
        return { sDet, sRec, chars };
      })(), 180000, "No se pudo preparar el lector de facturas (descarga muy lenta)").catch((e) => { listoP = null; throw e; });
    }
    return listoP;
  }

  function lienzo(fuente, w, h) {
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(fuente, 0, 0, w, h);
    return { c, ctx };
  }

  // ---------------------------------------------------------------- geometría
  /** Envolvente convexa (cadena monótona). */
  function envolvente(pts) {
    pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cruz = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const inf = [], sup = [];
    for (const p of pts) { while (inf.length >= 2 && cruz(inf[inf.length - 2], inf[inf.length - 1], p) <= 0) inf.pop(); inf.push(p); }
    for (let i = pts.length - 1; i >= 0; i--) {
      const p = pts[i];
      while (sup.length >= 2 && cruz(sup[sup.length - 2], sup[sup.length - 1], p) <= 0) sup.pop();
      sup.push(p);
    }
    inf.pop(); sup.pop();
    return inf.concat(sup);
  }

  /** Rectángulo girado de área mínima (como cv2.minAreaRect): las fotos casi nunca salen derechas
   *  y recortar el renglón inclinado con una caja recta mezcla letras de los renglones vecinos. */
  function rectMinimo(h) {
    let m = null;
    for (let i = 0; i < h.length; i++) {
      const p = h[i], q = h[(i + 1) % h.length], L = Math.hypot(q[0] - p[0], q[1] - p[1]);
      if (!L) continue;
      const ux = (q[0] - p[0]) / L, uy = (q[1] - p[1]) / L;
      let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity;
      for (const r of h) {
        const a = r[0] * ux + r[1] * uy, b = -r[0] * uy + r[1] * ux;
        if (a < a0) a0 = a; if (a > a1) a1 = a; if (b < b0) b0 = b; if (b > b1) b1 = b;
      }
      const area = (a1 - a0) * (b1 - b0);
      if (!m || area < m.area) m = { area, ux, uy, a0, a1, b0, b1 };
    }
    const am = (m.a0 + m.a1) / 2, bm = (m.b0 + m.b1) / 2;
    const cx = am * m.ux - bm * m.uy, cy = am * m.uy + bm * m.ux;
    let cos, sin, w, hh;
    if (Math.abs(m.ux) >= Math.abs(m.uy)) { cos = m.ux; sin = m.uy; w = m.a1 - m.a0; hh = m.b1 - m.b0; }
    else { cos = -m.uy; sin = m.ux; w = m.b1 - m.b0; hh = m.a1 - m.a0; }
    if (cos < 0) { cos = -cos; sin = -sin; }  // el ancho siempre «hacia la derecha»
    return { cx, cy, w, h: hh, cos, sin };
  }

  // ---------------------------------------------------------------- detección
  async function detectar(m, img) {
    const esc = DET_LADO / Math.max(img.width, img.height);
    const W = Math.max(32, Math.round((img.width * esc) / 32) * 32), H = Math.max(32, Math.round((img.height * esc) / 32) * 32);
    const { ctx } = lienzo(img, W, H);
    const px = ctx.getImageData(0, 0, W, H).data;
    const n = W * H, datos = new Float32Array(3 * n);
    for (let i = 0; i < n; i++) {
      for (let c = 0; c < 3; c++) datos[c * n + i] = (px[i * 4 + c] / 255 - MEDIA[c]) / DESV[c];
    }
    const sal = await m.sDet.run({ [m.sDet.inputNames[0]]: new ort.Tensor("float32", datos, [1, 3, H, W]) });
    const prob = sal[m.sDet.outputNames[0]].data;

    // Binariza y dilata 2x2 (igual que PaddleOCR) y agrupa píxeles conectados en cajas
    const bin = new Uint8Array(n);
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const i = y * W + x;
      if (prob[i] > DET_UMBRAL || (x > 0 && prob[i - 1] > DET_UMBRAL) || (y > 0 && prob[i - W] > DET_UMBRAL)
        || (x > 0 && y > 0 && prob[i - W - 1] > DET_UMBRAL)) bin[i] = 1;
    }
    const etiqueta = new Int32Array(n), pila = new Int32Array(n), cajas = [];
    const filaMin = new Int32Array(H).fill(W), filaMax = new Int32Array(H).fill(-1);
    const fx = img.width / W, fy = img.height / H;
    let sig = 0;
    for (let i0 = 0; i0 < n; i0++) {
      if (!bin[i0] || etiqueta[i0]) continue;
      sig++;
      let tope = 0, y0 = H, y1 = 0, suma = 0, cuenta = 0;
      pila[tope++] = i0; etiqueta[i0] = sig;
      while (tope) {
        const i = pila[--tope], x = i % W, y = (i / W) | 0;
        if (y < y0) y0 = y; if (y > y1) y1 = y;
        if (x < filaMin[y]) filaMin[y] = x; if (x > filaMax[y]) filaMax[y] = x;
        suma += prob[i]; cuenta++;
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
          const xx = x + dx, yy = y + dy;
          if (xx < 0 || yy < 0 || xx >= W || yy >= H) continue;
          const j = yy * W + xx;
          if (bin[j] && !etiqueta[j]) { etiqueta[j] = sig; pila[tope++] = j; }
        }
      }
      // Contorno por filas (extremos izquierdo y derecho de cada fila) → rectángulo girado
      const pts = [];
      for (let y = y0; y <= y1; y++) {
        if (filaMax[y] >= 0) pts.push([filaMin[y], y], [filaMax[y] + 1, y], [filaMin[y], y + 1], [filaMax[y] + 1, y + 1]);
        filaMin[y] = W; filaMax[y] = -1;
      }
      if (cuenta < 6 || suma / cuenta < CAJA_UMBRAL) continue;
      const r = rectMinimo(envolvente(pts));
      if (Math.min(r.w, r.h) < 3) continue;
      const d = (r.w * r.h * UNCLIP) / (2 * (r.w + r.h));  // «unclip»: el mapa marca el centro del texto
      r.w += 2 * d; r.h += 2 * d;
      if (Math.min(r.w, r.h) < MIN_LADO + 2) continue;
      // A coordenadas de la imagen de trabajo
      const ex = r.cos * fx, ey = r.sin * fy, nx = -r.sin * fx, ny = r.cos * fy;
      const le = Math.hypot(ex, ey), ln = Math.hypot(nx, ny);
      const caja = { cx: r.cx * fx, cy: r.cy * fy, rw: r.w * le, rh: r.h * ln, cos: ex / le, sin: ey / le };
      // Caja recta que lo contiene (para ordenar en renglones y para el análisis del servidor)
      const hx = (Math.abs(caja.cos) * caja.rw + Math.abs(caja.sin) * caja.rh) / 2;
      const hy = (Math.abs(caja.sin) * caja.rw + Math.abs(caja.cos) * caja.rh) / 2;
      caja.x = Math.max(0, caja.cx - hx); caja.y = Math.max(0, caja.cy - hy);
      caja.w = Math.min(img.width, caja.cx + hx) - caja.x; caja.h = Math.min(img.height, caja.cy + hy) - caja.y;
      cajas.push(caja);
    }
    return cajas;
  }

  // ----------------------------------------------------------- reconocimiento
  /** Recorta el rectángulo girado y lo endereza a alto REC_ALTO. */
  function recortar(img, b, w) {
    const c = document.createElement("canvas");
    c.width = w; c.height = REC_ALTO;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.imageSmoothingQuality = "high";
    const sx = b.rw / w, sy = b.rh / REC_ALTO;
    const tx = b.cx - b.cos * b.rw / 2 + b.sin * b.rh / 2, ty = b.cy - b.sin * b.rw / 2 - b.cos * b.rh / 2;
    const a = b.cos / sx, cc = b.sin / sx, bb = -b.sin / sy, d = b.cos / sy;
    ctx.setTransform(a, bb, cc, d, -(a * tx + cc * ty), -(bb * tx + d * ty));
    ctx.drawImage(img, 0, 0);
    return ctx.getImageData(0, 0, w, REC_ALTO).data;
  }

  async function reconocer(m, img, cajas) {
    const ratio = (b) => b.rw / b.rh;
    const orden = cajas.map((c, i) => i).sort((a, b) => ratio(cajas[a]) - ratio(cajas[b]));
    const res = new Array(cajas.length);
    for (let ini = 0; ini < orden.length; ini += REC_LOTE) {
      await ceder();
      const lote = orden.slice(ini, ini + REC_LOTE);
      const maxRatio = Math.max(320 / REC_ALTO, ...lote.map((i) => ratio(cajas[i])));
      const W = Math.min(Math.ceil(REC_ALTO * maxRatio), 2400);
      const plano = REC_ALTO * W, datos = new Float32Array(lote.length * 3 * plano);  // ceros = relleno
      lote.forEach((idx, k) => {
        const w = Math.max(1, Math.min(W, Math.ceil(REC_ALTO * ratio(cajas[idx]))));
        const px = recortar(img, cajas[idx], w);
        const base = k * 3 * plano;
        for (let y = 0; y < REC_ALTO; y++) for (let x = 0; x < w; x++) {
          const p = (y * w + x) * 4, q = y * W + x;
          for (let ch = 0; ch < 3; ch++) datos[base + ch * plano + q] = px[p + ch] / 127.5 - 1;
        }
      });
      const sal = await m.sRec.run({ [m.sRec.inputNames[0]]: new ort.Tensor("float32", datos, [lote.length, 3, REC_ALTO, W]) });
      const t = sal[m.sRec.outputNames[0]];
      const [, T, C] = t.dims, p = t.data;
      lote.forEach((idx, k) => {
        let texto = "", suma = 0, cuenta = 0, previo = -1;
        for (let s = 0; s < T; s++) {
          let mejor = 0, pm = -1;
          const off = (k * T + s) * C;
          for (let c = 0; c < C; c++) if (p[off + c] > pm) { pm = p[off + c]; mejor = c; }
          if (mejor !== 0 && mejor !== previo) { texto += m.chars[mejor] || ""; suma += pm; cuenta++; }
          previo = mejor;
        }
        res[idx] = { texto, confianza: cuenta ? suma / cuenta : 0 };
      });
    }
    return res;
  }

  /** Agrupa cajas en renglones (de arriba abajo, de izquierda a derecha). */
  function renglones(cajas) {
    const orden = [...cajas].sort((a, b) => (a.y + a.h / 2) - (b.y + b.h / 2));
    const filas = [];
    for (const c of orden) {
      const yc = c.y + c.h / 2;
      const fila = filas.find((f) => Math.abs(f.yc - yc) < 0.55 * Math.min(f.h, c.h));
      if (fila) { fila.cajas.push(c); fila.yc = (fila.yc * (fila.cajas.length - 1) + yc) / fila.cajas.length; }
      else filas.push({ yc, h: c.h, cajas: [c] });
    }
    return filas.map((f) => f.cajas.sort((a, b) => a.x - b.x).map((c) => c.texto).join("  ")).join("\n");
  }

  const aCaja = (c, r) => ({ texto: r.texto.trim(), confianza: Math.round(r.confianza * 100) / 100,
    x: Math.round(c.x), y: Math.round(c.y), w: Math.round(c.w), h: Math.round(c.h) });

  function resultado(leidas, t0, extra) {
    const buenas = leidas.filter((c) => c.texto && c.confianza >= TEXTO_MIN);
    const conf = buenas.length ? buenas.reduce((s, c) => s + c.confianza, 0) / buenas.length : 0;
    return { texto: renglones(buenas), confianza: Math.round(conf * 100) / 100, cajas: buenas.slice(0, 400),
      ms: Math.round(performance.now() - t0), ...extra };
  }

  /** Lee la factura por partes para que los datos lleguen cuanto antes:
   *  1) encabezado (cliente, factura, fecha, vendedor, pedido), 2) pie (totales, bultos), 3) resto.
   *  onParcial(resultado) se llama tras cada parte; devuelve el resultado completo.
   *  Resultado: {texto (por renglones), confianza 0..1, cajas: [{texto, confianza, x, y, w, h}], ms}. */
  async function leer(blob, { onParcial } = {}) {
    const t0 = performance.now();
    const [m, bmp] = await Promise.all([precalentar(), createImageBitmap(blob, { imageOrientation: "from-image" })]);
    // Imagen de trabajo (máx. 2000 px) para recortar las cajas con buena resolución
    const esc = Math.min(1, 2000 / Math.max(bmp.width, bmp.height));
    const { c: img } = lienzo(bmp, Math.round(bmp.width * esc), Math.round(bmp.height * esc));
    if (bmp.close) bmp.close();
    await ceder();
    const cajas = (await conTiempo(detectar(m, img), 60000, "La detección de texto tardó demasiado"))
      .filter((c) => c.h < img.height * 0.08 && c.w / c.h < 60);  // sin logotipos ni rayas
    const yc = (c) => (c.y + c.h / 2) / img.height;
    const partes = [cajas.filter((c) => yc(c) < 0.45), cajas.filter((c) => yc(c) > 0.62), cajas.filter((c) => yc(c) >= 0.45 && yc(c) <= 0.62)];
    const leidas = [];
    for (let i = 0; i < partes.length; i++) {
      if (!partes[i].length) continue;
      const r = await conTiempo(reconocer(m, img, partes[i]), 90000, "La lectura tardó demasiado");
      partes[i].forEach((c, k) => leidas.push(aCaja(c, r[k])));
      if (onParcial && i < partes.length - 1) onParcial(resultado(leidas, t0, { parcial: true }));
    }
    return resultado(leidas, t0, { hilos: ort.env.wasm.numThreads });
  }

  return {
    leer, precalentar,
    disponible: () => typeof createImageBitmap === "function" && typeof WebAssembly === "object",
  };
})();
