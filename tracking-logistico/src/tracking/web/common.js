/* Utilidades compartidas: sesión, llamadas a la API, formato y UI básica. */
"use strict";

const TL = (() => {
  const TOKEN_KEY = "tl_token";
  const USER_KEY = "tl_user";

  const ESTATUS = {
    recibida: "Preparado",
    en_transito: "Por Entregar",
    entregada: "Entregada",
    devolucion: "Devolución",
  };
  // «Cargado» ya no existe (v1.17.0). Se conservan los números: 3 = final, < 2 = aún puede pasar a Por Entregar.
  const ORDEN = { recibida: 0, en_transito: 2, entregada: 3, devolucion: 3 };

  function store(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (_) { /* almacenamiento bloqueado: la sesión dura lo que la pestaña */ }
  }
  function read(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }

  let memToken = read(TOKEN_KEY);
  let memUser = (() => { try { return JSON.parse(read(USER_KEY) || "null"); } catch (_) { return null; } })();

  function setSession(token, user) {
    memToken = token; memUser = user;
    store(TOKEN_KEY, token); store(USER_KEY, user ? JSON.stringify(user) : null);
  }
  function clearSession() { setSession(null, null); }
  function token() { return memToken; }
  function user() { return memUser; }

  class ApiError extends Error {
    constructor(status, message, body) { super(message); this.status = status; this.body = body; }
  }

  // Rol CONSULTOR: ve todos los botones, pero cualquier cambio se detiene aquí con este aviso
  // (el servidor también lo rechaza con el mismo mensaje).
  const MENSAJE_SOLO_CONSULTA = "Este usuario es Consultor: solo puede consultar. No puede agregar, modificar ni eliminar.";
  function bloqueadoPorConsulta(path, opts) {
    const metodo = (opts.method || "GET").toUpperCase();
    if (["GET", "HEAD", "OPTIONS"].includes(metodo) || !memUser || memUser.rol !== "consultor") return false;
    if (path.startsWith("/api/auth/") || path === "/api/me/clave" || path.startsWith("/api/me/biometria") || path === "/api/bitacora/vista") return false;
    // Análisis que no guardan nada: extracción de la foto y de la hoja de remesas
    if (["/api/capturas/analizar", "/api/importaciones/analizar", "/api/importaciones/analizar-archivo"].includes(path)) return false;
    const b = opts.body;
    return !(path.startsWith("/api/reportes/notificar/vendedores") && b && typeof b === "object" && b.simular === true);
  }

  async function api(path, opts = {}) {
    if (bloqueadoPorConsulta(path, opts)) {
      toast(MENSAJE_SOLO_CONSULTA, 5000);
      throw new ApiError(403, MENSAJE_SOLO_CONSULTA);
    }
    const headers = Object.assign({}, opts.headers || {});
    if (memToken) headers["Authorization"] = "Bearer " + memToken;
    let body = opts.body;
    if (body instanceof Blob) {
      headers["Content-Type"] = body.type || "application/octet-stream";
    } else if (body && !(body instanceof FormData) && typeof body !== "string") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const res = await fetch(path, { method: opts.method || "GET", headers, body, cache: "no-store" });
    if (opts.raw) {
      if (!res.ok) throw new ApiError(res.status, await errorText(res));
      return res;
    }
    let data = null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) data = await res.json();
    if (!res.ok) {
      if (res.status === 401 && opts.onUnauthorized !== false) onUnauthorized();
      // Agotó los accesos con la clave temporal: volver al ingreso para crear la clave personal
      if (res.status === 403 && detail(data).startsWith("Ya usó todos los accesos con la clave temporal")) {
        clearSession(); location.href = "/";
      }
      throw new ApiError(res.status, detail(data) || ("Error " + res.status), data);
    }
    return data;
  }

  async function errorText(res) {
    try { return detail(await res.json()) || ("Error " + res.status); } catch (_) { return "Error " + res.status; }
  }
  function detail(data) {
    if (!data) return "";
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg).join("; ");
    return "";
  }

  let unauthorizedHandler = () => { clearSession(); location.href = "/"; };
  function onUnauthorized() { unauthorizedHandler(); }
  function setUnauthorizedHandler(fn) { unauthorizedHandler = fn; }

  /** Aviso fijo mientras se use la clave temporal (panel y app del conductor). */
  function avisoClaveTemporal(u) {
    if (!u || !u.debe_cambiar_clave) return "";
    const n = u.accesos_temporales_restantes ?? 0;
    const quedan = n > 0 ? `Le ${n === 1 ? "queda 1 acceso" : `quedan ${n} accesos`} con ella` : "Este es su último acceso con ella";
    return `<div id="avisoClave" class="alert warn aviso-clave" role="status"><span>⚠ Está usando una <strong>clave temporal</strong>. ${quedan}; luego deberá cambiarla.</span>
      <button type="button" class="small" id="avisoClaveBtn">Cambiar mi clave</button></div>`;
  }

  function esc(v) {
    return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function fmtFechaHora(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString("es-VE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  /** Fecha y hora; si es exactamente las 00:00 (solo se conocía el día, p. ej. del Excel), solo la fecha. */
  function fmtMomento(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    if (d.getHours() === 0 && d.getMinutes() === 0 && d.getSeconds() === 0) {
      return d.toLocaleDateString("es-VE", { day: "2-digit", month: "2-digit", year: "numeric" });
    }
    return fmtFechaHora(iso);
  }
  function fmtFecha(iso) {
    if (!iso) return "";
    const [y, m, d] = String(iso).slice(0, 10).split("-");
    return `${d}/${m}/${y}`;
  }
  /** DÍAS TRANSCURRIDOS ENTRE PROCESOS (panel interno y consulta pública): jornada laboral, sin feriados.
   *  ``t`` = TiemposProcesoOut de la API. */
  function cuadroTiempos(t) {
    if (!t || !t.tramos) return "";
    const SIN = { pendiente: "Pendiente", sin_dato: "Falta la fecha" };
    const hasta = (x) => x.estado === "en_curso" ? "ahora" : (fmtMomento(x.fin) || "—");
    const fila = (x) => `<tr class="t-${esc(x.estado)}">
        <td>Entre ${esc(x.desde)} y ${esc(x.hasta)}<br><small class="muted">${esc(fmtMomento(x.inicio) || "—")} → ${esc(hasta(x))}</small></td>
        <td class="num">${x.texto === null ? `<small>${SIN[x.estado] || "—"}</small>` : `<strong class="${x.minutos < 0 ? "neg" : ""}">${esc(x.texto)}</strong>${x.estado === "en_curso" ? "<br><small>en curso</small>" : ""}`}</td></tr>`;
    const titulo = t.devolucion ? "Tiempo hasta la devolución" : "Total Días para la entrega";
    return `<section class="tiempos${t.devolucion ? " devolucion" : ""}" aria-label="Días transcurridos entre procesos">
      <h4>DÍAS TRANSCURRIDOS ENTRE PROCESOS</h4>
      <p class="nota">${esc(t.nota || "Días hábiles: de lunes a viernes de 7:00 a 16:00")}</p>
      ${t.mensaje ? `<div class="msg-devolucion">${esc(t.mensaje)}</div>` : ""}
      <table><tbody>${t.tramos.map(fila).join("")}</tbody>
        <tfoot><tr><td>${titulo}</td><td class="num"><strong>${t.total_texto === null ? "—" : esc(t.total_texto)}</strong>${t.en_curso ? "<br><small>en curso (hasta ahora)</small>" : ""}</td></tr></tfoot></table>
      ${t.aviso ? `<small class="aviso">${esc(t.aviso)}</small>` : ""}</section>`;
  }
  function fmtMonto(v) {
    if (v === null || v === undefined || v === "") return "";
    return Number(v).toLocaleString("es-VE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function badge(estatus) {
    return `<span class="badge s-${esc(estatus)}">${esc(ESTATUS[estatus] || estatus)}</span>`;
  }

  let toastTimer = null;
  function toast(msg, ms = 3500) {
    let el = document.getElementById("tl-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "tl-toast"; el.className = "toast"; el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.textContent = msg; el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), ms);
  }

  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  function $(sel, root = document) { return root.querySelector(sel); }
  function $$(sel, root = document) { return [...root.querySelectorAll(sel)]; }

  function registerSW() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
    }
  }

  async function download(path, fallbackName) {
    const res = await api(path, { raw: true });
    const blob = await res.blob();
    const cd = res.headers.get("content-disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = m ? m[1] : fallbackName;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  // Reduce y re-codifica una foto del teléfono a JPEG (orientación EXIF aplicada). Si el navegador no
  // puede (sin createImageBitmap, formato raro), devuelve el archivo original.
  async function comprimirImagen(file, maxDim = 1600, calidad = 0.72) {
    try {
      const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
      const escala = Math.min(1, maxDim / Math.max(bmp.width, bmp.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bmp.width * escala); canvas.height = Math.round(bmp.height * escala);
      canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", calidad));
      if (blob) return blob;
    } catch (_) { /* se usa el original */ }
    return file;
  }

  // Bitácora: pantalla abierta y cierre de sesión (no bloquean ni fallan si no hay señal)
  function registrarVista(vista, titulo) {
    if (!memToken) return;
    api("/api/bitacora/vista", { method: "POST", body: { vista, titulo } }).catch(() => {});
  }
  async function cerrarSesion() {
    if (memToken && navigator.onLine) {
      await Promise.race([api("/api/auth/logout", { method: "POST" }).catch(() => {}), new Promise((ok) => setTimeout(ok, 2500))]);
    }
    clearSession();
  }

  // Versión del sistema y fecha de creación (servidor: /api/config) en todo elemento [data-version] /
  // [data-fecha-creacion]: pies de página, app del conductor y su «Mis datos». Sin señal se muestra la
  // última versión conocida en este equipo.
  function pieVersion() {
    const vs = document.querySelectorAll("[data-version]"), fs = document.querySelectorAll("[data-fecha-creacion]");
    if (!vs.length && !fs.length) return;
    const poner = (version, fecha) => {
      if (version) vs.forEach((v) => { v.textContent = `Versión ${version}`; });
      if (fecha) fs.forEach((f) => { f.textContent = fecha; });
    };
    let guardada = null;
    try { guardada = JSON.parse(localStorage.getItem("tl_version") || "null"); } catch { /* sin almacenamiento */ }
    if (guardada) poner(guardada.version, guardada.fecha);
    fetch("/api/config", { cache: "no-store" }).then((r) => r.json()).then((c) => {
      const version = c.version_publica || c.version, fecha = c.fecha_creacion;
      poner(version, fecha);
      try { localStorage.setItem("tl_version", JSON.stringify({ version, fecha })); } catch { /* sin almacenamiento */ }
    }).catch(() => { /* sin conexión: queda la última versión conocida */ });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", pieVersion);
  else pieVersion();

  return {
    ESTATUS, ORDEN, api, ApiError, setSession, clearSession, token, user, setUnauthorizedHandler,
    esc, fmtFechaHora, fmtMomento, fmtFecha, fmtMonto, cuadroTiempos, avisoClaveTemporal, badge, toast, uuid, $, $$, registerSW, download, comprimirImagen,
    registrarVista, cerrarSesion,
  };
})();
