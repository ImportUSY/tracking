/* Datos de navegación: mide los datos MÓVILES (red celular) que usa la app del conductor.
 *
 * - Solo cuenta mientras navigator.connection.type === "cellular". Por Wi-Fi no se mide nada. Si el
 *   teléfono no informa el tipo de red (iPhone, algunos navegadores) tampoco se cuenta: no se adivina.
 * - Recibido: tamaño transferido de cada recurso (Resource Timing: cabeceras + cuerpo; 0 si vino de la caché).
 * - Enviado: cuerpo de cada petición fetch + una estimación de sus cabeceras.
 * - Se agrupa por conexión (cada vez que se abre la app), método + ruta, día y hora, se guarda en el teléfono
 *   y se envía cada 5 minutos (o al salir de la app). El propio envío de la medición no se cuenta.
 */
"use strict";
const Consumo = (() => {
  const RUTA_ENVIO = "/api/conductor/consumo";
  const INTERVALO_MS = 5 * 60 * 1000;
  const CABECERAS_ESTIMADAS = 350; // bytes aproximados de cabeceras de una petición (URL aparte)
  const me = TL.user();
  if (!me || me.rol !== "conductor" || !window.fetch) return { medir: false };
  const CLAVE = "tl_consumo_" + me.id;
  const conexionId = (() => {
    try {
      let id = sessionStorage.getItem("tl_conexion");
      if (!id) { id = TL.uuid(); sessionStorage.setItem("tl_conexion", id); }
      return id;
    } catch (_) { return TL.uuid(); }
  })();

  const red = () => navigator.connection || null;
  const esCelular = () => { const c = red(); return !!c && c.type === "cellular"; };
  const p2 = (n) => String(n).padStart(2, "0");
  const diaLocal = (d) => `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
  function rutaNormal(url) {
    let u;
    try { u = new URL(url, location.href); } catch (_) { return null; }
    if (u.origin !== location.origin) return null;
    return u.pathname.replace(/\/(?:\d+|[0-9a-fA-F-]{16,36})(?=\/|$)/g, "/:id").slice(0, 200);
  }

  let pendientes = (() => {
    try { return JSON.parse(localStorage.getItem(CLAVE) || "{}") || {}; } catch (_) { return {}; }
  })();
  function guardar() { try { localStorage.setItem(CLAVE, JSON.stringify(pendientes)); } catch (_) { /* sin espacio */ } }

  function sumar(metodo, ruta, subida, bajada, contarSolicitud) {
    if (!ruta || ruta === RUTA_ENVIO || !esCelular()) return;
    const ahora = new Date();
    const dia = diaLocal(ahora);
    const hora = ahora.getHours();
    const k = [conexionId, metodo, ruta, dia, hora].join("|");
    const r = pendientes[k] || (pendientes[k] = {
      dia, hora, conexion_id: conexionId, metodo, ruta, tipo_efectivo: null, solicitudes: 0,
      bytes_subida: 0, bytes_bajada: 0, desde: ahora.toISOString(), hasta: ahora.toISOString(),
    });
    r.bytes_subida += Math.max(0, Math.round(subida || 0));
    r.bytes_bajada += Math.max(0, Math.round(bajada || 0));
    if (contarSolicitud) r.solicitudes += 1;
    r.hasta = ahora.toISOString();
    r.tipo_efectivo = (red() && red().effectiveType) || r.tipo_efectivo;
    guardar();
  }

  function tamanoCuerpo(body) {
    if (!body) return 0;
    if (typeof body === "string") return new Blob([body]).size;
    if (body instanceof Blob) return body.size;
    if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) return body.byteLength;
    if (body instanceof URLSearchParams) return new Blob([body.toString()]).size;
    if (body instanceof FormData) {
      let n = 0;
      for (const [k, v] of body.entries()) n += 120 + k.length + (typeof v === "string" ? new Blob([v]).size : v.size);
      return n;
    }
    return 0;
  }

  // Método de cada petición (Resource Timing no lo informa): se empareja por URL
  const metodos = new Map();
  const fetchOriginal = window.fetch.bind(window);
  window.fetch = function (entrada, opciones) {
    try {
      const url = typeof entrada === "string" ? entrada : (entrada && entrada.url) || String(entrada);
      const metodo = ((opciones && opciones.method) || (entrada && entrada.method) || "GET").toUpperCase();
      const ruta = rutaNormal(url);
      if (ruta) {
        const abs = new URL(url, location.href).href;
        (metodos.get(abs) || metodos.set(abs, []).get(abs)).push(metodo);
        sumar(metodo, ruta, tamanoCuerpo(opciones && opciones.body) + CABECERAS_ESTIMADAS + url.length, 0, true);
      }
    } catch (_) { /* la medición nunca bloquea la petición */ }
    return fetchOriginal(entrada, opciones);
  };

  function registrarRecurso(e) {
    const ruta = rutaNormal(e.name);
    if (!ruta) return;
    const cola = metodos.get(e.name);
    const metodo = (cola && cola.shift()) || "GET";
    if (cola && !cola.length) metodos.delete(e.name);
    const esFetch = e.initiatorType === "fetch" || e.initiatorType === "xmlhttprequest";
    // Las peticiones fetch ya se contaron al salir; las demás (página, scripts, imágenes) se cuentan aquí
    sumar(metodo, ruta, esFetch ? 0 : CABECERAS_ESTIMADAS + e.name.length, e.transferSize || 0, !esFetch);
  }
  try {
    new PerformanceObserver((lista) => lista.getEntries().forEach(registrarRecurso))
      .observe({ type: "resource", buffered: true });
    new PerformanceObserver((lista) => lista.getEntries().forEach(registrarRecurso))
      .observe({ type: "navigation", buffered: true });
  } catch (_) { /* navegador sin Resource Timing: solo se mide lo enviado */ }

  let enviando = false;
  async function enviar(alSalir = false) {
    const registros = Object.values(pendientes);
    if (enviando || !registros.length || !navigator.onLine || !TL.token()) return;
    enviando = true;
    // Se recuerda la clave de cada registro (las guardadas por versiones anteriores no traen la hora)
    const lote = Object.entries(pendientes).slice(0, 300).map(([k, r]) => [k, Object.assign({}, r)]);
    try {
      const res = await fetchOriginal(RUTA_ENVIO, {
        method: "POST", keepalive: alSalir, cache: "no-store",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + TL.token() },
        body: JSON.stringify({ registros: lote.map(([, r]) => r) }),
      });
      if (res.ok || res.status === 422) {
        // Lo enviado se descuenta (lo medido mientras tanto queda para el próximo envío)
        for (const [k, r] of lote) {
          const actual = pendientes[k];
          if (!actual) continue;
          actual.bytes_subida -= r.bytes_subida; actual.bytes_bajada -= r.bytes_bajada;
          actual.solicitudes -= r.solicitudes;
          if (actual.bytes_subida <= 0 && actual.bytes_bajada <= 0 && actual.solicitudes <= 0) delete pendientes[k];
          else actual.desde = actual.hasta;
        }
        guardar();
      }
    } catch (_) { /* sin señal: se reintenta en el próximo ciclo */ } finally { enviando = false; }
  }
  setInterval(() => enviar(), INTERVALO_MS);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") enviar(true); });
  window.addEventListener("online", () => enviar());
  setTimeout(() => enviar(), 30000);
  return { medir: true, enviar, esCelular };
})();
