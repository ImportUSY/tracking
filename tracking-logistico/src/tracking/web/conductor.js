"use strict";
(() => {
  const { $, $$, esc, badge, ORDEN, toast, uuid, api } = TL;
  TL.registerSW();

  const me = TL.user();
  if (!TL.token() || !me || me.rol !== "conductor" || me.cambio_clave_obligatorio) { location.replace("/"); return; }

  let maxFotoMb = 4;
  fetch("/api/config").then((r) => r.json()).then((c) => { maxFotoMb = c.max_foto_mb || 4; }).catch(() => {});
  const state = { facturas: [], pendientes: [], rechazados: [], filtro: "abiertas", q: "", sel: new Set(), servidorAt: null };
  $("#nombre").textContent = me.nombre;
  // Clave temporal: aviso con los accesos que le quedan; el botón abre «Mis datos» para cambiarla
  if (me.debe_cambiar_clave) {
    $("#main").insertAdjacentHTML("afterbegin", TL.avisoClaveTemporal(me));
    $("#avisoClaveBtn").onclick = () => { $("#btnMenu").click(); setTimeout(() => $("#pC1").focus(), 300); };
  }

  TL.setUnauthorizedHandler(() => {
    const a = $("#aviso");
    a.innerHTML = 'Su sesión venció. Sus cambios pendientes siguen guardados en el teléfono. ' +
      '<button type="button" class="small primary" id="reLogin">Iniciar sesión</button>';
    a.classList.remove("hidden");
    $("#reLogin").onclick = () => { TL.setSession(null, me); location.href = "/"; };
  });

  // ------------------------------------------------------------------ datos
  async function loadLocal() {
    const cache = await Outbox.loadFacturas(me.id);
    state.facturas = cache.facturas || [];
    state.servidorAt = cache.servidorAt;
    state.pendientes = await Outbox.pending(me.id);
    state.rechazados = await Outbox.rechazados(me.id);
  }

  function merged() {
    const porFactura = new Map();
    for (const ev of state.pendientes) {
      if (!porFactura.has(ev.factura_id)) porFactura.set(ev.factura_id, []);
      porFactura.get(ev.factura_id).push(ev);
    }
    return state.facturas.map((f) => {
      const evs = porFactura.get(f.id) || [];
      let estatus = f.estatus;
      for (const ev of evs) {
        const finales = ORDEN[estatus] === 3 && ORDEN[ev.estatus] === 3;
        if (ORDEN[ev.estatus] > ORDEN[estatus] || finales) estatus = ev.estatus;
      }
      return Object.assign({}, f, { estatusVista: estatus, pendiente: evs.length > 0 });
    });
  }

  // ------------------------------------------------------------------ render
  function render() {
    const lista = merged();
    const esFinal = (f) => ORDEN[f.estatusVista] === 3;
    const abiertas = lista.filter((f) => !esFinal(f));
    const cerradas = lista.filter(esFinal);
    $("#cAb").textContent = `(${abiertas.length})`;
    $("#cCe").textContent = `(${cerradas.length})`;

    const q = state.q.toLowerCase();
    let ver = state.filtro === "abiertas" ? abiertas : cerradas;
    if (q) {
      ver = ver.filter((f) => [f.numero, f.cliente, f.pedido, f.tracking].some((v) => (v || "").toLowerCase().includes(q)));
    }
    // limpiar selección de facturas que ya no están visibles/abiertas
    for (const id of [...state.sel]) if (!abiertas.some((f) => f.id === id)) state.sel.delete(id);

    const grupos = new Map();
    for (const f of ver) {
      const k = f.remesa_numero;
      if (!grupos.has(k)) grupos.set(k, { destino: f.destino, empresa: f.empresa, items: [] });
      grupos.get(k).items.push(f);
    }

    const cont = $("#lista");
    if (!ver.length) {
      cont.innerHTML = `<div class="card muted">${state.facturas.length ? "No hay facturas en esta vista." :
        (navigator.onLine ? "No tiene facturas asignadas." : "Sin conexión y sin datos guardados todavía.")}</div>`;
    } else {
      cont.innerHTML = [...grupos.entries()].map(([num, g]) => {
        const seleccionables = g.items.filter((f) => ORDEN[f.estatusVista] < 2);
        return `
        <section class="card" style="margin-bottom:12px">
          <div class="row between">
            <div><strong>Remesa ${esc(num)}</strong><br><small>${esc(g.destino || "")} · ${esc(g.empresa || "")}</small></div>
            ${state.filtro === "abiertas" && seleccionables.length ?
              `<button type="button" class="small" data-selall="${esc(num)}">Seleccionar todas</button>` : ""}
          </div>
          <div style="margin-top:10px">${g.items.map(itemHtml).join("")}</div>
        </section>`;
      }).join("");
    }
    renderBar();
    renderRechazados();
    renderSyncCount();
  }

  function itemHtml(f) {
    const abierta = ORDEN[f.estatusVista] < 3;
    const seleccionable = ORDEN[f.estatusVista] < 2;
    return `
      <div class="fact ${f.pendiente ? "pending" : ""}">
        <div>${abierta && seleccionable ? `<input type="checkbox" data-sel="${f.id}" aria-label="Seleccionar ${esc(f.numero)}" ${state.sel.has(f.id) ? "checked" : ""}>` : ""}</div>
        <div>
          <div class="row between"><strong class="mono">${esc(f.numero)}</strong>${badge(f.estatusVista === "no_entregado" ? "en_transito" : f.estatusVista)}</div>
          ${f.estatusVista === "no_entregado" ? `<div class="alerta-no-entregado" role="status">⚠ Intento anterior: No se entregó - Cliente no Disponible</div>` : ""}
          <div>${esc(f.cliente || "")}</div>
          <small>Pedido ${esc(f.pedido || "-")} · Bultos ${esc(f.bultos ?? "-")}${f.tracking ? ` · <span class="mono">${esc(f.tracking)}</span>` : ""}</small>
          ${f.comentario ? `<div><small>${esc(f.comentario)}</small></div>` : ""}
          ${f.observacion_entrega ? `<div><small><strong>Observación:</strong> ${esc(f.observacion_entrega)}</small></div>` : ""}
          ${f.pendiente ? `<div><small class="lvl-advertencia">Pendiente por subir</small></div>` : ""}
        </div>
        ${abierta ? `<div class="acts">
          <button type="button" class="ok" data-cerrar="entregada" data-id="${f.id}">Entregada</button>
          <button type="button" class="danger" data-cerrar="devolucion" data-id="${f.id}">Devolución</button>
          <button type="button" class="btn-no-entregado" data-cerrar="no_entregado" data-id="${f.id}">No Entregado</button>
        </div>` : ""}
      </div>`;
  }

  function renderBar() {
    const bar = $("#bar");
    bar.classList.toggle("hidden", state.sel.size === 0);
    $("#selTxt").textContent = `${state.sel.size} sel.`;
  }

  function renderRechazados() {
    const box = $("#rech");
    if (!state.rechazados.length) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    box.innerHTML = `<div class="alert err stack">
      <strong>Cambios que el sistema no aceptó</strong>
      ${state.rechazados.map((r) => `<div class="row between"><span><span class="mono">${esc(r.numero || r.factura_id)}</span> → ${esc(TL.ESTATUS[r.estatus])}: ${esc(r.error)}</span>
      <button type="button" class="small" data-okrech="${esc(r.id)}">Entendido</button></div>`).join("")}
    </div>`;
  }

  function renderSyncCount() {
    const n = state.pendientes.length;
    if (!navigator.onLine) setSync(false, `Sin señal · ${n} pendiente(s) guardado(s) en el teléfono`);
    else if (n) setSync(true, `${n} cambio(s) por subir`);
  }
  function setSync(online, txt) {
    $("#dot").classList.toggle("off", !online);
    $("#syncTxt").textContent = txt;
  }

  // ------------------------------------------------------------------ acciones
  async function encolar(facturaId, estatus, extra = {}) {
    const f = state.facturas.find((x) => x.id === facturaId);
    await Outbox.enqueue(me.id, Object.assign({
      id: uuid(), factura_id: facturaId, numero: f ? f.numero : null, estatus, ocurrido_at: new Date().toISOString(),
    }, extra));
  }

  async function lote(estatus, ocurridoAt = null) {
    const lista = merged();
    let n = 0;
    for (const id of state.sel) {
      const f = lista.find((x) => x.id === id);
      if (f && ORDEN[f.estatusVista] < ORDEN[estatus]) { await encolar(id, estatus, ocurridoAt ? { ocurrido_at: ocurridoAt } : {}); n++; }
    }
    state.sel.clear();
    await refreshLocal();
    toast(`${n} factura(s) marcadas como ${TL.ESTATUS[estatus]}`);
    Sync.run();
  }

  let cierre = null;
  function abrirCierre(id, tipo) {
    const f = merged().find((x) => x.id === id);
    if (!f) return;
    cierre = { id, tipo, numero: f.numero };
    const dev = tipo === "devolucion";
    const noEnt = tipo === "no_entregado";
    $("#cierreTitulo").textContent = dev ? "Registrar devolución" : noEnt ? "No Entregado - Cliente No Disponible" : "Registrar entrega";
    $("#cierreInfo").textContent = `${f.numero} · ${f.cliente || ""}`;
    $("#fotoLabel").textContent = dev ? "Foto de la factura con el motivo (obligatoria)"
      : noEnt ? "Foto del lugar de entrega (obligatoria)" : "¿Desea tomar foto de la factura firmada? (opcional)";
    $("#fotoAyuda").textContent = dev ? "Escriba el motivo en la factura antes de tomar la foto."
      : noEnt ? "Tome la foto que demuestre que el cliente no estaba disponible (local cerrado, portón…)." : "Puede confirmar sin foto.";
    // Entregada: observación/notificación de la entrega (quién recibió, faltantes…). Devolución / No Entregado: motivo.
    $("#motivoLabel").textContent = dev ? "Motivo (opcional, también puede escribirlo en la factura)"
      : noEnt ? "Observación (opcional)" : "Observación de la entrega (opcional): quién recibió, faltantes, novedades…";
    $("#motivo").placeholder = dev ? "" : noEnt ? "Ej.: Local cerrado, nadie atendió" : "Ej.: Recibido por Juan Pérez, conforme";
    $("#foto").value = ""; $("#motivo").value = "";
    $("#prev").classList.add("hidden");
    $("#cierreErr").classList.add("hidden");
    $("#gpsAviso").classList.toggle("hidden", !noEnt);
    $("#cierreOk").className = dev ? "danger" : noEnt ? "btn-no-entregado" : "ok";
    $("#cierreOk").textContent = dev ? "Confirmar devolución" : noEnt ? "Confirmar Estatus" : "Confirmar entrega";
    $("#dlgCierre").showModal();
  }

  // Ubicación GPS del teléfono. null si no hay permiso, no hay GPS o tarda demasiado.
  function obtenerUbicacion() {
    return new Promise((resolve) => {
      if (!navigator.geolocation) { resolve(null); return; }
      navigator.geolocation.getCurrentPosition(
        (p) => resolve({
          latitud: p.coords.latitude, longitud: p.coords.longitude,
          precision_m: Number.isFinite(p.coords.accuracy) ? Math.round(p.coords.accuracy) : null,
        }),
        () => resolve(null),
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
      );
    });
  }

  const comprimir = (file) => TL.comprimirImagen(file, 1600, 0.72);
  // Fecha y hora de la toma: la del archivo de la cámara (si es de hoy y no es futura); si no, ahora
  function momentoFoto(file) {
    const ahora = Date.now();
    const t = file && file.lastModified;
    return new Date(t && t <= ahora + 60000 && ahora - t < 24 * 3600 * 1000 ? Math.min(t, ahora) : ahora);
  }

  async function confirmarCierre(ev) {
    ev.preventDefault();
    const file = $("#foto").files[0];
    const err = $("#cierreErr");
    if (cierre.tipo === "devolucion" && !file) {
      err.textContent = "La devolución requiere la foto de la factura."; err.classList.remove("hidden"); return;
    }
    if (cierre.tipo === "no_entregado" && !file) {
      err.textContent = "«No Entregado» requiere una foto."; err.classList.remove("hidden"); return;
    }
    $("#cierreOk").disabled = true;
    try {
      let ubicacion = null;
      if (cierre.tipo === "no_entregado") {
        $("#cierreOk").textContent = "Obteniendo ubicación…";
        ubicacion = await obtenerUbicacion();
        $("#cierreOk").textContent = "Confirmar Estatus";
        if (!ubicacion && !confirm("No se pudo obtener la ubicación GPS (revise que el GPS esté encendido y que la app tenga permiso). ¿Guardar el estatus sin ubicación?")) {
          return;
        }
      }
      let fotoId = null;
      if (file) {
        const blob = await comprimir(file);
        if (blob.size > maxFotoMb * 1024 * 1024) throw new Error(`La foto supera ${maxFotoMb} MB`);
        fotoId = uuid();
        await Outbox.saveFoto(me.id, fotoId, cierre.id, blob, momentoFoto(file).toISOString());
      }
      await encolar(cierre.id, cierre.tipo, Object.assign({ foto_id: fotoId, motivo: $("#motivo").value.trim() || null }, ubicacion || {}));
      $("#dlgCierre").close();
      state.sel.delete(cierre.id);
      await refreshLocal();
      toast({ devolucion: "Devolución registrada", no_entregado: "Registrada como No Entregado" }[cierre.tipo] || "Entrega registrada");
      Sync.run();
    } catch (e) {
      err.textContent = e.message; err.classList.remove("hidden");
    } finally {
      $("#cierreOk").disabled = false;
    }
  }

  async function refreshLocal() { await loadLocal(); render(); }

  // ------------------------------------------------------------------ eventos UI
  $("#lista").addEventListener("change", (e) => {
    const id = e.target.dataset.sel;
    if (!id) return;
    if (e.target.checked) state.sel.add(Number(id)); else state.sel.delete(Number(id));
    renderBar();
  });
  $("#lista").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.cerrar) abrirCierre(Number(b.dataset.id), b.dataset.cerrar);
    if (b.dataset.selall) {
      merged().filter((f) => f.remesa_numero === b.dataset.selall && ORDEN[f.estatusVista] < 2)
        .forEach((f) => state.sel.add(f.id));
      render();
    }
  });
  $("#rech").addEventListener("click", async (e) => {
    const id = e.target.dataset.okrech;
    if (id) { await Outbox.clearRechazado(id); await refreshLocal(); }
  });
  $$("[data-lote]").forEach((b) => b.addEventListener("click", () =>
    (b.dataset.lote === "en_transito" ? abrirPorEntregar() : lote(b.dataset.lote))));

  // Por Entregar: la fecha y hora actual del teléfono o la que indique el conductor
  const MAX_ANTIGUEDAD_MS = 30 * 24 * 3600 * 1000;
  const fechaHoraLocal = (d) => {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  let peMinimo = null;
  function modoPorEntregar(otra) {
    $("#pePregunta").classList.toggle("hidden", otra);
    $("#peOtra").classList.toggle("hidden", !otra);
    for (const id of ["#peNo", "#peSi"]) $(id).classList.toggle("hidden", otra);
    for (const id of ["#peCancelar", "#peConfirmar"]) $(id).classList.toggle("hidden", !otra);
  }
  function abrirPorEntregar() {
    if (!state.sel.size) return;
    const lista = merged().filter((f) => state.sel.has(f.id));
    // No puede ser anterior a la fecha Preparado (en Preparado, estatus_at es ese momento) ni pasar de 30 días
    const preparado = Math.max(0, ...lista.filter((f) => f.estatus === "recibida").map((f) => new Date(f.estatus_at).getTime()));
    peMinimo = new Date(Math.max(preparado, Date.now() - MAX_ANTIGUEDAD_MS));
    $("#peInfo").textContent = `${lista.length} factura(s) seleccionada(s)`;
    $("#peAhora").textContent = TL.fmtFechaHora(new Date().toISOString());
    $("#peFecha").value = fechaHoraLocal(new Date());
    $("#peFecha").max = fechaHoraLocal(new Date());
    $("#peFecha").min = fechaHoraLocal(peMinimo);
    $("#peErr").classList.add("hidden");
    modoPorEntregar(false);
    $("#dlgPorEntregar").showModal();
  }
  $("#peSi").addEventListener("click", () => { $("#dlgPorEntregar").close(); lote("en_transito"); });
  $("#peNo").addEventListener("click", () => { modoPorEntregar(true); $("#peFecha").focus(); });
  $("#peCancelar").addEventListener("click", () => $("#dlgPorEntregar").close());
  $("#formPorEntregar").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const err = $("#peErr");
    const valor = $("#peFecha").value;
    const cuando = valor ? new Date(valor) : null;
    let msg = "";
    if (!cuando || Number.isNaN(cuando.getTime())) msg = "Indique la fecha y hora.";
    else if (cuando.getTime() > Date.now() + 60000) msg = "La fecha y hora no puede ser posterior a este momento.";
    else if (cuando < new Date(peMinimo.getTime() - 60000)) msg = `La fecha y hora no puede ser anterior a ${TL.fmtFechaHora(peMinimo.toISOString())}.`;
    if (msg) { err.textContent = msg; err.classList.remove("hidden"); return; }
    $("#dlgPorEntregar").close();
    lote("en_transito", new Date(Math.min(cuando.getTime(), Date.now())).toISOString());
  });
  $$("[data-filtro]").forEach((b) => b.addEventListener("click", () => {
    $$("[data-filtro]").forEach((x) => x.classList.toggle("active", x === b));
    state.filtro = b.dataset.filtro; state.sel.clear(); render();
  }));
  $("#buscar").addEventListener("input", (e) => { state.q = e.target.value; render(); });
  $("#btnSync").addEventListener("click", () => Sync.run());
  $("#formCierre").addEventListener("submit", confirmarCierre);
  $("#cierreCancelar").addEventListener("click", () => $("#dlgCierre").close());
  $("#foto").addEventListener("change", () => {
    const f = $("#foto").files[0];
    if (!f) { $("#prev").classList.add("hidden"); return; }
    $("#prevImg").src = URL.createObjectURL(f);
    $("#prevHora").textContent = "Foto tomada: " + TL.fmtFechaHora(momentoFoto(f).toISOString());
    $("#prev").classList.remove("hidden");
  });

  // Perfil
  $("#btnMenu").addEventListener("click", async () => {
    $("#perfilMsg").classList.add("hidden");
    $("#pC1").value = ""; $("#pC2").value = "";
    try {
      const p = await api("/api/me/perfil");
      $("#pNombre").value = p.conductor?.nombre || "";
      $("#pPlaca").value = p.conductor?.placa || "";
      $("#pTel").value = p.conductor?.telefono || "";
    } catch (_) { $("#pNombre").value = me.nombre; }
    $("#bioSeccion").innerHTML = "";
    Biometria.seccion($("#bioSeccion"), TL.user() || me);
    $("#dlgPerfil").showModal();
  });
  $("#perfilCerrar").addEventListener("click", () => $("#dlgPerfil").close());
  $("#formPerfil").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#perfilMsg");
    try {
      if (!$("#pC1").value || !$("#pC2").value) throw new Error("Escriba su clave actual y la nueva");
      const r = await api("/api/me/clave", { method: "POST", body: { clave_actual: $("#pC1").value, clave_nueva: $("#pC2").value } });
      TL.setSession(r.access_token, r.usuario);
      $("#pC1").value = ""; $("#pC2").value = "";
      const aviso = $("#avisoClave"); if (aviso) aviso.remove();
      msg.className = "alert ok"; msg.textContent = "Clave cambiada";
    } catch (err) {
      msg.className = "alert err"; msg.textContent = navigator.onLine ? err.message : "Necesita señal para cambiar la clave";
    }
  });
  $("#btnSalir").addEventListener("click", async () => {
    const n = (await Outbox.pending(me.id)).length;
    if (n && !confirm(`Tiene ${n} cambio(s) sin subir. Quedarán guardados en este teléfono hasta que vuelva a entrar. ¿Cerrar sesión?`)) return;
    await TL.cerrarSesion();
    location.replace("/");
  });

  Sync.onChange(async (info) => {
    if (info.estado === "sincronizando") setSync(true, "Sincronizando…");
    if (info.estado === "ok") setSync(true, "Al día · " + info.at.toLocaleTimeString("es-VE", { hour: "2-digit", minute: "2-digit" }));
    if (info.estado === "error") setSync(navigator.onLine, `No se pudo sincronizar. Reintento en ${Math.round(info.reintento / 1000)} s`);
    if (info.estado === "offline") setSync(false, "Sin señal");
    if (info.estado !== "sincronizando") await refreshLocal();
  });

  TL.registrarVista("conductor", "App del conductor");
  (async () => {
    await Outbox.open();
    await refreshLocal();
    Sync.run();
  })();
})();
