/* "Libreta interna" del teléfono: IndexedDB con caché de facturas, cola de cambios y fotos pendientes.
 *
 * Garantías:
 *  - Cada cambio de estatus lleva un UUID generado aquí: el servidor lo usa como clave de idempotencia,
 *    así que reintentar nunca duplica.
 *  - La foto se guarda en el teléfono y solo se borra cuando el servidor confirmó la foto Y el cambio.
 *  - Los cambios rechazados por el servidor (p.ej. conflicto) no se pierden: pasan a "rechazados" y se muestran.
 */
"use strict";

const Outbox = (() => {
  const DB_NAME = "tracking-conductor";
  const DB_VERSION = 1;
  let dbp = null;

  function open() {
    if (dbp) return dbp;
    dbp = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("facturas")) db.createObjectStore("facturas", { keyPath: "key" });
        if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "key" });
        if (!db.objectStoreNames.contains("outbox")) {
          const s = db.createObjectStore("outbox", { keyPath: "id" });
          s.createIndex("uid", "uid");
        }
        if (!db.objectStoreNames.contains("fotos")) {
          const s = db.createObjectStore("fotos", { keyPath: "id" });
          s.createIndex("uid", "uid");
        }
        if (!db.objectStoreNames.contains("rechazados")) {
          const s = db.createObjectStore("rechazados", { keyPath: "id" });
          s.createIndex("uid", "uid");
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return dbp;
  }

  function tx(store, mode, fn) {
    return open().then((db) => new Promise((resolve, reject) => {
      const t = db.transaction(store, mode);
      const s = t.objectStore(store);
      let result;
      Promise.resolve(fn(s)).then((r) => { result = r; });
      t.oncomplete = () => resolve(result);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error || new Error("Transacción abortada"));
    }));
  }
  function reqP(req) {
    return new Promise((resolve, reject) => { req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); });
  }

  const put = (store, value) => tx(store, "readwrite", (s) => { s.put(value); });
  const del = (store, key) => tx(store, "readwrite", (s) => { s.delete(key); });
  const get = (store, key) => tx(store, "readonly", (s) => reqP(s.get(key)));
  const byUid = (store, uid) => tx(store, "readonly", (s) => reqP(s.index("uid").getAll(uid)));

  // ---------------- Caché de facturas (por usuario)
  async function saveFacturas(uid, facturas, servidorAt) {
    await tx("facturas", "readwrite", (s) => {
      s.put({ key: "u" + uid, uid, facturas, servidorAt, guardado: new Date().toISOString() });
    });
  }
  async function loadFacturas(uid) {
    const row = await get("facturas", "u" + uid);
    return row || { facturas: [], servidorAt: null, guardado: null };
  }

  // ---------------- Cola
  async function enqueue(uid, ev) {
    const item = Object.assign({ uid, creado: new Date().toISOString(), intentos: 0, offline: !navigator.onLine }, ev);
    await put("outbox", item);
    return item;
  }
  const pending = async (uid) => (await byUid("outbox", uid)).sort((a, b) => a.creado.localeCompare(b.creado));
  const removeEvent = (id) => del("outbox", id);
  const updateEvent = (item) => put("outbox", item);

  async function saveFoto(uid, id, facturaId, blob) {
    await put("fotos", { id, uid, factura_id: facturaId, blob, subida: false, creado: new Date().toISOString() });
  }
  const fotos = (uid) => byUid("fotos", uid);
  const getFoto = (id) => get("fotos", id);
  const markFotoSubida = async (id) => {
    const f = await get("fotos", id);
    if (f) { f.subida = true; await put("fotos", f); }
  };
  const removeFoto = (id) => del("fotos", id);

  async function reject(uid, item, mensaje) {
    await put("rechazados", { id: item.id, uid, factura_id: item.factura_id, numero: item.numero, estatus: item.estatus,
      error: mensaje, at: new Date().toISOString() });
    await removeEvent(item.id);
  }
  const rechazados = (uid) => byUid("rechazados", uid);
  const clearRechazado = (id) => del("rechazados", id);

  return {
    open, saveFacturas, loadFacturas, enqueue, pending, removeEvent, updateEvent, saveFoto, fotos, getFoto,
    markFotoSubida, removeFoto, reject, rechazados, clearRechazado,
  };
})();

/* Motor de sincronización: sube fotos, luego cambios, luego refresca. Reintenta con backoff exponencial. */
const Sync = (() => {
  const PERMANENTES = new Set(["conflicto", "invalido", "no_encontrada", "sin_permiso"]);
  const LOTE = 100;
  let running = false;
  let backoff = 0;
  let timer = null;
  let listeners = [];

  const onChange = (fn) => listeners.push(fn);
  const emit = (info) => listeners.forEach((fn) => { try { fn(info); } catch (_) { /* noop */ } });

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(() => run(), ms);
  }

  async function uploadFotos(uid) {
    const lista = (await Outbox.fotos(uid)).filter((f) => !f.subida);
    for (const f of lista) {
      const fd = new FormData();
      fd.append("factura_id", String(f.factura_id));
      if (f.creado) fd.append("capturada_at", f.creado);  // PoD: hora real de la foto, aunque se suba sin señal más tarde
      fd.append("archivo", f.blob, f.id + ".jpg");
      try {
        await TL.api("/api/fotos/" + f.id, { method: "PUT", body: fd });
        await Outbox.markFotoSubida(f.id);
      } catch (e) {
        if (e instanceof TL.ApiError && [403, 404, 409, 413, 415, 422].includes(e.status)) {
          // Foto inválida/ajena: no se puede subir nunca. Rechazar sus cambios asociados.
          for (const ev of (await Outbox.pending(uid)).filter((x) => x.foto_id === f.id)) {
            await Outbox.reject(uid, ev, "La foto no se pudo subir: " + e.message);
          }
          await Outbox.removeFoto(f.id);
        } else {
          throw e; // red o 5xx: reintentar más tarde
        }
      }
    }
  }

  async function sendEventos(uid) {
    const cola = await Outbox.pending(uid);
    for (let i = 0; i < cola.length; i += LOTE) {
      const lote = cola.slice(i, i + LOTE);
      const body = {
        eventos: lote.map((e) => ({
          id: e.id, factura_id: e.factura_id, estatus: e.estatus, ocurrido_at: e.ocurrido_at,
          motivo: e.motivo || null, foto_id: e.foto_id || null, offline: !!e.offline,
        })),
      };
      const r = await TL.api("/api/conductor/eventos", { method: "POST", body });
      const porId = new Map(lote.map((e) => [e.id, e]));
      for (const res of r.resultados) {
        const item = porId.get(res.id);
        if (!item) continue;
        if (res.ok) {
          await Outbox.removeEvent(item.id);
          if (item.foto_id) await Outbox.removeFoto(item.foto_id);
        } else if (PERMANENTES.has(res.codigo_error)) {
          await Outbox.reject(uid, item, res.error || "Rechazado por el servidor");
          if (item.foto_id) await Outbox.removeFoto(item.foto_id);
        } else {
          item.intentos += 1; item.ultimo_error = res.error;
          await Outbox.updateEvent(item);
        }
      }
    }
  }

  async function refresh(uid) {
    // Sincronización automática cada minuto: no es una consulta del usuario (no va a la bitácora)
    const r = await TL.api("/api/conductor/facturas?dias_finales=3", { headers: { "X-Sincronizacion": "1" } });
    await Outbox.saveFacturas(uid, r.facturas, r.servidor_at);
  }

  async function run() {
    const u = TL.user();
    if (running || !u) return;
    if (!navigator.onLine) { emit({ estado: "offline" }); return; }
    running = true;
    emit({ estado: "sincronizando" });
    try {
      await uploadFotos(u.id);
      await sendEventos(u.id);
      await refresh(u.id);
      backoff = 0;
      emit({ estado: "ok", at: new Date() });
      schedule(60000);
    } catch (e) {
      backoff = Math.min(backoff ? backoff * 2 : 5000, 300000);
      emit({ estado: "error", error: e.message, reintento: backoff });
      schedule(backoff);
    } finally {
      running = false;
    }
  }

  window.addEventListener("online", () => { backoff = 0; run(); });
  window.addEventListener("offline", () => emit({ estado: "offline" }));
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") run(); });

  return { run, onChange, schedule };
})();
