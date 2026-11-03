"use strict";
(async () => {
  const { $, $$, api, esc, badge, fmtFechaHora, fmtMomento, fmtFecha, fmtMonto, toast, ESTATUS } = TL;

  const me = TL.user();
  if (!TL.token() || !me || me.rol === "conductor" || me.cambio_clave_obligatorio) { location.replace("/"); return; }
  // Permisos efectivos (RBAC) calculados por el servidor: la interfaz solo muestra lo permitido.
  // El servidor vuelve a validar cada acción; ocultar un botón no es la medida de seguridad.
  // El Consultor recibe en «permisos_interfaz» los botones del Supervisor: los ve, pero al usarlos
  // aparece el aviso de «solo consulta» (common.js y el servidor lo detienen).
  const MISPERM = await api("/api/me/permisos");
  const PERM = new Set(MISPERM.permisos_interfaz && MISPERM.permisos_interfaz.length ? MISPERM.permisos_interfaz : MISPERM.permisos);
  // Página y servidor deben ser de la misma versión. Si no, casi siempre es una ventana vieja del
  // programa que sigue abierta (o una página guardada en caché): se avisa en vez de fallar en silencio.
  const VERSION_PANEL = "1.26.0";
  const cmpVer = (a, b) => { const x = a.split(".").map(Number), y = b.split(".").map(Number); for (let i = 0; i < 3; i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0); } return 0; };
  const AVISO_VERSION_VIEJA = "El programa que está abierto en esta PC es una versión anterior. Cierre TODAS las ventanas negras de «Tracking Logístico» y abra de nuevo «1 - PROBAR EN MI PC.bat» (o vuelva a publicar en Vercel).";
  api("/api/config").then((c) => {
    if (!c.version || cmpVer(c.version, VERSION_PANEL) === 0) return;
    const msg = cmpVer(c.version, VERSION_PANEL) < 0 ? AVISO_VERSION_VIEJA
      : "Hay una versión nueva del programa: recargue la página (Ctrl+F5, o cierre y abra la app en el teléfono).";
    document.querySelector("main").insertAdjacentHTML("beforebegin", `<div class="wrap"><div class="alert err" role="alert" style="margin-top:12px">${esc(msg)}</div></div>`);
  }).catch(() => {});
  const puede = (p) => PERM.has(p);
  // Red de seguridad: un error de la API que ningún botón atrapó se muestra en vez de perderse
  window.addEventListener("unhandledrejection", (e) => {
    const err = e.reason;
    if (err && err.message && typeof err.status === "number") { toast(err.message, 5000); e.preventDefault(); }
  });
  const PUEDE_EDITAR = puede("catalogos.gestionar");
  const ROLES = { supervisor: "Supervisor", transportista: "Transportista", cobranzas: "Cobranzas", consultor: "Consultor", conductor: "Conductor" };
  const SOLO_CONSULTA = MISPERM.solo_consulta || me.rol === "consultor";
  $("#quien").textContent = `${me.nombre} · ${ROLES[me.rol] || me.rol}${me.empresa_nombre ? " · " + me.empresa_nombre : ""}${SOLO_CONSULTA ? " · solo consulta" : ""}`;

  // ------------------------------------------------------------------ empresas (multi-tenant)
  // Solo el Supervisor gestiona empresas y elige la empresa de usuarios/conductores; el Transportista
  // siempre trabaja en la suya (el servidor la asigna y filtra los datos).
  const ES_SUPERVISOR_EMPRESAS = puede("empresas.gestionar");
  let EMPRESAS = [];
  async function cargarEmpresas() {
    if (puede("empresas.ver")) EMPRESAS = await api("/api/admin/empresas?incluir_inactivas=false");
    return EMPRESAS;
  }
  const nombreEmpresa = (id) => (EMPRESAS.find((e) => e.id === id) || {}).nombre || "";
  const campoEmpresa = (valor, label = "Empresa de transporte") => ES_SUPERVISOR_EMPRESAS
    ? [{ name: "empresa_id", label, type: "select", value: valor ?? "", options: [["", "— Sin empresa —"], ...EMPRESAS.map((e) => [e.id, e.nombre])] }]
    : [];
  const conEmpresa = (o) => { if ("empresa_id" in o) o.empresa_id = o.empresa_id ? Number(o.empresa_id) : null; return o; };
  const selectorEmpresa = (id, label) => ES_SUPERVISOR_EMPRESAS
    ? `<div><label for="${id}">${esc(label)}</label><select id="${id}"><option value="">— Seleccione —</option>
        ${EMPRESAS.map((e) => `<option value="${e.id}">${esc(e.nombre)}</option>`).join("")}</select></div>` : "";
  const valorSelector = (id) => { const s = $("#" + id); return s && s.value ? Number(s.value) : null; };
  await cargarEmpresas();

  // ------------------------------------------------------------------ diálogo genérico
  const dlg = $("#dlg");
  let dlgSubmit = null;
  function abrirDialogo(html, botones, onSubmit) {
    $("#dlgBody").innerHTML = html;
    $("#dlgFoot").innerHTML = botones;
    dlgSubmit = onSubmit;
    dlg.showModal();
    const first = $("#dlgBody input, #dlgBody select, #dlgBody textarea");
    if (first) first.focus();
  }
  $("#dlgForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!dlgSubmit) { dlg.close(); return; }
    const err = $("#dlgErr");
    try {
      const cerrar = await dlgSubmit(new FormData($("#dlgForm")), e.submitter);
      if (cerrar !== false) dlg.close();
    } catch (ex) {
      if (err) { err.textContent = ex.message; err.classList.remove("hidden"); } else toast(ex.message);
    }
  });
  $("#dlgFoot").addEventListener("click", (e) => { if (e.target.dataset.cerrar !== undefined) dlg.close(); });

  function campo(f) {
    const v = f.value ?? "";
    if (f.type === "nota") {
      return '<small class="muted">Se le crea su usuario (la cédula) con una clave temporal que verá al guardar, para que use la app del conductor.</small>';
    }
    if (f.type === "select") {
      return `<div><label for="f_${f.name}">${esc(f.label)}</label><select id="f_${f.name}" name="${f.name}">
        ${f.options.map(([k, t]) => `<option value="${esc(k)}" ${String(k) === String(v) ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>`;
    }
    if (f.type === "checkbox") {
      return `<div class="row"><input type="checkbox" id="f_${f.name}" name="${f.name}" ${v ? "checked" : ""}><label for="f_${f.name}" style="margin:0">${esc(f.label)}</label></div>`;
    }
    if (f.type === "textarea") {
      return `<div><label for="f_${f.name}">${esc(f.label)}</label><textarea id="f_${f.name}" name="${f.name}" ${f.required ? "required" : ""}>${esc(v)}</textarea></div>`;
    }
    return `<div><label for="f_${f.name}">${esc(f.label)}</label><input id="f_${f.name}" name="${f.name}" type="${f.type || "text"}"
      value="${esc(v)}" ${f.required ? "required" : ""} ${f.readonly ? "readonly" : ""} ${f.min ? `minlength="${f.min}"` : ""}></div>`;
  }
  function formulario(titulo, campos, onSubmit, okTxt = "Guardar") {
    abrirDialogo(`<h2 style="margin-top:0">${esc(titulo)}</h2>${campos.map(campo).join("")}<div id="dlgErr" class="alert err hidden"></div>`,
      `<button type="button" data-cerrar>Cancelar</button><button type="submit" class="primary">${esc(okTxt)}</button>`, onSubmit);
  }
  function datos(fd, campos) {
    const o = {};
    for (const c of campos) {
      if (c.type === "checkbox") o[c.name] = fd.get(c.name) === "on";
      else { const v = (fd.get(c.name) || "").toString().trim(); o[c.name] = v === "" ? null : v; }
    }
    return o;
  }
  function mostrarCredenciales(creds, titulo = "Claves temporales") {
    if (!creds || !creds.length) return;
    abrirDialogo(`<h2 style="margin-top:0">${esc(titulo)}</h2>
      <p class="muted">Entregue cada clave a su dueño. Con la clave temporal puede entrar <strong>3 veces</strong> (el sistema le avisa
        cuántos accesos le quedan); después deberá crear su clave personal.</p>
      <div class="table-wrap"><table><tr><th>Usuario</th><th>Nombre</th><th>Clave temporal</th></tr>
      ${creds.map((c) => `<tr><td class="mono">${esc(c.usuario)}</td><td>${esc(c.nombre)}</td><td class="mono"><strong>${esc(c.clave_temporal)}</strong></td></tr>`).join("")}
      </table></div>`, `<button type="button" id="btnPrint">Imprimir</button><button type="submit" class="primary">Listo</button>`, null);
    $("#btnPrint").onclick = () => window.print();
  }

  // Alta rápida de conductor desde «Cargar REMESA» (foto de factura o Excel) cuando no está registrado.
  // El Transportista lo crea en su empresa (lo asigna el servidor); el Supervisor elige la empresa.
  const PUEDE_CREAR_CONDUCTOR = puede("conductores.crear");
  const camposConductor = (c = {}) => [
    { name: "cedula", label: "Cédula *", value: c.cedula, required: true },
    { name: "nombre", label: "Nombre y apellido *", value: c.nombre, required: true },
    { name: "telefono", label: "Teléfono", value: c.telefono },
    { name: "placa", label: "Placa del camión", value: c.placa },
    ...campoEmpresa(c.empresa_id ?? "", "Empresa de transporte *"),
  ];
  function nuevoConductor(prefill, alCrear) {
    const campos = camposConductor(prefill);
    formulario("Nuevo conductor", [...campos, { name: "_nota", type: "nota" }], async (fd) => {
      const body = conEmpresa(datos(fd, campos));
      if (ES_SUPERVISOR_EMPRESAS && !body.empresa_id) throw new Error("Elija la empresa de transporte del conductor.");
      const r = await api("/api/admin/conductores", { method: "POST", body });
      conductoresP = null;  // la lista de conductores de la foto se vuelve a pedir
      alCrear(r.conductor);
      if (r.credencial) setTimeout(() => mostrarCredenciales([r.credencial], "Conductor creado: entréguele su clave"), 50);
    }, "Crear conductor");
  }

  // ------------------------------------------------------------------ navegación
  const TABS = [
    { id: "inicio", t: "Inicio", fn: vistaInicio },
    { id: "cargar", t: "Cargar REMESA", fn: vistaCargar, perm: "remesas.importar" },
    { id: "remesas", t: "Remesas", fn: vistaRemesas },
    { id: "facturas", t: "Facturas", fn: vistaFacturas },
    { id: "conductores", t: "Conductores", fn: vistaConductores },
    { id: "camiones", t: "Camiones", fn: vistaCamiones },
    { id: "vendedores", t: "Vendedores", fn: vistaVendedores },
    { id: "feriados", t: "Feriados", fn: vistaFeriados, perm: "catalogos.ver" },
    { id: "usuarios", t: "Usuarios", fn: vistaUsuarios, perm: "usuarios.ver" },
    { id: "empresas", t: "Empresas", fn: vistaEmpresas, perm: "empresas.ver" },
    { id: "reportes", t: "Cobranzas y reportes", fn: vistaReportes },
    { id: "auditoria", t: "Auditoría", fn: vistaAuditoria, perm: "auditoria.ver" },
  ].filter((t) => !t.perm || puede(t.perm));

  $("#tabs").innerHTML = TABS.map((t) => `<button type="button" data-tab="${t.id}">${esc(t.t)}</button>`).join("");
  $("#tabs").addEventListener("click", (e) => { const id = e.target.dataset.tab; if (id) ir(id); });

  async function ir(id) {
    const tab = TABS.find((t) => t.id === id) || TABS[0];
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab.id));
    history.replaceState(null, "", "#" + tab.id);
    TL.registrarVista(tab.id, tab.t);  // bitácora: módulo consultado
    $("#view").innerHTML = '<div class="card muted">Cargando…</div>';
    try { await tab.fn($("#view")); } catch (e) { $("#view").innerHTML = `<div class="alert err">${esc(e.message)}</div>`; }
  }

  // ------------------------------------------------------------------ Inicio
  async function vistaInicio(v) {
    // La métrica estrella solo la ve el Supervisor (el servidor también lo exige)
    const verMetrica = puede("metricas.ver");
    const [d, m] = await Promise.all([api("/api/dashboard"), verMetrica ? api("/api/reportes/metricas") : null]);
    const kpi = (t, n) => `<div class="card"><small>${esc(t)}</small><div class="kpi">${esc(n ?? 0)}</div></div>`;
    v.innerHTML = `
      <h1>Resumen</h1>
      <div class="grid">
        ${kpi("Remesas activas", d.remesas_activas)}
        ${Object.values(ESTATUS).map((l) => kpi(l, d.por_estatus[l] || 0)).join("")}
      </div>
      ${m ? `<h2>Métrica estrella (últimos 7 días)</h2>
      <div class="grid">
        ${kpi("Cambios registrados el mismo día", m.pct_mismo_dia === null ? "—" : m.pct_mismo_dia + " %")}
        ${kpi("Meta", m.meta_pct_mismo_dia + " %")}
        ${kpi("Cambios sin señal sincronizados", m.cambios_sin_senal_sincronizados)}
        ${kpi("Consultas públicas", m.consultas_publicas)}
      </div>` : ""}
      <h2>Conductores en ruta</h2>
      <div class="card table-wrap">${d.choferes_en_ruta.length ? `<table><tr><th>Conductor</th><th>Facturas</th><th>Cerradas</th><th style="width:40%">Avance</th></tr>
        ${d.choferes_en_ruta.map((c) => `<tr><td>${esc(c.chofer)}</td><td>${c.facturas}</td><td>${c.cerradas}</td>
        <td><div class="progress"><span style="width:${Math.round(100 * c.cerradas / Math.max(1, c.facturas))}%"></span></div></td></tr>`).join("")}</table>`
        : '<span class="muted">No hay remesas activas.</span>'}</div>`;
  }

  // ------------------------------------------------------------------ Cargar REMESA
  async function vistaCargar(v) {
    v.innerHTML = `
      <h1>Cargar REMESA</h1>
      <section class="card stack captura" aria-labelledby="capTit">
        <div class="row between">
          <div><h2 id="capTit" style="margin:0">Factura en papel</h2>
            <small class="muted">Tome una foto de la factura: la IA lee los datos, usted los revisa, asigna el conductor y se guarda.<br>
              Para leer mejor: factura completa y de frente, con buena luz, sin sombras ni reflejos.</small></div>
          <div class="row">
            <button type="button" class="primary" id="btnFoto">📷 Tomar Foto de Factura</button>
            <button type="button" id="btnImg">Elegir imagen</button>
          </div>
        </div>
        <input type="file" id="fotoCam" accept="image/*" capture="environment" hidden>
        <input type="file" id="fotoArch" accept="image/jpeg,image/png,image/webp,image/*" hidden>
        <small id="ocrEstado" class="muted" aria-live="polite"></small>
        <div id="capOut" aria-live="polite"></div>
        <div id="capPend"></div>
      </section>
      <h2>Hoja REMESA (Excel)</h2>
      <form id="up" class="card stack">
        <div><label for="arch">Archivo Excel (.xlsx)</label><input id="arch" type="file" accept=".xlsx,.xlsm" required></div>
        <div id="hojaBox" class="hidden"><label for="hoja">Hoja</label><select id="hoja"></select></div>
        <fieldset class="filtro-fac">
          <legend>Fecha Fac (opcional)</legend>
          <div class="row">
            <label for="facDesde" style="margin:0">Desde</label><input id="facDesde" type="date" style="width:auto">
            <label for="facHasta" style="margin:0">Hasta</label><input id="facHasta" type="date" style="width:auto">
            <button type="button" class="small" id="facLimpiar">Quitar fechas</button>
          </div>
          <small class="muted">Solo se analizan y cargan las facturas cuya <strong>Fecha Fac</strong> esté en este rango (ambos días incluidos); las demás se ignoran en esta carga. Vacío = toda la hoja.</small>
        </fieldset>
        <div id="facErr" class="alert err hidden" role="alert"></div>
        <div class="row"><button class="primary" type="submit" id="btnAn">Analizar</button>
        <small class="muted">${SOLO_CONSULTA ? "Modo consulta: puede analizar la hoja completa; no se registra nada." : "Nada se guarda hasta que usted confirme el resumen."}</small></div>
      </form>
      <div id="res" class="stack" style="margin-top:16px"></div>
      <h2>Cargas recientes</h2><div id="hist" class="card table-wrap"></div>`;
    // El Excel se lee en el navegador y solo se envían los valores de la hoja (límite de 4,5 MB en Vercel).
    let libro = null;
    $("#arch").addEventListener("change", async () => {
      libro = null;
      const f = $("#arch").files[0];
      if (!f) return;
      $("#res").innerHTML = '<div class="card muted">Abriendo el libro…</div>';
      try {
        libro = await XlsxLite.open(f);
        const det = await libro.detectRemesaSheet();
        $("#hoja").innerHTML = libro.sheetNames.map((h) => `<option ${h === det ? "selected" : ""}>${esc(h)}</option>`).join("");
        $("#hojaBox").classList.remove("hidden");
        $("#res").innerHTML = det ? `<div class="alert ok">Hoja de remesas detectada: «${esc(det)}». Pulse Analizar.</div>`
          : '<div class="alert warn">No se detectó automáticamente la hoja de remesas: elíjala en la lista.</div>';
      } catch (ex) {
        $("#res").innerHTML = `<div class="alert warn">No se pudo leer el libro en el navegador (${esc(ex.message)}). Se enviará el archivo completo.</div>`;
      }
    });
    // Rango de «Fecha Fac»: se valida aquí y lo aplica el servidor al leer la hoja (vacío = toda la hoja)
    const rangoFac = () => {
      const desde = $("#facDesde").value || null, hasta = $("#facHasta").value || null;
      if (desde && hasta && desde > hasta) throw new Error("En Fecha Fac, «Desde» no puede ser posterior a «Hasta».");
      return { fecha_fac_desde: desde, fecha_fac_hasta: hasta };
    };
    const validarRango = () => {
      try { rangoFac(); $("#facErr").classList.add("hidden"); return true; } catch (ex) {
        $("#facErr").textContent = ex.message; $("#facErr").classList.remove("hidden"); return false;
      }
    };
    $("#facDesde").onchange = validarRango;
    $("#facHasta").onchange = validarRango;
    $("#facLimpiar").onclick = () => { $("#facDesde").value = ""; $("#facHasta").value = ""; validarRango(); };
    $("#up").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = $("#arch").files[0];
      if (!f || !validarRango()) return;
      const rango = rangoFac();
      $("#btnAn").disabled = true;
      $("#res").innerHTML = '<div class="card muted">Leyendo el archivo… (los libros grandes pueden tardar unos segundos)</div>';
      try {
        let imp;
        if (libro) {
          const hoja = $("#hoja").value;
          const filas = await libro.readSheet(hoja);
          const body = await XlsxLite.gzipJson({ archivo: f.name, hoja, hojas: libro.sheetNames, sha256: await libro.sha256(), filas, ...rango });
          imp = await api(SOLO_CONSULTA ? "/api/importaciones/analizar" : "/api/importaciones/filas", { method: "POST", body });
        } else {
          if (f.size > 4.4 * 1024 * 1024) throw new Error("El archivo es demasiado grande para enviarlo completo. Use un navegador actualizado (Chrome, Edge, Firefox).");
          const fd = new FormData();
          fd.append("archivo", f);
          if (!$("#hojaBox").classList.contains("hidden") && $("#hoja").value) fd.append("hoja", $("#hoja").value);
          if (rango.fecha_fac_desde) fd.append("fecha_fac_desde", rango.fecha_fac_desde);
          if (rango.fecha_fac_hasta) fd.append("fecha_fac_hasta", rango.fecha_fac_hasta);
          imp = await api(SOLO_CONSULTA ? "/api/importaciones/analizar-archivo" : "/api/importaciones", { method: "POST", body: fd });
        }
        mostrarResumen(imp);
      } catch (ex) {
        $("#res").innerHTML = `<div class="alert err">${esc(ex.message)}</div>`;
      } finally { $("#btnAn").disabled = false; }
    });
    cargarHistorial();
    iniciarCaptura();
  }

  // ------------------------------------------------------------------ Captura de factura (foto + OCR)
  const CAMPOS_CAPTURA = [
    { k: "numero", t: "Número de factura", req: true },
    { k: "pedido", t: "Número de pedido" },
    { k: "cliente", t: "Cliente", req: true, ancho: true },
    { k: "empresa_emisora", t: "Empresa emisora" },
    { k: "fecha_fac", t: "Fecha de la factura", tipo: "date" },
    { k: "cod_vendedor", t: "Código de vendedor" },
    { k: "bultos", t: "Bultos", tipo: "number", step: "1" },
    { k: "total_fact", t: "Total factura (Subtotal)", tipo: "number", step: "0.0001" },
    { k: "porcentaje", t: "Descuento (%)", tipo: "number", step: "0.01", pct: true },
    { k: "total_neto", t: "Total neto (Base imponible)", tipo: "number", step: "0.0001" },
  ];

  // Lo que la foto necesita se prepara al abrir la pantalla (lector de texto y conductores), así al
  // tomar la foto el formulario aparece al instante y los datos se van rellenando solos.
  let conductoresP = null;
  const conductoresActivos = () => (conductoresP ||= api("/api/admin/conductores")
    .then((cs) => cs.filter((c) => c.activo)).catch((e) => { conductoresP = null; throw e; }));

  async function iniciarCaptura() {
    const cfg = await api("/api/config").catch(() => ({ ocr_modo: "local" }));
    // El Consultor siempre extrae en el dispositivo: la foto no se sube ni se guarda
    const ocrLocal = (cfg.ocr_modo !== "ia" || SOLO_CONSULTA) && typeof OcrLocal !== "undefined" && OcrLocal.disponible();
    conductoresActivos().catch(() => {});
    if (ocrLocal) {
      const st = $("#ocrEstado");
      st.textContent = "Preparando el lector de facturas…";
      OcrLocal.precalentar((x) => {
        const s = $("#ocrEstado");
        if (s && x < 1) s.textContent = `Preparando el lector de facturas… ${Math.round(x * 100)} % (solo la primera vez)`;
      })
        .then(() => { const s = $("#ocrEstado"); if (s) s.textContent = "✓ Lector de facturas listo"; })
        .catch((e) => {
          console.warn("lector", e);
          const s = $("#ocrEstado");
          if (s) s.textContent = `⚠ No se pudo preparar el lector de facturas (${e.message || e}). Recargue la página; si sigue igual, escriba los datos a mano.`;
        });
    }
    const procesar = async (input) => {
      const file = input.files[0];
      input.value = "";
      if (!file) return;
      // 1) Formulario inmediato con la foto  2) subida y lectura en paralelo  3) los datos llegan solos
      const form = await formularioCaptura(null, URL.createObjectURL(file), { leyendo: true });
      const comprimida = TL.comprimirImagen(file, 2000, 0.85);
      // Consultor: extracción completa sin subir la foto ni crear la captura (no se registra nada)
      const ANALISIS = { id: "", estado: "analisis", campos: {}, avisos: [], motor: "lectura en curso" };
      const subida = SOLO_CONSULTA ? Promise.resolve(ANALISIS) : comprimida.then((blob) => {
        const fd = new FormData();
        fd.append("archivo", blob, (file.name || "factura").replace(/\.[^.]+$/, "") + ".jpg");
        if (ocrLocal) fd.append("ocr_en_curso", "true");
        // Hora en que se tomó la foto: será la Fecha Preparado (y la fecha de «Preparado» en el historial)
        if (file.lastModified) fd.append("tomada_at", new Date(file.lastModified).toISOString());
        return api("/api/capturas", { method: "POST", body: fd });
      });
      let cadena = Promise.resolve();  // las lecturas se aplican en orden (encabezado y pie → todo)
      const enviar = (cap, l, final) => (cadena = cadena.then(async () => {
        const body = { texto: l.texto, confianza: l.confianza, cajas: l.cajas };
        const r = await api(cap.id ? `/api/capturas/${cap.id}/lectura` : "/api/capturas/analizar", { method: "POST", body });
        form.llenar(r, final);
      }).catch((ex) => {
        console.warn("lectura", ex);
        if (ex.status === 404) form.error(AVISO_VERSION_VIEJA);
        else if (final) form.finLectura("No se pudieron interpretar los datos: complételos mirando la foto.");
      }));
      // El lector ocupa el procesador: primero se prepara la foto a subir, así la subida va en paralelo
      const lectura = ocrLocal
        ? comprimida.catch(() => null).then(() => OcrLocal.leer(file, {
          onParcial: (l) => { subida.then((cap) => enviar(cap, l, false)).catch(() => {}); },
        }))
        : null;
      let cap;
      try {
        cap = await subida;
        form.asociar(cap);
      } catch (ex) { form.error(ex.status === 404 ? AVISO_VERSION_VIEJA : ex.message); return; }
      finally { cargarPendientes(); }
      if (!lectura) { form.llenar(cap, true); return; }
      try { await enviar(cap, await lectura, true); }
      catch (ex) {
        console.warn("OCR local", ex);
        form.finLectura(`No se pudo leer el texto en este dispositivo (${ex.message || ex}): complete los datos mirando la foto.`);
      }
    };
    $("#btnFoto").onclick = () => $("#fotoCam").click();
    $("#btnImg").onclick = () => $("#fotoArch").click();
    $("#fotoCam").onchange = (e) => procesar(e.target);
    $("#fotoArch").onchange = (e) => procesar(e.target);
    $("#capPend").addEventListener("click", async (e) => {
      const id = e.target.dataset.revisar;
      if (!id) return;
      try {
        const cap = await api("/api/capturas/" + id);
        const img = await api(cap.imagen_url, { raw: true }).then((r) => r.blob());
        const form = await formularioCaptura(cap, URL.createObjectURL(img), { leyendo: false });
        form.asociar(cap);
        form.llenar(cap, true);
        $("#capOut").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (ex) { toast(ex.message); }
    });
    cargarPendientes();
  }

  let capturaEnRevision = null;
  async function cargarPendientes() {
    const box = $("#capPend");
    if (!box) return;
    const lista = (await api("/api/capturas?estado=pendiente&limite=10").catch(() => []))
      .filter((c) => c.id !== capturaEnRevision);  // la que está abierta en el formulario no se repite
    box.innerHTML = lista.length ? `<details><summary>Fotos pendientes de revisión (${lista.length})</summary>
      <div class="table-wrap"><table><tr><th>Tomada</th><th>Factura</th><th>Cliente</th><th></th></tr>
      ${lista.map((c) => `<tr><td>${esc(fmtFechaHora(c.created_at))}</td><td class="mono">${esc(c.campos.numero?.valor || "—")}</td>
        <td>${esc(c.campos.cliente?.valor || "")}</td><td><button type="button" class="small" data-revisar="${esc(c.id)}">Revisar</button></td></tr>`).join("")}
      </table></div></details>` : "";
  }

  /** Dibuja el formulario de revisión y devuelve un controlador para ir llenándolo:
   *  asociar(captura) habilita guardar; llenar(captura, final) vuelca la lectura sin pisar lo que el
   *  operador ya escribió; finLectura(msg) y error(msg) informan. */
  async function formularioCaptura(capInicial, imgUrl, { leyendo }) {
    let cap = capInicial;
    capturaEnRevision = cap ? cap.id : null;
    const conductores = await conductoresActivos().catch(() => []);
    $("#capOut").innerHTML = `
      <form id="capForm" class="captura-form">
        <div class="captura-img">
          <a href="${esc(imgUrl)}" target="_blank" rel="noopener" title="Abrir la foto completa"><img src="${esc(imgUrl)}" alt="Foto de la factura"></a>
          <small class="muted" id="capMotor"></small>
        </div>
        <div class="stack">
          <div id="capLeyendo" class="procesando ${leyendo ? "" : "hidden"}" role="status">
            <span class="spinner" aria-hidden="true"></span><span>Procesando factura con IA... <small class="muted">puede ir eligiendo el conductor</small></span></div>
          ${SOLO_CONSULTA ? '<div class="alert">Modo consulta: se extraen todos los datos de la foto, pero la factura <strong>no se registra</strong> y la foto no se guarda.</div>' : ""}
          <div id="capAvisos"></div>
          <div class="grid campos-captura">
            ${CAMPOS_CAPTURA.map((f) => `<div id="cw_${f.k}" class="${f.ancho ? "ancho" : ""}">
              <label for="cf_${f.k}">${esc(f.t)}${f.req ? " *" : ""} <span class="conf-slot"></span></label>
              <input id="cf_${f.k}" name="${f.k}" type="${f.tipo || "text"}" ${f.step ? `step="${f.step}" min="0"` : ""} ${f.req ? "required" : ""} class="${leyendo ? "leyendo" : ""}">
              <small class="muted leido"></small></div>`).join("")}
          </div>
          <div id="capCoh" class="lvl-advertencia" role="status"></div>
          <h3 style="margin-bottom:0">Asignación</h3>
          <small class="muted">Fecha Preparado: la fecha y hora en que se tomó la foto (también es la fecha de «Preparado» en el historial). La fecha Por Entregar y la Fecha Entregado se guardan cuando el conductor marque esos estatus.</small>
          <div class="grid">
            <div><label for="cfCond">Conductor *</label><select id="cfCond" required>
              <option value="">— Seleccione el conductor —</option>
              ${conductores.map((x) => `<option value="${x.id}">${esc(x.nombre)}${x.placa ? " · " + esc(x.placa) : ""}</option>`).join("")}
              ${PUEDE_CREAR_CONDUCTOR ? '<option value="__nuevo">＋ Crear conductor nuevo…</option>' : ""}</select>
              ${PUEDE_CREAR_CONDUCTOR ? '<button type="button" class="link small" id="cfNuevoCond">¿No aparece? Crear conductor nuevo</button>' : ""}</div>
            <div><label for="cfRem">Remesa</label><select id="cfRem" disabled><option value="">Nueva remesa</option></select></div>
            <div class="rem-nueva"><label for="cfDest">Destino (remesa nueva)</label><input id="cfDest" maxlength="160"></div>
            <div class="rem-nueva"><label for="cfNum">Número de remesa (opcional)</label><input id="cfNum" maxlength="40" placeholder="Se genera si lo deja vacío"></div>
          </div>
          <div id="capErr" class="alert err hidden"></div>
          <div class="row"><button type="submit" class="primary" id="capOk" disabled>Registrar factura</button>
            <button type="button" id="capDesc" disabled>Descartar foto</button></div>
        </div>
      </form>`;

    const form = $("#capForm");
    // Lo que el operador toca ya no lo cambia una lectura que llegue después
    for (const f of CAMPOS_CAPTURA) {
      $("#cf_" + f.k).addEventListener("input", (e) => { e.target.dataset.editado = "1"; e.target.classList.remove("leyendo"); });
    }
    const num = (k) => { const x = $("#cf_" + k).value; return x === "" ? null : Number(x); };
    const coherencia = () => {
      const t = num("total_fact"), n = num("total_neto"), p = num("porcentaje") || 0;
      const esperado = t === null ? null : t * (1 - p / 100);
      $("#capCoh").textContent = (t !== null && n !== null && Math.abs(esperado - n) > 0.05)
        ? `El neto no cuadra: total × (1 − descuento) = ${fmtMonto(esperado)}` : "";
    };
    ["total_fact", "total_neto", "porcentaje"].forEach((k) => $("#cf_" + k).addEventListener("input", coherencia));

    const crearDesdeFoto = () => nuevoConductor({}, (c) => {
      const opt = document.createElement("option");
      opt.value = c.id; opt.textContent = c.nombre + (c.placa ? " · " + c.placa : "");
      const sel = $("#cfCond");
      if (!sel) return;
      sel.insertBefore(opt, sel.querySelector('option[value="__nuevo"]'));
      sel.value = String(c.id);
      sel.onchange();
      toast(`Conductor ${c.nombre} creado y seleccionado`);
    });
    if ($("#cfNuevoCond")) $("#cfNuevoCond").onclick = crearDesdeFoto;
    $("#cfCond").onchange = async () => {
      if ($("#cfCond").value === "__nuevo") { $("#cfCond").value = ""; $("#cfCond").onchange(); crearDesdeFoto(); return; }
      const id = $("#cfCond").value;
      $("#cfRem").innerHTML = '<option value="">Nueva remesa</option>';
      $("#cfRem").disabled = !id;
      if (!id) return;
      const r = await api(`/api/remesas?conductor_id=${id}&estado=activas&limit=50`);
      $("#cfRem").innerHTML += r.items.map((m) => `<option value="${m.id}">${esc(m.numero)} · ${esc(m.destino || "sin destino")} · ${m.total_facturas} fact.</option>`).join("");
      $("#cfRem").onchange();
    };
    $("#cfRem").onchange = () => $$(".rem-nueva").forEach((d) => d.classList.toggle("hidden", !!$("#cfRem").value));

    $("#capDesc").onclick = async () => {
      if (cap && !cap.id) { $("#capOut").innerHTML = '<div class="alert">Foto descartada (en modo consulta no se había guardado).</div>'; return; }
      if (!cap || !confirm("¿Descartar esta foto? No se registrará ninguna factura.")) return;
      await api(`/api/capturas/${cap.id}/descartar`, { method: "POST" });
      $("#capOut").innerHTML = '<div class="alert">Foto descartada.</div>';
      capturaEnRevision = null;
      cargarPendientes();
    };
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!cap) return;
      const body = {};
      for (const f of CAMPOS_CAPTURA) {
        const x = $("#cf_" + f.k).value.trim();
        body[f.k] = x === "" ? null : (f.tipo === "number" ? (f.pct ? +(Number(x) / 100).toFixed(4) : Number(x)) : x);
      }
      Object.assign(body, {
        conductor_id: Number($("#cfCond").value),
        remesa_id: $("#cfRem").value ? Number($("#cfRem").value) : null,
        destino: $("#cfDest").value.trim() || null,
        remesa_numero: $("#cfNum").value.trim() || null,
      });
      $("#capOk").disabled = true;
      try {
        const r = await api(`/api/capturas/${cap.id}/confirmar`, { method: "POST", body });
        $("#capOut").innerHTML = `<div class="alert ok">Factura <strong class="mono">${esc(r.factura_numero)}</strong> registrada en estatus Preparado,
          remesa <strong class="mono">${esc(r.remesa_numero)}</strong>${r.remesa_nueva ? " (nueva)" : ""}, conductor ${esc(r.conductor)}.
          <button type="button" class="small" id="capOtra">📷 Tomar otra foto</button></div>`;
        $("#capOtra").onclick = () => $("#fotoCam").click();
        capturaEnRevision = null;
        cargarPendientes();
      } catch (ex) {
        $("#capErr").textContent = ex.message; $("#capErr").classList.remove("hidden");
      } finally { const b = $("#capOk"); if (b) b.disabled = false; }
    });

    const vivo = () => document.body.contains(form);
    const ctl = {
      asociar(c) {
        if (!vivo()) return;
        cap = c; capturaEnRevision = c.id;
        $("#capOk").disabled = false; $("#capDesc").disabled = false;
      },
      llenar(c, final) {
        if (!vivo()) return;
        cap = c;
        const campos = c.campos || {};
        const manual = c.motor === "manual";
        for (const f of CAMPOS_CAPTURA) {
          const x = campos[f.k] || {};
          const input = $("#cf_" + f.k), caja = $("#cw_" + f.k);
          const tiene = x.valor !== null && x.valor !== undefined;
          if (!input.dataset.editado && tiene) {
            const v = f.pct ? String(+(Number(x.valor) * 100).toFixed(4)) : String(x.valor);
            if (input.value !== v) { input.value = v; input.classList.add("recien"); setTimeout(() => input.classList.remove("recien"), 900); }
          }
          if (tiene || final) input.classList.remove("leyendo");
          caja.classList.toggle("dudoso", !input.dataset.editado && !manual && (final || tiene) && !!x.dudoso);
          $(".conf-slot", caja).innerHTML = tiene && x.confianza !== null && x.confianza !== undefined
            ? `<small class="conf ${x.dudoso ? "baja" : ""}" title="Confianza de la lectura">${Math.round(x.confianza * 100)} %</small>` : "";
          const plano = (s) => String(s ?? "").toUpperCase().replace(/[^A-Z0-9]/g, "");
          const distinto = x.texto && !plano(x.texto).startsWith(plano(input.value).slice(0, 6));
          $(".leido", caja).textContent = x.dudoso && distinto ? `Leído: «${x.texto}»` : "";
        }
        const avisos = final ? c.avisos || [] : [];
        $("#capAvisos").innerHTML = avisos.length ? `<div class="alert warn"><strong>Revise antes de guardar:</strong>
          <ul style="margin:4px 0 0;padding-left:18px">${avisos.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>` : "";
        $("#capMotor").textContent = `${manual ? "Sin lectura automática" : c.motor.startsWith("anthropic") ? "Leída con IA" : "Leída en este dispositivo"} · ${fmtFechaHora(c.created_at)}`;
        coherencia();
        if (final) ctl.finLectura();
      },
      finLectura(msg) {
        if (!vivo()) return;
        $("#capLeyendo").classList.add("hidden");
        $$(".campos-captura input.leyendo").forEach((i) => i.classList.remove("leyendo"));
        if (msg) $("#capAvisos").innerHTML = `<div class="alert warn">${esc(msg)}</div>`;
      },
      error(msg) {
        if (!vivo()) return;
        ctl.finLectura();
        $("#capErr").textContent = msg; $("#capErr").classList.remove("hidden");
      },
    };
    $("#cfCond").focus({ preventScroll: true });
    return ctl;
  }

  async function cargarHistorial() {
    const lista = await api("/api/importaciones?limite=15");
    $("#hist").innerHTML = lista.length ? `<table><tr><th>#</th><th>Archivo</th><th>Hoja</th><th>Estado</th><th>Fecha</th><th>Resultado</th></tr>
      ${lista.map((i) => `<tr><td>${i.id}</td><td>${esc(i.archivo_nombre)}</td><td>${esc(i.hoja)}${i.filtro_fecha_fac ? `<br><small class="muted">Fecha Fac ${esc(i.filtro_fecha_fac)}</small>` : ""}</td><td>${esc(i.estado)}</td>
      <td>${esc(fmtFechaHora(i.created_at))}</td><td>${i.resultado ? `${i.resultado.facturas_creadas} facturas, ${i.resultado.remesas_creadas} remesas` : ""}</td></tr>`).join("")}</table>`
      : '<span class="muted">Sin cargas todavía.</span>';
  }

  function mostrarResumen(imp) {
    const r = imp.resumen, t = r.totales;
    const hojaSel = $("#hoja");
    hojaSel.innerHTML = r.hojas_disponibles.map((h) => `<option ${h === r.hoja ? "selected" : ""}>${esc(h)}</option>`).join("");
    $("#hojaBox").classList.remove("hidden");
    const kpi = (tt, n, cls = "") => `<div class="card"><small>${esc(tt)}</small><div class="kpi ${cls}">${esc(n)}</div></div>`;
    const avisos = r.avisos;
    const flt = r.filtro_fecha_fac;
    const CLASE = { nueva: "Nueva", reenvio: "Reenvío (tras devolución)", ya_cargada: "Ya cargada",
      en_otra_remesa: "En otra remesa (se omite)", en_otra_empresa: "De otra empresa (se omite)", remesa_excluida: "Remesa excluida" };
    const claseCls = (c) => ["nueva", "reenvio"].includes(c) ? "ok-txt" : ["en_otra_remesa", "en_otra_empresa", "remesa_excluida"].includes(c) ? "lvl-advertencia" : "muted";
    const filtroHtml = !flt ? "" : `<div class="alert ok filtro-fac-res">
        <strong>Filtro Fecha Fac ${esc(flt.texto)}:</strong> se analizan <strong>${flt.facturas_en_rango}</strong> de ${flt.facturas_archivo} factura(s) de la hoja.
        Ignoradas en esta carga: ${flt.facturas_fuera_de_rango} fuera del rango${flt.facturas_sin_fecha ? `, ${flt.facturas_sin_fecha} sin Fecha Fac` : ""}${flt.remesas_fuera_de_rango ? ` y ${flt.remesas_fuera_de_rango} remesa(s) sin facturas en el rango` : ""}.
        ${flt.fecha_fac_min ? `<br><small>Fechas Fac en la hoja: del ${esc(fmtFecha(flt.fecha_fac_min))} al ${esc(fmtFecha(flt.fecha_fac_max))}.</small>` : ""}</div>`;
    const det = r.facturas_detalle;
    const detalleHtml = !det ? "" : `<div class="card stack">
        <div class="row between"><h2 style="margin:0">Facturas en el rango (${flt.facturas_en_rango})</h2>
          <input id="fDet" type="search" placeholder="Buscar factura, cliente, pedido o remesa" style="width:auto;min-width:240px" aria-label="Buscar en las facturas del rango"></div>
        <small class="muted">Solo estas facturas forman parte de esta carga. Revíselas antes de confirmar.${r.facturas_detalle_truncado ? ` Se muestran las primeras ${det.length}.` : ""}</small>
        <div class="table-wrap" style="max-height:420px;overflow:auto"><table id="tDet">
          <thead><tr><th>Remesa</th><th>Factura</th><th>Fecha Fac</th><th>Cliente</th><th>Pedido</th><th>Cod. Vendedor</th><th class="num">Bultos</th><th>Chofer</th><th>Destino</th><th>Situación</th></tr></thead>
          <tbody>${det.map((d) => `<tr data-q="${esc([d.remesa, d.factura, d.cliente, d.pedido, d.cod_vendedor].join(" ").toLowerCase())}">
            <td class="mono">${esc(d.remesa || "-")}</td><td class="mono">${esc(d.factura)}</td><td>${esc(fmtFecha(d.fecha_fac))}</td>
            <td>${esc(d.cliente || "")}</td><td class="mono">${esc(d.pedido || "")}</td><td>${esc(d.cod_vendedor || "")}</td>
            <td class="num">${esc(d.bultos ?? "")}</td><td>${esc(d.chofer || "")}</td><td>${esc(d.destino || "")}</td>
            <td class="${claseCls(d.clase)}">${esc(CLASE[d.clase] || "—")}</td></tr>`).join("")}</tbody>
        </table></div>
        <small class="muted" id="nDet">${det.length} factura(s)</small>
      </div>`;
    $("#res").innerHTML = `
      <div class="card stack">
        <div class="row between"><h2 style="margin:0">Resumen de «${esc(r.archivo)}» · hoja «${esc(r.hoja)}»</h2>
        <small>${r.filas_leidas} filas leídas</small></div>
        ${filtroHtml}
        ${imp.estado === "analisis" ? '<div class="alert">Modo consulta: este es el análisis completo de la hoja, <strong>sin guardar</strong>. Las remesas y facturas no se registran.</div>' : ""}
        <div class="grid">
          ${kpi(flt ? "Remesas en el rango" : "Remesas en el archivo", t.remesas)}${kpi("Remesas nuevas", t.remesas_nuevas)}
          ${kpi("Facturas nuevas", t.facturas_nuevas)}${kpi("Ya cargadas", t.facturas_ya_cargadas)}
          ${kpi("En otra remesa (se omiten)", t.facturas_en_otra_remesa, t.facturas_en_otra_remesa ? "lvl-advertencia" : "")}
          ${kpi("Choferes sin registrar", t.choferes_no_registrados, t.choferes_no_registrados ? "lvl-advertencia" : "")}
        </div>
        ${imp.revision_ia ? `<div class="alert"><strong>Revisión asistida (IA)</strong><div style="white-space:pre-wrap">${esc(imp.revision_ia)}</div></div>` : ""}
        ${Object.keys(r.mapeo_ia || {}).length ? `<div class="alert warn">Columnas asociadas por IA: ${Object.entries(r.mapeo_ia).map(([k, c]) => `«${esc(k)}» → ${esc(c)}`).join(", ")}</div>` : ""}
      </div>
      ${detalleHtml}
      <div class="card">
        <div class="row between"><h2 style="margin:0">Avisos (${avisos.length})</h2>
          <select id="fNivel" style="width:auto"><option value="">Todos</option><option value="error">Errores</option>
          <option value="advertencia">Advertencias</option><option value="info">Información</option></select></div>
        <div class="table-wrap" style="max-height:320px;overflow:auto"><table id="tAvisos"></table></div>
      </div>
      <div class="card table-wrap">
        <h2 style="margin-top:0">Choferes</h2>
        <table><tr><th>Cédula</th><th>Nombre</th><th>Placa</th><th>Remesas</th><th>Facturas nuevas</th><th>Registrado</th></tr>
        ${r.choferes.map((c, i) => `<tr><td class="mono">${esc(c.cedula)}</td><td>${esc(c.nombre)}</td><td>${esc(c.placa || "")}</td>
          <td>${c.remesas}</td><td>${c.facturas_nuevas}</td><td data-reg="${esc(c.cedula)}">${c.registrado ? "Sí"
            : `<span class="lvl-advertencia">No</span>${PUEDE_CREAR_CONDUCTOR ? ` <button type="button" class="small" data-crear-chofer="${i}">Crear conductor</button>` : ""}`}</td></tr>`).join("")}</table>
        ${t.choferes_no_registrados && PUEDE_CREAR_CONDUCTOR ? '<p class="muted" style="margin:6px 0 0">Los choferes sin registrar se pueden crear aquí uno por uno (revisando sus datos) o todos juntos marcando «Registrar automáticamente» abajo. Si no, sus remesas se omiten.</p>' : ""}
      </div>
      <div class="card stack">
        <div class="row between"><h2 style="margin:0">Remesas a cargar</h2>
          <div class="row"><label for="fDesde" style="margin:0">Solo desde</label><input id="fDesde" type="date" style="width:auto">
          <button type="button" class="small" id="selSug">Sugeridas</button><button type="button" class="small" id="selNada">Ninguna</button></div></div>
        <div class="table-wrap" style="max-height:420px;overflow:auto"><table>
          <tr><th></th><th>Remesa</th><th>Fecha</th><th>Chofer</th><th>Destino</th><th>Empresa</th><th>Facturas</th><th>Nuevas</th><th>Estado</th></tr>
          ${r.remesas.map((m) => `<tr>
            <td>${!["excluida", "otra_empresa"].includes(m.estado) ? `<input type="checkbox" data-rem="${esc(m.numero)}" data-fecha="${esc(m.fecha_referencia || "")}" data-sug="${m.seleccion_sugerida ? 1 : 0}" ${m.seleccion_sugerida ? "checked" : ""}>` : ""}</td>
            <td class="mono">${esc(m.numero || "-")}${m.numero_generado ? ' <small class="lvl-advertencia">(generado)</small>' : ""}</td>
            <td>${esc(fmtFecha(m.fecha_referencia))}</td><td>${esc(m.chofer_nombre || "")}${m.chofer_registrado ? "" : ` <small class="lvl-advertencia chofer-pend" data-ced="${esc(m.chofer_cedula || "")}">sin registrar</small>`}</td>
            <td>${esc(m.destino || "")}</td><td>${esc(m.empresa || "")}</td><td>${m.facturas}</td><td>${m.facturas_nuevas}</td>
            <td>${m.estado === "excluida" ? '<span class="lvl-error">excluida</span>'
              : m.estado === "otra_empresa" ? '<span class="lvl-error">de otra empresa</span>' : esc(m.estado)}</td></tr>`).join("")}
        </table></div>
        ${puede("conductores.crear") ? `<div class="row"><input type="checkbox" id="regCh"><label for="regCh" style="margin:0">Registrar automáticamente los choferes nuevos (se generan claves temporales)</label></div>
          ${selectorEmpresa("empCh", "Empresa de transporte de los choferes nuevos")}`
          : '<p class="muted" style="margin:0">Los choferes sin registrar deben darse de alta antes (su rol no crea conductores).</p>'}
        <div id="confErr" class="alert err hidden"></div>
        <div class="row"><button type="button" class="primary" id="btnConf">Confirmar y cargar <span id="nSel"></span></button>
          <button type="button" id="btnDesc">Descartar</button></div>
      </div>`;

    const pintarAvisos = () => {
      const n = $("#fNivel").value;
      const lista = avisos.filter((a) => !n || a.nivel === n);
      $("#tAvisos").innerHTML = `<tr><th>Nivel</th><th>Fila</th><th>Remesa</th><th>Detalle</th></tr>` +
        lista.map((a) => `<tr><td class="lvl-${a.nivel}">${esc(a.nivel)}</td><td>${esc(a.fila ?? "")}</td><td class="mono">${esc(a.remesa || "")}</td><td>${esc(a.mensaje)}</td></tr>`).join("");
    };
    pintarAvisos();
    $("#fNivel").onchange = pintarAvisos;
    if (det) {
      $("#fDet").oninput = () => {
        const q = $("#fDet").value.trim().toLowerCase();
        let n = 0;
        $$("#tDet tbody tr").forEach((tr) => { const ver = !q || tr.dataset.q.includes(q); tr.hidden = !ver; n += ver ? 1 : 0; });
        $("#nDet").textContent = q ? `${n} de ${det.length} factura(s)` : `${det.length} factura(s)`;
      };
    }
    // Crear un chofer del Excel que no está registrado (datos del archivo, editables)
    $("#res").onclick = (e) => {  // propiedad (no addEventListener): el resumen se vuelve a pintar al cambiar de hoja
      const i = e.target.dataset.crearChofer;
      if (i === undefined) return;
      const ch = r.choferes[Number(i)];
      nuevoConductor({ cedula: ch.cedula, nombre: ch.nombre, placa: ch.placa, telefono: ch.telefono, empresa_id: valorSelector("empCh") ?? "" }, (c) => {
        ch.registrado = true;
        const celda = $(`[data-reg="${CSS.escape(ch.cedula)}"]`);
        if (celda) celda.innerHTML = '<span class="ok-txt">Sí · recién creado</span>';
        $$("#res .chofer-pend").forEach((x) => { if (x.dataset.ced === ch.cedula) x.remove(); });
        toast(`Conductor ${c.nombre} creado: sus remesas se cargarán al confirmar`);
      });
    };
    const checks = () => $$("[data-rem]");
    const contar = () => { $("#nSel").textContent = `(${checks().filter((c) => c.checked).length})`; };
    $("#res").addEventListener("change", (e) => { if (e.target.dataset.rem !== undefined) contar(); });
    $("#selSug").onclick = () => { checks().forEach((c) => { c.checked = c.dataset.sug === "1"; }); contar(); };
    $("#selNada").onclick = () => { checks().forEach((c) => { c.checked = false; }); contar(); };
    $("#fDesde").onchange = () => {
      const d = $("#fDesde").value;
      checks().forEach((c) => { c.checked = c.dataset.sug === "1" && (!d || (c.dataset.fecha && c.dataset.fecha >= d)); });
      contar();
    };
    contar();
    $("#btnDesc").onclick = async () => {
      if (imp.estado === "analisis") { $("#res").innerHTML = '<div class="alert">Análisis cerrado (no se había guardado nada).</div>'; return; }
      await api(`/api/importaciones/${imp.id}/descartar`, { method: "POST" });
      $("#res").innerHTML = '<div class="alert">Carga descartada.</div>'; cargarHistorial();
    };
    $("#btnConf").onclick = async () => {
      const sel = checks().filter((c) => c.checked).map((c) => c.dataset.rem);
      if (!sel.length) { $("#confErr").textContent = "Seleccione al menos una remesa"; $("#confErr").classList.remove("hidden"); return; }
      $("#btnConf").disabled = true;
      try {
        const out = await api(`/api/importaciones/${imp.id}/confirmar`, { method: "POST", body: { remesas: sel, registrar_choferes: !!($("#regCh") && $("#regCh").checked), empresa_id: valorSelector("empCh") } });
        $("#res").innerHTML = `<div class="alert ok">Carga completada: ${out.remesas_creadas} remesas nuevas, ${out.facturas_creadas} facturas en estatus Preparado
          (${out.facturas_omitidas} omitidas).${out.remesas_excluidas.length ? ` Remesas excluidas por chofer sin registrar: ${esc(out.remesas_excluidas.join(", "))}.` : ""}</div>`;
        mostrarCredenciales(out.choferes_registrados, "Choferes registrados");
        cargarHistorial();
      } catch (ex) {
        $("#confErr").textContent = ex.message; $("#confErr").classList.remove("hidden");
      } finally { const b = $("#btnConf"); if (b) b.disabled = false; }
    };
  }

  // ------------------------------------------------------------------ Remesas
  async function vistaRemesas(v) {
    v.innerHTML = `<h1>Remesas</h1>
      <form id="fr" class="card row">
        <input name="q" placeholder="Número, chofer, destino o placa" class="grow" style="flex:1 1 200px">
        <select name="estado" style="width:auto"><option value="activas">Activas</option><option value="completas">Completas</option><option value="todas">Todas</option></select>
        <input type="date" name="desde" style="width:auto" aria-label="Desde"><input type="date" name="hasta" style="width:auto" aria-label="Hasta">
        <button class="primary" type="submit">Filtrar</button></form>
      <div id="tr" class="card table-wrap" style="margin-top:12px"></div>`;
    const cargar = async (offset = 0) => {
      const p = new URLSearchParams([...new FormData($("#fr"))].filter(([, x]) => x));
      p.set("limit", 50); p.set("offset", offset);
      const r = await api("/api/remesas?" + p);
      $("#tr").innerHTML = r.items.length ? `<table><tr><th>Remesa</th><th>Fecha carga</th><th>Empresa de transporte</th><th>Chofer</th><th class="hide-sm">Placa</th><th class="hide-sm">Destino</th><th>Facturas</th><th style="min-width:120px">Avance</th><th></th></tr>
        ${r.items.map((m) => `<tr><td class="mono">${esc(m.numero)}</td><td>${esc(fmtFechaHora(m.created_at))}</td><td>${esc(m.empresa_transporte || "—")}</td><td>${esc(m.chofer_nombre || "") || '<small class="lvl-advertencia">sin conductor</small>'}</td>
        <td class="hide-sm">${esc(m.placa || "")}</td><td class="hide-sm">${esc(m.destino || "")}</td><td>${m.total_facturas}</td>
        <td><div class="progress"><span style="width:${Math.round(m.progreso * 100)}%"></span></div><small>${Math.round(m.progreso * 100)} %</small></td>
        <td><button type="button" class="small" data-ver="${m.id}">Ver</button></td></tr>`).join("")}</table>
        ${paginador(r.total, offset)}` : '<span class="muted">Sin resultados.</span>';
    };
    $("#fr").addEventListener("submit", (e) => { e.preventDefault(); cargar(); });
    $("#tr").addEventListener("click", (e) => {
      if (e.target.dataset.ver) verRemesa(Number(e.target.dataset.ver), () => cargar());
      if (e.target.dataset.pag) cargar(Number(e.target.dataset.pag));
    });
    await cargar();
  }

  function paginador(total, offset, size = 50) {
    if (total <= size) return `<small class="muted">${total} resultado(s)</small>`;
    return `<div class="row" style="margin-top:8px"><small class="muted">${offset + 1}–${Math.min(offset + size, total)} de ${total}</small>
      ${offset > 0 ? `<button type="button" class="small" data-pag="${offset - size}">Anterior</button>` : ""}
      ${offset + size < total ? `<button type="button" class="small" data-pag="${offset + size}">Siguiente</button>` : ""}</div>`;
  }

  // Cambiar la EMPRESA DE TRANSPORTE (solo Supervisor): se aplica a la remesa y, en cascada, a TODAS sus facturas.
  // El conductor actual es de la empresa anterior: se elige uno de la nueva o la remesa queda sin conductor.
  const CAMBIA_EMPRESA = puede("remesas.empresa_transporte.cambiar");
  async function cambiarEmpresaTransporte(o, alCambiar) {
    // o: { remesaId, facturaId?, numero, empresaId, empresa, facturas }
    const [empresas, conductores] = await Promise.all([cargarEmpresas(), api("/api/admin/conductores")]);
    abrirDialogo(`<h2 style="margin-top:0">Cambiar empresa de transporte</h2>
      <p style="margin:0">Remesa <strong class="mono">${esc(o.numero)}</strong> · empresa actual: <strong>${esc(o.empresa || "Sin empresa")}</strong></p>
      <div class="alert warn">El cambio se aplica a la remesa y, en cascada, a <strong>sus ${o.facturas} factura(s)</strong>${o.facturaId ? " (no solo a esta factura)" : ""}. Queda registrado en Auditoría.</div>
      <div><label for="ceEmp">Nueva empresa de transporte</label><select id="ceEmp" name="empresa_id" required>
        <option value="">— Elija la empresa —</option>
        ${empresas.map((e) => `<option value="${e.id}" ${e.id === o.empresaId ? "disabled" : ""}>${esc(e.nombre)}${e.id === o.empresaId ? " (actual)" : ""}</option>`).join("")}</select></div>
      <div><label for="ceCond">Conductor de la nueva empresa</label><select id="ceCond" name="conductor_id"></select>
        <small class="muted">Opcional. Si no elige, la remesa queda sin conductor y la nueva empresa la asigna con «Reasignar».</small></div>
      <div id="dlgErr" class="alert err hidden"></div>`,
      `<button type="button" data-cerrar>Cancelar</button><button type="submit" class="primary" id="ceOk">Aplicar a ${o.facturas} factura(s)</button>`,
      async (fd) => {
        const empresa = Number(fd.get("empresa_id"));
        if (!empresa) throw new Error("Elija la nueva empresa de transporte.");
        const cond = fd.get("conductor_id") ? Number(fd.get("conductor_id")) : null;
        const url = o.facturaId ? `/api/facturas/${o.facturaId}/empresa-transporte` : `/api/remesas/${o.remesaId}/empresa-transporte`;
        const r = await api(url, { method: "PUT", body: { empresa_id: empresa, conductor_id: cond } });
        toast(`Remesa ${o.numero}: ${r.facturas_actualizadas} factura(s) pasaron a ${r.empresa_nueva}${r.conductor_nombre ? " · conductor " + r.conductor_nombre : " · sin conductor"}`);
        alCambiar && alCambiar();
      });
    const pintarConductores = () => {
      const emp = Number($("#ceEmp").value);
      const lista = conductores.filter((c) => emp && c.empresa_id === emp);
      $("#ceCond").innerHTML = `<option value="">— Sin conductor (se asigna después) —</option>`
        + lista.map((c) => `<option value="${c.id}">${esc(c.nombre)}${c.placa ? " · " + esc(c.placa) : ""}</option>`).join("");
      $("#ceCond").disabled = !emp;
    };
    $("#ceEmp").onchange = pintarConductores;
    pintarConductores();
  }

  async function verRemesa(id, alCambiar) {
    const r = await api("/api/remesas/" + id);
    const REASIGNAR = puede("remesas.reasignar"), ELIMINAR = puede("remesas.eliminar");
    // Total factura / neto: solo Supervisor y Cobranzas (la API tampoco envía los montos a los demás)
    const MONTOS = puede("facturas.montos.ver") && r.facturas.every((f) => f.montos_visibles !== false);
    const conductores = REASIGNAR ? await api("/api/admin/conductores") : [];
    abrirDialogo(`
      <h2 style="margin-top:0">Remesa ${esc(r.numero)}</h2>
      <div class="grid">
        <div><small>Chofer</small><div>${esc(r.chofer_nombre || "")}</div>${r.chofer_cedula ? `<small>C.I. ${esc(r.chofer_cedula)} · ${esc(r.chofer_telefono || "")}</small>` : ""}</div>
        <div><small>Placa</small><div>${esc(r.placa || "")}</div></div>
        <div><small>Destino</small><div>${esc(r.destino || "")}</div></div>
        <div><small>Empresa emisora</small><div>${esc(r.empresa || "")}</div></div>
        <div><small>Empresa de transporte</small><div><strong>${esc(r.empresa_transporte || "Sin empresa")}</strong>
          ${CAMBIA_EMPRESA ? '<br><button type="button" class="small" id="btnEmpRem">Cambiar empresa de transporte</button>' : ""}</div></div>
      </div>
      <div class="table-wrap"><table><tr><th>Factura</th><th>Cliente</th><th class="hide-sm">Vendedor</th>${MONTOS ? '<th class="hide-sm">Total</th>' : ""}<th>Estatus</th><th>Fecha</th></tr>
      ${r.facturas.map((f) => `<tr><td class="mono"><a href="#" data-fact="${f.id}">${esc(f.numero)}</a></td><td>${esc(f.cliente || "")}</td><td class="hide-sm">${esc(f.cod_vendedor || "")}</td>
        ${MONTOS ? `<td class="hide-sm">${fmtMonto(f.total_fact)}</td>` : ""}<td>${badge(f.estatus)}</td><td><small>${esc(fmtFechaHora(f.estatus_at))}</small></td></tr>`).join("")}</table></div>
      ${REASIGNAR ? `<div class="row"><label for="reas" style="margin:0">Reasignar a</label><select id="reas" style="width:auto">
        ${conductores.map((c) => `<option value="${c.id}" ${c.id === r.conductor_id ? "selected" : ""}>${esc(c.nombre)}</option>`).join("")}</select>
        <button type="button" class="small" id="btnReas">Reasignar</button></div>` : ""}
      <div id="dlgErr" class="alert err hidden"></div>`,
      `${ELIMINAR ? '<button type="button" class="danger" id="btnDelRem">Eliminar remesa</button>' : ""}
       <button type="button" id="btnXls">Excel Cobranzas</button><button type="submit" class="primary">Cerrar</button>`, null);
    $("#btnXls").onclick = () => TL.download(`/api/reportes/cobranzas.xlsx?remesa_ids=${r.id}`, `Remesa_${r.numero}.xlsx`).catch((e) => toast(e.message));
    $("#dlgBody").addEventListener("click", (e) => {
      const fid = e.target.dataset.fact;
      if (fid) { e.preventDefault(); dlg.close(); verFactura(Number(fid), alCambiar); }
    });
    if (CAMBIA_EMPRESA) {
      $("#btnEmpRem").onclick = () => cambiarEmpresaTransporte({ remesaId: r.id, numero: r.numero, empresaId: r.empresa_id,
        empresa: r.empresa_transporte, facturas: r.facturas.length }, () => { alCambiar && alCambiar(); verRemesa(r.id, alCambiar); });
    }
    if (REASIGNAR) {
      $("#btnReas").onclick = async () => {
        await api(`/api/remesas/${r.id}/reasignar`, { method: "POST", body: { conductor_id: Number($("#reas").value) } });
        toast("Remesa reasignada"); dlg.close(); alCambiar && alCambiar();
      };
    }
    if (ELIMINAR) {
      $("#btnDelRem").onclick = async () => {
        if (!confirm(`¿Eliminar la remesa ${r.numero} con sus ${r.facturas.length} facturas e historial? No se puede deshacer.`)) return;
        await api(`/api/remesas/${r.id}`, { method: "DELETE" });
        toast("Remesa eliminada"); dlg.close(); alCambiar && alCambiar();
      };
    }
  }

  // ------------------------------------------------------------------ Facturas
  async function vistaFacturas(v) {
    v.innerHTML = `<h1>Facturas</h1>
      <form id="ff" class="card row">
        <input name="q" placeholder="Factura, pedido, tracking o cliente" class="grow" style="flex:1 1 220px">
        <select name="estatus" style="width:auto"><option value="">Todos los estatus</option>${Object.entries(ESTATUS).map(([k, t]) => `<option value="${k}">${t}</option>`).join("")}</select>
        <input name="cod_vendedor" placeholder="Cod. vendedor" style="width:140px">
        <input type="date" name="desde" style="width:auto" aria-label="Desde"><input type="date" name="hasta" style="width:auto" aria-label="Hasta">
        <button class="primary" type="submit">Buscar</button></form>
      <div id="tf" class="card table-wrap" style="margin-top:12px"></div>`;
    const cargar = async (offset = 0) => {
      const p = new URLSearchParams([...new FormData($("#ff"))].filter(([, x]) => x));
      p.set("limit", 50); p.set("offset", offset);
      const r = await api("/api/facturas?" + p);
      $("#tf").innerHTML = r.items.length ? `<table><tr><th>Factura</th><th>Pedido</th><th>Cliente</th><th class="hide-sm">Remesa</th><th>Empresa de transporte</th><th class="hide-sm">Chofer</th><th>Estatus</th><th>Fecha</th></tr>
        ${r.items.map((f) => `<tr><td class="mono"><a href="#" data-fact="${f.id}">${esc(f.numero)}</a></td><td class="mono">${esc(f.pedido || "")}</td><td>${esc(f.cliente || "")}</td>
        <td class="hide-sm mono">${esc(f.remesa_numero || "")}</td><td>${esc(f.empresa_transporte || "—")}</td><td class="hide-sm">${esc(f.conductor_nombre || "")}</td><td>${badge(f.estatus)}</td><td><small>${esc(fmtFechaHora(f.estatus_at))}</small></td></tr>`).join("")}</table>
        ${paginador(r.total, offset)}` : '<span class="muted">Sin resultados.</span>';
    };
    $("#ff").addEventListener("submit", (e) => { e.preventDefault(); cargar(); });
    $("#tf").addEventListener("click", (e) => {
      if (e.target.dataset.fact) { e.preventDefault(); verFactura(Number(e.target.dataset.fact), () => cargar()); }
      if (e.target.dataset.pag) cargar(Number(e.target.dataset.pag));
    });
    await cargar();
  }

  async function verFactura(id, alCambiar) {
    const f = await api("/api/facturas/" + id);
    const ORIG = { importacion: "Carga REMESA", online: "App conductor", offline: "App conductor (sin señal)", admin: "Corrección manual", captura: "Foto de factura" };
    abrirDialogo(`
      <div class="row between"><h2 style="margin:0" class="mono">${esc(f.numero)}</h2>${badge(f.estatus)}</div>
      <div class="grid">
        <div><small>Cliente</small><div>${esc(f.cliente || "")}</div></div>
        <div><small>Pedido</small><div class="mono">${esc(f.pedido || "")}</div></div>
        <div><small>Tracking</small><div class="mono">${esc(f.tracking || "—")}</div></div>
        <div><small>Remesa</small><div class="mono">${esc(f.remesa_numero || "")}</div></div>
        <div><small>Empresa de transporte</small><div><strong>${esc(f.empresa_transporte || "Sin empresa")}</strong>
          ${CAMBIA_EMPRESA ? '<br><button type="button" class="small" id="btnEmpFac">Cambiar empresa de transporte</button>' : ""}</div></div>
        <div><small>Vendedor</small><div>${esc(f.cod_vendedor || "")}</div></div>
        ${f.montos_visibles !== false ? `<div><small>Total factura / neto</small><div>${fmtMonto(f.total_fact)} / ${fmtMonto(f.total_neto)}</div></div>` : ""}
        <div><small>Fecha factura</small><div>${esc(fmtFecha(f.fecha_fac)) || "—"}</div></div>
        <div><small>Fecha Preparado</small><div>${esc(fmtMomento(f.fecha_alm)) || "—"}</div></div>
        <div><small>Por Entregar</small><div>${esc(fmtMomento(f.fecha_tra)) || "—"}</div></div>
        <div><small>Fecha Entregado (Fecha Rec)</small><div>${esc(fmtMomento(f.fecha_rec)) || (f.estatus === "devolucion" ? `No entregada devolución - ${fmtFechaHora(f.estatus_at).replace(",", "")}` : "Por Entregar")}</div></div>
        <div><small>Bultos</small><div>${esc(f.bultos ?? "")}</div></div>
      </div>
      ${f.comentario ? `<div><small>Comentario</small><div>${esc(f.comentario)}</div></div>` : ""}
      ${f.observacion_entrega ? `<div class="alert ok"><small>Observación de la entrega</small><div>${esc(f.observacion_entrega)}</div></div>` : ""}
      ${f.habilitada_reingreso ? '<div class="alert warn"><strong>Devuelta · habilitada para reingreso:</strong> puede cargarse de nuevo en una remesa nueva (Excel o foto). Este historial se conserva.</div>' : ""}
      ${f.reingreso_de ? `<div class="alert"><small>Reingreso tras devolución</small><div>Ciclo anterior: <a href="#" data-fact="${f.reingreso_de.id}">remesa ${esc(f.reingreso_de.remesa_numero || "—")}</a> · ${esc(ESTATUS[f.reingreso_de.estatus] || f.reingreso_de.estatus)} el ${esc(fmtFechaHora(f.reingreso_de.estatus_at))}</div></div>` : ""}
      ${f.reingresos.length ? `<div class="alert"><small>Se volvió a cargar después de la devolución</small>${f.reingresos.map((x) => `<div><a href="#" data-fact="${x.id}">Remesa ${esc(x.remesa_numero || "—")}</a> · ${esc(ESTATUS[x.estatus] || x.estatus)}</div>`).join("")}</div>` : ""}
      ${f.captura_id ? `<div><small>Factura original (foto)</small><div><img class="thumb" data-captura="${esc(f.captura_id)}" alt="Foto de la factura original"></div></div>` : ""}
      <h3>Historial</h3>
      <div class="hist-tiempos">
      <ul class="timeline">${f.eventos.map((e) => `<li class="${e.aplicado ? "" : "muted"}">
        <strong>${esc(ESTATUS[e.estatus])}</strong> · ${esc(fmtMomento(e.ocurrido_at))}
        <small> · ${esc(ORIG[e.origen] || e.origen)}${e.usuario_nombre ? " · " + esc(e.usuario_nombre) : ""}${e.aplicado ? "" : " · llegó tarde, solo historial"}</small>
        ${e.motivo ? `<div><small>${e.estatus === "entregada" && e.origen !== "admin" ? "Observación" : "Motivo"}: ${esc(e.motivo)}</small></div>` : ""}
        ${e.foto_id ? `<div><img class="thumb" data-foto="${esc(e.foto_id)}" alt="Foto de la factura"></div>` : ""}</li>`).join("")}</ul>
      ${TL.cuadroTiempos(f.tiempos)}
      </div>
      ${puede("facturas.estatus.corregir") ? `<h3>Corregir estatus</h3><div class="row">
        <select id="nuevoEst" style="width:auto">${Object.entries(ESTATUS).map(([k, t]) => `<option value="${k}" ${k === f.estatus ? "selected" : ""}>${t}</option>`).join("")}</select>
        <input id="motivoAdm" placeholder="Motivo (obligatorio si retrocede)" class="grow" style="flex:1 1 200px">
        <button type="button" class="small" id="btnEst">Aplicar</button></div>` : ""}
      <div id="dlgErr" class="alert err hidden"></div>`, `<button type="submit" class="primary">Cerrar</button>`, null);
    if (CAMBIA_EMPRESA) {
      $("#btnEmpFac").onclick = async () => {
        const rem = await api("/api/remesas/" + f.remesa_id);
        cambiarEmpresaTransporte({ remesaId: rem.id, facturaId: f.id, numero: rem.numero, empresaId: rem.empresa_id,
          empresa: rem.empresa_transporte, facturas: rem.facturas.length }, () => { alCambiar && alCambiar(); verFactura(f.id, alCambiar); });
      };
    }
    // Enlaces a los otros ciclos (reingresos) de la misma factura
    for (const a of $$("a[data-fact]", $("#dlgBody"))) {
      a.onclick = (ev) => { ev.preventDefault(); dlg.close(); verFactura(Number(a.dataset.fact), alCambiar); };
    }
    // Las fotos requieren el token: se descargan con fetch y se muestran como blob
    for (const img of $$("img[data-foto], img[data-captura]", $("#dlgBody"))) {
      const url = img.dataset.foto ? "/api/fotos/" + img.dataset.foto : `/api/capturas/${img.dataset.captura}/imagen`;
      api(url, { raw: true }).then((r) => r.blob())
        .then((b) => { img.src = URL.createObjectURL(b); }).catch(() => { img.alt = "Foto no disponible"; });
    }
    if (puede("facturas.estatus.corregir")) {
      $("#btnEst").onclick = async () => {
        try {
          await api(`/api/facturas/${f.id}/estatus`, { method: "POST", body: { estatus: $("#nuevoEst").value, motivo: $("#motivoAdm").value || null } });
          toast("Estatus actualizado"); dlg.close(); alCambiar && alCambiar();
        } catch (e) { $("#dlgErr").textContent = e.message; $("#dlgErr").classList.remove("hidden"); }
      };
    }
  }

  // ------------------------------------------------------------------ Feriados
  /** Días feriados: no cuentan como hábiles en los tiempos entre procesos. Solo el Supervisor los cambia. */
  async function vistaFeriados(v) {
    const GESTIONA = puede("feriados.gestionar");
    const hoy = new Date().getFullYear();
    const DIAS = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];
    v.innerHTML = `<h1>Feriados</h1>
      <p class="muted">Los días feriados <strong>no cuentan como días hábiles</strong> en los días transcurridos entre procesos
        (ficha de la factura, consulta pública e Indicador de Gestión). Los que caen sábado o domingo ya no eran hábiles.</p>
      <div class="card row"><label for="fAnio" style="margin:0">Año</label>
        <select id="fAnio" style="width:auto">${[hoy - 2, hoy - 1, hoy, hoy + 1, hoy + 2].map((a) => `<option ${a === hoy ? "selected" : ""}>${a}</option>`).join("")}</select>
        ${GESTIONA ? '<button type="button" id="fNac">Cargar feriados nacionales del año</button>' : ""}</div>
      ${GESTIONA ? `<form id="fNuevo" class="card row" style="margin-top:12px">
        <input type="date" name="fecha" required style="width:auto" aria-label="Fecha">
        <input name="nombre" required minlength="2" maxlength="120" placeholder="Motivo (p. ej. Decreto presidencial)" class="grow" style="flex:1 1 240px" aria-label="Motivo">
        <button class="primary" type="submit">Agregar feriado</button></form>` : ""}
      <div id="fLista" class="card table-wrap" style="margin-top:12px"></div>`;
    async function cargar() {
      const anio = $("#fAnio").value;
      const lista = await api(`/api/feriados?anio=${anio}`);
      $("#fLista").innerHTML = lista.length ? `<table><tr><th>Fecha</th><th>Día</th><th>Motivo</th><th>Origen</th>${GESTIONA ? "<th></th>" : ""}</tr>
        ${lista.map((f) => {
          const d = new Date(f.fecha + "T12:00:00"), finde = [0, 6].includes(d.getDay());
          return `<tr class="${finde ? "muted" : ""}"><td class="mono">${esc(fmtFecha(f.fecha))}</td><td>${DIAS[d.getDay()]}${finde ? " <small>(fin de semana)</small>" : ""}</td>
            <td>${esc(f.nombre)}</td><td>${f.nacional ? "Nacional" : "Agregado"}</td>
            ${GESTIONA ? `<td><button type="button" class="small danger" data-quitar="${f.id}" data-txt="${esc(fmtFecha(f.fecha))} · ${esc(f.nombre)}">Quitar</button></td>` : ""}</tr>`;
        }).join("")}</table><small class="muted">${lista.length} feriado(s) en ${esc(anio)}</small>`
        : `<span class="muted">No hay feriados cargados en ${esc(anio)}.${GESTIONA ? " Use «Cargar feriados nacionales del año»." : ""}</span>`;
    }
    $("#fAnio").onchange = cargar;
    if (GESTIONA) {
      $("#fNac").onclick = async () => {
        try {
          const r = await api("/api/feriados/nacionales", { method: "POST", body: { anio: Number($("#fAnio").value) } });
          toast(r.agregados ? `${r.agregados} feriado(s) nacional(es) agregado(s)` : "Los feriados nacionales de ese año ya estaban cargados");
          cargar();
        } catch (e) { toast(e.message); }
      };
      $("#fNuevo").onsubmit = async (e) => {
        e.preventDefault();
        const fd = new FormData(e.target);
        try {
          await api("/api/feriados", { method: "POST", body: { fecha: fd.get("fecha"), nombre: fd.get("nombre") } });
          toast("Feriado agregado"); e.target.reset();
          $("#fAnio").value = String(fd.get("fecha")).slice(0, 4);
          if (!$("#fAnio").value) $("#fAnio").innerHTML += `<option selected>${String(fd.get("fecha")).slice(0, 4)}</option>`;
          cargar();
        } catch (err) { toast(err.message); }
      };
      $("#fLista").addEventListener("click", async (e) => {
        const id = e.target.dataset.quitar;
        if (!id || !confirm(`¿Quitar el feriado ${e.target.dataset.txt}? Ese día volverá a contar como hábil.`)) return;
        try { await api(`/api/feriados/${id}`, { method: "DELETE" }); toast("Feriado quitado"); cargar(); } catch (err) { toast(err.message); }
      });
    }
    await cargar();
  }

  // ------------------------------------------------------------------ Catálogos (CRUD genérico)
  async function vistaCrud(v, cfg) {
    v.innerHTML = `<div class="row between"><h1 style="margin:0">${esc(cfg.titulo)}</h1>
      <div class="row">${cfg.extraBotones || ""}${(cfg.puedeCrear ?? PUEDE_EDITAR) ? `<button type="button" class="primary" id="btnNuevo">Nuevo</button>` : ""}</div></div>
      <input id="filtro" type="search" placeholder="Filtrar…" style="margin:12px 0">
      <div id="tbl" class="card table-wrap"></div>`;
    let items = [];
    const pintar = () => {
      const q = $("#filtro").value.toLowerCase();
      const ver = items.filter((i) => !q || JSON.stringify(i).toLowerCase().includes(q));
      $("#tbl").innerHTML = ver.length ? `<table><tr>${cfg.cols.map((c) => `<th>${esc(c[1])}</th>`).join("")}<th></th></tr>
        ${ver.map((i) => `<tr>${cfg.cols.map(([k]) => `<td>${cfg.fmt && cfg.fmt[k] ? cfg.fmt[k](i[k], i) : esc(i[k] ?? "")}</td>`).join("")}
        <td class="row">${(cfg.acciones || []).map((a) => `<button type="button" class="small ${a.cls || ""}" data-acc="${a.id}" data-key="${esc(i[cfg.key])}">${esc(a.t)}</button>`).join("")}</td></tr>`).join("")}</table>
        <small class="muted">${ver.length} registro(s)</small>` : '<span class="muted">Sin registros.</span>';
    };
    const recargar = async () => { items = await api(cfg.url); pintar(); };
    $("#filtro").addEventListener("input", pintar);
    $("#tbl").addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-acc]");
      if (!b) return;
      const item = items.find((i) => String(i[cfg.key]) === b.dataset.key);
      const acc = cfg.acciones.find((a) => a.id === b.dataset.acc);
      try { await acc.fn(item, recargar); } catch (ex) { toast(ex.message); }
    });
    const nuevo = $("#btnNuevo");
    if (nuevo) nuevo.onclick = () => cfg.nuevo(recargar);
    if (cfg.alMontar) cfg.alMontar(recargar);
    await recargar();
  }

  function subirCatalogo(url, titulo, extra) {
    return (recargar) => {
      abrirDialogo(`<h2 style="margin-top:0">${esc(titulo)}</h2><div><label for="catF">Archivo Excel</label><input id="catF" name="archivo" type="file" accept=".xlsx,.xlsm" required></div>
        ${extra || ""}<div id="dlgErr" class="alert err hidden"></div>`,
      `<button type="button" data-cerrar>Cancelar</button><button type="submit" class="primary">Cargar</button>`, async (fd) => {
        const r = await api(url, { method: "POST", body: fd });
        toast(`${r.creados} creados, ${r.actualizados} actualizados`);
        await recargar();
        if (r.credenciales && r.credenciales.length) { setTimeout(() => mostrarCredenciales(r.credenciales, "Conductores creados"), 50); }
      });
    };
  }

  function vistaConductores(v) {
    const campos = (c = {}) => [
      { name: "cedula", label: "Cédula", value: c.cedula, required: true, readonly: !!c.id },
      { name: "nombre", label: "Nombre", value: c.nombre, required: true },
      { name: "telefono", label: "Teléfono", value: c.telefono },
      { name: "placa", label: "Placa habitual", value: c.placa },
      ...(c.id ? [{ name: "activo", label: "Activo", type: "checkbox", value: c.activo }] :
        [{ name: "clave", label: "Clave inicial (vacío = se genera una temporal)", type: "password", min: 6 }, ...campoEmpresa("")]),
    ];
    const cargarPlacas = (recargar) => {
      abrirDialogo(`<h2 style="margin-top:0">Cargar choferes y camiones (hoja PLACAS)</h2>
        <div><label for="catF">Archivo Excel (por ejemplo REMESA.xlsx)</label><input id="catF" type="file" accept=".xlsx,.xlsm" required></div>
        <div><label for="hojaP">Hoja</label><input id="hojaP" value="PLACAS" required></div>
        ${selectorEmpresa("empP", "Empresa de transporte de los choferes nuevos")}<div id="dlgErr" class="alert err hidden"></div>`,
      `<button type="button" data-cerrar>Cancelar</button><button type="submit" class="primary">Cargar</button>`, async () => {
        const libro = await XlsxLite.open($("#catF").files[0]);
        const buscada = XlsxLite.norm($("#hojaP").value);
        const hoja = libro.sheetNames.find((n) => XlsxLite.norm(n) === buscada);
        if (!hoja) throw new Error(`El libro no tiene la hoja «${$("#hojaP").value}». Hojas: ${libro.sheetNames.join(", ")}`);
        const filas = await libro.readSheet(hoja, { maxCols: 12 });
        const r = await api("/api/admin/catalogos/placas/filas", { method: "POST", body: await XlsxLite.gzipJson({ hoja, filas, empresa_id: valorSelector("empP") }) });
        toast(`${r.creados} creados, ${r.actualizados} actualizados`);
        await recargar();
        if (r.credenciales && r.credenciales.length) setTimeout(() => mostrarCredenciales(r.credenciales, "Conductores creados"), 50);
      });
    };
    return vistaCrud(v, {
      titulo: "Conductores", url: "/api/admin/conductores?incluir_inactivos=true", key: "id",
      cols: [["cedula", "Cédula"], ["nombre", "Nombre"], ["telefono", "Teléfono"], ["placa", "Placa"],
        ...(puede("empresas.ver") ? [["empresa_id", "Empresa"]] : []), ["activo", "Activo"]],
      fmt: { activo: (x) => (x ? "Sí" : '<span class="muted">No</span>'), empresa_id: (x) => esc(nombreEmpresa(x)) || '<span class="muted">—</span>' },
      puedeCrear: puede("conductores.crear"),
      extraBotones: puede("conductores.crear") && PUEDE_EDITAR ? '<button type="button" id="btnPlacas">Cargar desde Excel</button>' : "",
      alMontar: (recargar) => { const b = $("#btnPlacas"); if (b) b.onclick = () => cargarPlacas(recargar); },
      nuevo: (recargar) => formulario("Nuevo conductor", campos(), async (fd) => {
        const r = await api("/api/admin/conductores", { method: "POST", body: conEmpresa(datos(fd, campos())) });
        await recargar();
        if (r.credencial) setTimeout(() => mostrarCredenciales([r.credencial]), 50);
      }),
      acciones: [
        ...(PUEDE_EDITAR ? [{ id: "ed", t: "Editar", fn: (c, recargar) => formulario("Editar conductor", campos(c), async (fd) => {
          const d = datos(fd, campos(c)); delete d.cedula;
          await api(`/api/admin/conductores/${c.id}`, { method: "PATCH", body: d }); await recargar();
        }) }] : []),
        ...(puede("conductores.claves.gestionar") ? [{ id: "cl", t: "Clave", fn: async (c) => {
          dialogoClave(c.nombre, `/api/admin/conductores/${c.id}/clave`);
        } }] : []),
        ...(PUEDE_EDITAR ? [{ id: "del", t: "Eliminar", cls: "danger", fn: async (c, recargar) => {
          if (!confirm(`¿Eliminar a ${c.nombre}? Si tiene remesas se desactiva para conservar el historial.`)) return;
          const r = await api(`/api/admin/conductores/${c.id}`, { method: "DELETE" });
          toast(r.eliminado ? "Conductor eliminado" : "Conductor desactivado (tiene historial)"); await recargar();
        } }] : []),
      ],
    });
  }

  function vistaCamiones(v) {
    const campos = (c = {}) => [
      { name: "placa", label: "Placa", value: c.placa, required: true, readonly: !!c.id },
      { name: "clase", label: "Clase", value: c.clase }, { name: "color", label: "Color", value: c.color },
      { name: "serial", label: "Serial N.I.V.", value: c.serial },
      ...(c.id ? [{ name: "activo", label: "Activo", type: "checkbox", value: c.activo }] : []),
    ];
    return vistaCrud(v, {
      titulo: "Camiones", url: "/api/admin/camiones", key: "id",
      cols: [["placa", "Placa"], ["clase", "Clase"], ["color", "Color"], ["serial", "Serial"], ["activo", "Activo"]],
      fmt: { activo: (x) => (x ? "Sí" : '<span class="muted">No</span>') },
      nuevo: (recargar) => formulario("Nuevo camión", campos(), async (fd) => {
        await api("/api/admin/camiones", { method: "POST", body: datos(fd, campos()) }); await recargar();
      }),
      acciones: PUEDE_EDITAR ? [
        { id: "ed", t: "Editar", fn: (c, recargar) => formulario("Editar camión", campos(c), async (fd) => {
          const d = datos(fd, campos(c)); delete d.placa;
          await api(`/api/admin/camiones/${c.id}`, { method: "PATCH", body: d }); await recargar();
        }) },
        { id: "del", t: "Eliminar", cls: "danger", fn: async (c, recargar) => {
          if (!confirm(`¿Eliminar el camión ${c.placa}?`)) return;
          await api(`/api/admin/camiones/${c.id}`, { method: "DELETE" }); await recargar();
        } },
      ] : [],
    });
  }

  function vistaVendedores(v) {
    const campos = (c = {}) => [
      { name: "codigo", label: "Código", value: c.codigo, required: true, readonly: !!c.codigo },
      { name: "nombre", label: "Nombre", value: c.nombre, required: true },
      { name: "territorio", label: "Territorio", value: c.territorio },
      { name: "email", label: "Correo (para avisos de estatus)", type: "email", value: c.email },
      ...(c.codigo ? [{ name: "activo", label: "Activo", type: "checkbox", value: c.activo }] : []),
    ];
    const cargar = subirCatalogo("/api/admin/catalogos/vendedores", "Cargar Listado de Código de Vendedores");
    return vistaCrud(v, {
      titulo: "Vendedores", url: "/api/admin/vendedores", key: "codigo",
      cols: [["codigo", "Código"], ["nombre", "Nombre"], ["territorio", "Territorio"], ["email", "Correo"], ["activo", "Activo"]],
      fmt: { activo: (x) => (x ? "Sí" : '<span class="muted">No</span>') },
      extraBotones: PUEDE_EDITAR ? '<button type="button" id="btnVend">Cargar desde Excel</button>' : "",
      alMontar: (recargar) => { const b = $("#btnVend"); if (b) b.onclick = () => cargar(recargar); },
      nuevo: (recargar) => formulario("Nuevo vendedor", campos(), async (fd) => {
        await api("/api/admin/vendedores", { method: "POST", body: datos(fd, campos()) }); await recargar();
      }),
      acciones: PUEDE_EDITAR ? [
        { id: "ed", t: "Editar", fn: (c, recargar) => formulario("Editar vendedor", campos(c), async (fd) => {
          const d = datos(fd, campos(c)); delete d.codigo;
          await api(`/api/admin/vendedores/${encodeURIComponent(c.codigo)}`, { method: "PATCH", body: d }); await recargar();
        }) },
        { id: "del", t: "Eliminar", cls: "danger", fn: async (c, recargar) => {
          if (!confirm(`¿Eliminar al vendedor ${c.codigo}?`)) return;
          await api(`/api/admin/vendedores/${encodeURIComponent(c.codigo)}`, { method: "DELETE" }); await recargar();
        } },
      ] : [],
    });
  }

  function vistaUsuarios(v) {
    const GESTIONA = puede("usuarios.gestionar");
    // Activo Sí/No: Supervisor a cualquiera (menos a sí mismo); Transportista solo a conductores de su empresa
    const PUEDE_ACTIVAR = puede("usuarios.activar");
    const activable = (u) => PUEDE_ACTIVAR && u.id !== me.id && (GESTIONA || u.rol === "conductor");
    const selActivo = (u) => `<select data-activo="${u.id}" class="sel-activo ${u.activo ? "" : "no"}" aria-label="Activo de ${esc(u.username)}">
      <option value="true" ${u.activo ? "selected" : ""}>Sí</option><option value="false" ${u.activo ? "" : "selected"}>No</option></select>`;
    const roles = [["transportista", "Transportista"], ["cobranzas", "Cobranzas"], ["consultor", "Consultor (solo consulta)"], ["supervisor", "Supervisor"]];
    if (puede("conductores.crear")) roles.push(["conductor", "Conductor"]);
    const campos = (u = {}) => u.id ? [
      { name: "nombre", label: "Nombre", value: u.nombre, required: true },
      ...(u.rol !== "conductor" ? [{ name: "rol", label: "Rol", type: "select", options: roles.filter((r) => r[0] !== "conductor"), value: u.rol }] : []),
      { name: "email", label: "Correo", type: "email", value: u.email },
      ...campoEmpresa(u.empresa_id, "Empresa (obligatoria para Transportista y Conductor)"),
      { name: "activo", label: "Activo (con «No» el usuario no puede entrar)", type: "select", options: [["true", "Sí"], ["false", "No"]], value: String(!!u.activo) },
    ] : [
      { name: "rol", label: "Rol", type: "select", options: roles, value: "transportista" },
      { name: "username", label: "Usuario (no aplica a Conductor: entra con su cédula)" },
      { name: "nombre", label: "Nombre", required: true },
      { name: "email", label: "Correo", type: "email" },
      ...campoEmpresa("", "Empresa (obligatoria para Transportista y Conductor)"),
      { name: "cedula", label: "Cédula (solo Conductor)" },
      { name: "telefono", label: "Teléfono (solo Conductor)" },
      { name: "clave", label: "Clave inicial (vacío = se genera una temporal)", type: "password", min: 6 },
    ];
    return vistaCrud(v, {
      titulo: GESTIONA ? "Usuarios" : "Usuarios · conductores de su empresa",
      url: "/api/admin/usuarios?incluir_conductores=true", key: "id", puedeCrear: GESTIONA,
      cols: [["username", "Usuario"], ["nombre", "Nombre"], ["rol", "Rol"], ["empresa_nombre", "Empresa"], ["email", "Correo"], ["activo", "Activo"]],
      fmt: { rol: (x) => esc(ROLES[x] || x), activo: (x, u) => (activable(u) ? selActivo(u) : (x ? "Sí" : '<span class="muted">No</span>')) },
      alMontar: (recargar) => {
        $("#tbl").addEventListener("change", async (e) => {
          const sel = e.target.closest("select[data-activo]");
          if (!sel) return;
          const activo = sel.value === "true";
          if (!activo && !confirm("Con «No» el usuario pierde el acceso de inmediato (se cierran sus sesiones) hasta que vuelva a «Sí». ¿Continuar?")) {
            sel.value = "true"; return;
          }
          try {
            const u = await api(`/api/admin/usuarios/${sel.dataset.activo}/activo`, { method: "POST", body: { activo } });
            toast(`${u.nombre}: Activo = ${u.activo ? "Sí" : "No"}`);
            await recargar();
          } catch (ex) { sel.value = String(!activo); toast(ex.message); }
        });
      },
      nuevo: (recargar) => formulario("Nuevo usuario", campos(), async (fd) => {
        const r = await api("/api/admin/usuarios", { method: "POST", body: conEmpresa(datos(fd, campos())) });
        await recargar();
        if (r.credencial) setTimeout(() => mostrarCredenciales([r.credencial]), 50);
      }),
      acciones: [
        ...(GESTIONA ? [{ id: "ed", t: "Editar", fn: (u, recargar) => formulario("Editar usuario", campos(u), async (fd) => {
          const d = conEmpresa(datos(fd, campos(u))); delete d.username;
          d.activo = d.activo === "true";
          await api(`/api/admin/usuarios/${u.id}`, { method: "PATCH", body: d }); await recargar();
        }) }] : []),
        ...(puede("usuarios.claves.gestionar") || puede("conductores.claves.gestionar") ? [{ id: "cl", t: "Clave", fn: async (u) => {
          if (u.id === me.id) { toast("Para su propia clave use «Mi clave»"); return; }
          dialogoClave(u.nombre, `/api/admin/usuarios/${u.id}/clave`);
        } }] : []),
        ...(puede("usuarios.credenciales.consultar") ? [{ id: "cr", t: "Estado de clave", fn: async (u) => verCredencial(u) }] : []),
        ...(GESTIONA ? [{ id: "del", t: "Eliminar", cls: "danger", fn: async (u, recargar) => {
          if (!confirm(`¿Eliminar el usuario ${u.username}?`)) return;
          await api(`/api/admin/usuarios/${u.id}`, { method: "DELETE" }); await recargar();
        } }] : []),
      ],
    });
  }

  // ------------------------------------------------------------------ Empresas (solo Supervisor)
  function vistaEmpresas(v) {
    const campos = (e = {}) => [
      { name: "nombre", label: "Nombre", value: e.nombre, required: true },
      { name: "rif", label: "RIF (ej. J-12345678-9)", value: e.rif },
      { name: "telefono", label: "Teléfono", value: e.telefono },
      { name: "email", label: "Correo", type: "email", value: e.email },
      { name: "direccion", label: "Dirección", type: "textarea", value: e.direccion },
      ...(e.id ? [{ name: "activo", label: "Activa (si se desactiva, sus usuarios pierden el acceso)", type: "checkbox", value: e.activo }] : []),
    ];
    const recargarTodo = async (recargar) => { await recargar(); await cargarEmpresas(); };
    const GESTIONA = puede("empresas.gestionar");
    return vistaCrud(v, {
      titulo: "Empresas de transporte", url: "/api/admin/empresas", key: "id", puedeCrear: GESTIONA,
      cols: [["nombre", "Empresa"], ["rif", "RIF"], ["telefono", "Teléfono"], ["email", "Correo"], ["usuarios", "Usuarios"], ["remesas", "Remesas"], ["activo", "Activa"]],
      fmt: { activo: (x) => (x ? "Sí" : '<span class="muted">No</span>') },
      nuevo: (recargar) => formulario("Nueva empresa", campos(), async (fd) => {
        await api("/api/admin/empresas", { method: "POST", body: datos(fd, campos()) }); await recargarTodo(recargar);
      }),
      acciones: [
        ...(GESTIONA ? [{ id: "ed", t: "Editar", fn: (e, recargar) => formulario("Editar empresa", campos(e), async (fd) => {
          await api(`/api/admin/empresas/${e.id}`, { method: "PATCH", body: datos(fd, campos(e)) }); await recargarTodo(recargar);
        }) }] : []),
        { id: "usr", t: "Usuarios", fn: async (e) => {
          const us = await api(`/api/admin/usuarios?incluir_conductores=true&empresa_id=${e.id}`);
          abrirDialogo(`<h2 style="margin-top:0">Usuarios de ${esc(e.nombre)}</h2>${us.length ? `<div class="table-wrap"><table><tr><th>Usuario</th><th>Nombre</th><th>Rol</th><th>Activo</th></tr>
            ${us.map((u) => `<tr><td class="mono">${esc(u.username)}</td><td>${esc(u.nombre)}</td><td>${esc(ROLES[u.rol] || u.rol)}</td><td>${u.activo ? "Sí" : "No"}</td></tr>`).join("")}</table></div>`
            : '<p class="muted">Sin usuarios.</p>'}`, '<button type="submit" class="primary">Cerrar</button>', null);
        } },
        ...(GESTIONA ? [{ id: "del", t: "Eliminar", cls: "danger", fn: async (e, recargar) => {
          if (!confirm(`¿Eliminar ${e.nombre}? Si tiene usuarios o historial se desactiva y sus usuarios pierden el acceso.`)) return;
          const r = await api(`/api/admin/empresas/${e.id}`, { method: "DELETE" });
          toast(r.eliminada ? "Empresa eliminada" : "Empresa desactivada (tiene historial)"); await recargarTodo(recargar);
        } }] : []),
      ],
    });
  }

  // ------------------------------------------------------------------ Claves y auditoría
  function dialogoClave(nombre, url) {
    abrirDialogo(`<h2 style="margin-top:0">Clave de ${esc(nombre)}</h2>
      <p class="muted" style="margin:0">Las claves se guardan cifradas y no pueden consultarse. Puede generar una temporal o fijar una nueva.
        En ambos casos se cierran las sesiones abiertas de ese usuario.</p>
      <div class="row"><input type="radio" name="modo" id="mTemp" value="temp" checked><label for="mTemp" style="margin:0">Generar una clave temporal (recomendado)</label></div>
      <div class="row"><input type="radio" name="modo" id="mFija" value="fija"><label for="mFija" style="margin:0">Fijar esta clave:</label></div>
      <div><input id="cNueva" type="password" minlength="6" autocomplete="new-password" placeholder="Mínimo 6 caracteres"></div>
      <div class="row"><input type="checkbox" id="cCambiar" checked><label for="cCambiar" style="margin:0">Clave temporal: pedir el cambio (3 accesos con aviso; luego obligatorio)</label></div>
      <div id="dlgErr" class="alert err hidden"></div>`,
    `<button type="button" data-cerrar>Cancelar</button><button type="submit" class="primary">Aplicar</button>`, async () => {
      const fija = $("#mFija").checked;
      const body = fija ? { clave_nueva: $("#cNueva").value, debe_cambiar_clave: $("#cCambiar").checked } : {};
      if (fija && body.clave_nueva.length < 6) throw new Error("La clave debe tener al menos 6 caracteres");
      const r = await api(url, { method: "POST", body });
      if (r.clave_temporal) setTimeout(() => mostrarCredenciales([r], "Clave temporal generada"), 50);
      else toast("Clave actualizada. El usuario deberá entrar con la nueva clave.");
    });
    $("#cNueva").addEventListener("input", () => { $("#mFija").checked = true; });
  }

  async function verCredencial(u) {
    const c = await api(`/api/admin/usuarios/${u.id}/credencial`);
    abrirDialogo(`<h2 style="margin-top:0">Estado de la clave · ${esc(c.nombre)}</h2>
      <table>
        <tr><td>Usuario</td><td class="mono">${esc(c.usuario)}</td></tr>
        <tr><td>Rol</td><td>${esc(ROLES[c.rol] || c.rol)}</td></tr>
        <tr><td>Activo</td><td>${c.activo ? "Sí" : "No"}</td></tr>
        <tr><td>Clave</td><td>${c.tiene_clave ? "Asignada" : "Sin clave"} · cifrado ${esc(c.algoritmo)}</td></tr>
        <tr><td>Debe cambiarla al entrar</td><td>${c.debe_cambiar_clave ? "Sí" : "No"}</td></tr>
        <tr><td>Último cambio</td><td>${esc(fmtFechaHora(c.clave_actualizada_at) || "—")}${c.clave_actualizada_por ? " · por " + esc(c.clave_actualizada_por) : ""}</td></tr>
        <tr><td>Último ingreso</td><td>${esc(fmtFechaHora(c.ultimo_acceso_at) || "Nunca")}</td></tr>
      </table>
      <p class="muted">${esc(c.nota)}</p>`,
    `${puede("usuarios.claves.gestionar") && u.id !== me.id ? '<button type="button" id="btnRest">Restablecer clave</button>' : ""}<button type="submit" class="primary">Cerrar</button>`, null);
    const b = $("#btnRest");
    if (b) b.onclick = () => { dlg.close(); dialogoClave(c.nombre, `/api/admin/usuarios/${u.id}/clave`); };
  }

  async function vistaAuditoria(v) {
    v.innerHTML = `<h1>Auditoría</h1>
      <div class="subtabs" role="tablist">
        <button type="button" role="tab" data-sub="seg" class="active">Auditoría de seguridad</button>
        <button type="button" role="tab" data-sub="bit">Bitácora</button>
      </div>
      <div id="audSub"></div>`;
    const usuarios = await api("/api/admin/usuarios?incluir_conductores=true").catch(() => []);
    const subs = { seg: () => auditoriaSeguridad($("#audSub"), usuarios), bit: () => bitacora($("#audSub"), usuarios) };
    v.querySelector(".subtabs").onclick = (e) => {
      const b = e.target.closest("button[data-sub]");
      if (!b) return;
      $$(".subtabs button", v).forEach((x) => x.classList.toggle("active", x === b));
      TL.registrarVista("auditoria-" + (b.dataset.sub === "bit" ? "bitacora" : "seguridad"), b.textContent);
      subs[b.dataset.sub]();
    };
    await subs.seg();
  }

  async function auditoriaSeguridad(box, usuarios) {
    const ACC = {
      usuario_creado: "Usuario creado", conductor_creado: "Conductor creado", usuario_editado: "Usuario editado",
      usuario_eliminado: "Usuario eliminado", clave_cambiada: "Cambió su clave", clave_establecida: "Clave fijada por administrador",
      clave_restablecida: "Clave temporal generada", credencial_consultada: "Consulta de credencial",
      usuario_activado: "Usuario activado (Activo = Sí)", usuario_desactivado: "Usuario desactivado (Activo = No)",
      empresa_creada: "Empresa creada", empresa_editada: "Empresa editada", empresa_eliminada: "Empresa eliminada", empresa_desactivada: "Empresa desactivada",
      base_inicializada: "Base de datos inicializada (limpieza)",
      empresa_transporte_cambiada: "Empresa de transporte cambiada (remesa y sus facturas)",
    };
    const eventos = await api("/api/admin/auditoria?limite=300");
    const nombre = new Map(usuarios.map((u) => [u.id, `${u.nombre} (${u.username})`]));
    box.innerHTML = `<p class="muted">Altas de usuarios y conductores, cambios de rol, de clave y de estado Activo. Nunca se registran las claves.</p>
      <div class="card table-wrap">${eventos.length ? `<table><tr><th>Fecha</th><th>Acción</th><th>Quién</th><th>Sobre</th><th class="hide-sm">Detalle</th></tr>
      ${eventos.map((e) => `<tr><td><small>${esc(fmtFechaHora(e.created_at))}</small></td><td>${esc(ACC[e.accion] || e.accion)}</td>
        <td>${esc(nombre.get(e.actor_id) || (e.actor_rol ? "(" + e.actor_rol + ")" : "—"))}</td>
        <td>${esc(nombre.get(e.objetivo_usuario_id) || e.detalle.usuario || e.detalle.cedula || "—")}</td>
        <td class="hide-sm"><small class="mono">${esc(JSON.stringify(e.detalle))}</small></td></tr>`).join("")}</table>` : '<span class="muted">Sin registros.</span>'}</div>`;
  }

  // Bitácora de actividad: todo lo que hace cada usuario (solo lectura, inmutable)
  const ACC_BIT = {
    LOGIN: "Inicio de sesión", LOGIN_FALLIDO: "Intento de acceso fallido", LOGOUT: "Cierre de sesión", VISTA: "Abrió pantalla",
    CONSULTAR: "Consultar", CREAR: "Crear", MODIFICAR: "Modificar", ELIMINAR: "Eliminar",
  };
  async function bitacora(box, usuarios) {
    const hoy = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    box.innerHTML = `<p class="muted">Registro inmutable de la actividad de cada usuario: inicio y cierre de sesión, pantallas abiertas,
      consultas, altas, cambios y bajas, con el detalle de la operación (sin claves). No se puede editar ni borrar.</p>
      <form id="bitF" class="card stack">
        <div class="row">
          <label style="margin:0" for="bDesde">Desde</label><input type="date" id="bDesde" name="desde" value="${hoy}" style="width:auto">
          <label style="margin:0" for="bHasta">Hasta</label><input type="date" id="bHasta" name="hasta" value="${hoy}" style="width:auto">
          <select name="accion" style="width:auto" aria-label="Acción"><option value="">Todas las acciones</option>
            ${Object.entries(ACC_BIT).map(([k, t]) => `<option value="${k}">${esc(t)}</option>`).join("")}</select>
          <select name="exito" style="width:auto" aria-label="Resultado"><option value="">Todos los resultados</option><option value="true">Correctas</option><option value="false">Rechazadas</option></select>
        </div>
        <div class="row">
          <select name="usuario_id" style="width:auto" aria-label="Usuario"><option value="">Todos los usuarios</option>
            ${usuarios.map((u) => `<option value="${u.id}">${esc(u.nombre)} (${esc(u.username)})</option>`).join("")}</select>
          <select name="rol" style="width:auto" aria-label="Rol"><option value="">Todos los roles</option>
            ${Object.entries(ROLES).map(([k, t]) => `<option value="${k}">${esc(t)}</option>`).join("")}</select>
          ${EMPRESAS.length ? `<select name="empresa_id" style="width:auto" aria-label="Empresa"><option value="">Todas las empresas</option>
            ${EMPRESAS.map((e) => `<option value="${e.id}">${esc(e.nombre)}</option>`).join("")}</select>` : ""}
          <input name="modulo" placeholder="Módulo (ej. remesas)" style="width:160px" aria-label="Módulo">
          <input name="q" type="search" placeholder="Usuario o ruta…" style="flex:1 1 160px;width:auto" aria-label="Buscar">
          <button type="submit" class="primary">Filtrar</button>
        </div>
        <small id="bErr" class="lvl-error" role="alert"></small>
      </form>
      <div id="bitOut" class="card table-wrap" style="margin-top:12px"></div>
      <section id="bitArch" class="card stack" style="margin-top:12px"></section>`;
    const LIM = 50;
    const cargar = async (offset = 0) => {
      const fd = new FormData($("#bitF"));
      if (fd.get("desde") && fd.get("hasta") && fd.get("desde") > fd.get("hasta")) { $("#bErr").textContent = "La fecha Desde no puede ser posterior a Hasta."; return; }
      $("#bErr").textContent = "";
      const p = new URLSearchParams([...fd].filter(([, x]) => x));
      p.set("limit", LIM); p.set("offset", offset);
      const out = $("#bitOut");
      out.innerHTML = '<span class="muted">Cargando…</span>';
      try {
        const r = await api("/api/admin/bitacora?" + p);
        const aviso = r.archivados.length ? `<div class="alert">Período(s) archivado(s) en este rango: <strong>${esc(r.archivados.join(", "))}</strong>.
          Sus registros ya no están en la bitácora activa: descárguelos en «Archivo de la bitácora».</div>` : "";
        out.innerHTML = aviso + (r.items.length ? `<table class="bitacora"><tr><th>Fecha y hora</th><th>Usuario</th><th class="hide-sm">Rol · Empresa</th><th>Acción</th><th>Módulo / ruta</th><th>Resultado</th><th>Detalle</th></tr>
          ${r.items.map((b) => `<tr class="${b.exito ? "" : "rechazada"}">
            <td><small>${esc(fmtFechaHora(b.ocurrido_at))}</small></td>
            <td>${esc(b.usuario_nombre || b.detalle.usuario_intentado || "—")}<div><small class="mono muted">${esc(b.usuario || "")}</small></div></td>
            <td class="hide-sm"><small>${esc(ROLES[b.rol] || b.rol || "—")}${b.empresa_nombre ? " · " + esc(b.empresa_nombre) : ""}</small></td>
            <td><span class="acc acc-${esc(b.accion.toLowerCase())}">${esc(ACC_BIT[b.accion] || b.accion)}</span></td>
            <td><small>${esc(b.modulo || "")}${b.detalle.vista ? " · " + esc(b.detalle.titulo || b.detalle.vista) : ""}</small><div><small class="mono muted">${esc(b.metodo || "")} ${esc(b.ruta)}</small></div></td>
            <td><small class="${b.exito ? "ok-txt" : "lvl-error"}">${b.estado_http ?? ""} ${b.exito ? "OK" : "Rechazada"}</small></td>
            <td><details><summary><small>Ver</small></summary><pre class="mono" style="white-space:pre-wrap;max-width:420px;font-size:.75rem">${esc(JSON.stringify(b.detalle, null, 2))}</pre>
              <small class="muted">IP ${esc(b.ip || "—")} · ${b.duracion_ms ?? "?"} ms${b.recurso_id ? " · recurso " + esc(b.recurso_id) : ""}</small></details></td></tr>`).join("")}</table>
          ${paginador(r.total, offset, LIM)}` : '<span class="muted">Sin actividad para el filtro indicado.</span>');
      } catch (ex) { out.innerHTML = `<div class="alert err">${esc(ex.message)}</div>`; }
    };
    $("#bitF").onsubmit = (e) => { e.preventDefault(); cargar(0); };
    $("#bitOut").onclick = (e) => { if (e.target.dataset.pag) cargar(Number(e.target.dataset.pag)); };
    await cargar(0);
    await archivoBitacora(() => cargar(0));
  }

  // Archivo por mes o por año: saca períodos cerrados de la bitácora activa y los guarda comprimidos
  async function archivoBitacora(alArchivar) {
    const box = $("#bitArch");
    if (!box) return;
    const kb = (n) => (n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`);
    const nombreP = (a) => a.tipo === "anio" ? `Año ${a.periodo}` : a.periodo;
    const pintar = async () => {
      const r = await api("/api/admin/bitacora/archivos");
      const PUEDE = puede("bitacora.archivar");
      const hoy = new Date();
      const mesAnterior = new Date(hoy.getFullYear(), hoy.getMonth() - 1, 1);
      const defMes = `${mesAnterior.getFullYear()}-${String(mesAnterior.getMonth() + 1).padStart(2, "0")}`;
      box.innerHTML = `<h2 style="margin:0">Archivo de la bitácora (por mes o por año)</h2>
        <p class="muted" style="margin:0">Saca un mes o un año <strong>ya terminado</strong> de la bitácora activa y lo guarda comprimido, sin perder nada:
          se puede descargar en CSV (Excel) o en su formato original con su firma SHA-256. Bitácora activa: <strong>${r.registros_activos.toLocaleString("es-VE")}</strong> registros.</p>
        ${PUEDE ? `<div class="row">
            <select id="aTipo" style="width:auto" aria-label="Archivar por"><option value="mes">Por mes</option><option value="anio">Por año</option></select>
            <input id="aMes" type="month" value="${defMes}" max="${defMes}" style="width:auto" aria-label="Mes">
            <input id="aAnio" type="number" min="2020" max="${hoy.getFullYear() - 1}" value="${hoy.getFullYear() - 1}" style="width:110px" class="hidden" aria-label="Año">
            <button type="button" class="primary" id="aBtn">Archivar</button></div>` : ""}
        ${r.pendientes.length ? `<div><small class="muted">Períodos cerrados que siguen en la bitácora activa:</small>
          <div class="row" style="flex-wrap:wrap">${r.pendientes.map((p) => `<span class="chip">${esc(p.nombre)} · ${p.filas.toLocaleString("es-VE")} reg.
            ${PUEDE ? `<button type="button" class="small" data-arch-tipo="${p.tipo}" data-arch="${esc(p.periodo)}">Archivar</button>` : ""}</span>`).join("")}</div></div>`
          : '<small class="muted">No hay períodos cerrados pendientes de archivar.</small>'}
        <div class="table-wrap">${r.archivos.length ? `<table><tr><th>Período</th><th>Registros</th><th class="hide-sm">Tamaño</th><th class="hide-sm">Archivado</th><th>Descargar</th></tr>
          ${r.archivos.map((a) => `<tr><td><strong>${esc(nombreP(a))}</strong><div><small class="muted mono" title="SHA-256">${esc(a.sha256.slice(0, 16))}…</small></div></td>
            <td>${a.filas.toLocaleString("es-VE")}</td><td class="hide-sm">${kb(a.bytes)}</td>
            <td class="hide-sm"><small>${esc(fmtFechaHora(a.created_at))}<br>${esc(a.creado_por || "")}</small></td>
            <td class="row"><button type="button" class="small" data-desc="${a.id}" data-fmt="csv" data-nom="${esc(a.periodo)}">CSV (Excel)</button>
              <button type="button" class="small" data-desc="${a.id}" data-fmt="jsonl.gz" data-nom="${esc(a.periodo)}">Original</button></td></tr>`).join("")}</table>`
          : '<span class="muted">Todavía no hay períodos archivados.</span>'}</div>`;
      const tipo = $("#aTipo");
      if (tipo) tipo.onchange = () => { $("#aMes").classList.toggle("hidden", tipo.value !== "mes"); $("#aAnio").classList.toggle("hidden", tipo.value !== "anio"); };
    };
    const archivar = async (t, periodo) => {
      const nom = t === "anio" ? `el año ${periodo}` : `el mes ${periodo}`;
      if (!confirm(`¿Archivar ${nom}? Sus registros salen de la bitácora activa y quedan guardados comprimidos (se pueden descargar cuando quiera).`)) return;
      try {
        const a = await api("/api/admin/bitacora/archivar", { method: "POST", body: { tipo: t, periodo } });
        toast(`Archivado ${a.periodo}: ${a.filas.toLocaleString("es-VE")} registros`);
        await pintar(); alArchivar && alArchivar();
      } catch (ex) { toast(ex.message, 6000); }
    };
    box.onclick = (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.id === "aBtn") {
        const t = $("#aTipo").value;
        const periodo = t === "mes" ? $("#aMes").value : String($("#aAnio").value || "");
        if (!periodo) { toast("Indique el período"); return; }
        archivar(t, periodo);
      } else if (b.dataset.arch) archivar(b.dataset.archTipo, b.dataset.arch);
      else if (b.dataset.desc) {
        const ext = b.dataset.fmt === "csv" ? "csv" : "jsonl.gz";
        TL.download(`/api/admin/bitacora/archivos/${b.dataset.desc}/descargar?formato=${b.dataset.fmt}`, `bitacora_${b.dataset.nom}.${ext}`)
          .catch((ex) => toast(ex.message));
      }
    };
    await pintar();
  }

  // ------------------------------------------------------------------ Reportes
  async function vistaReportes(v) {
    const cfg = await api("/api/config");
    // Día local (toISOString es UTC: de noche en Venezuela daría el día siguiente)
    const hoy = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    v.innerHTML = `<h1>Cobranzas y reportes</h1>
      <section class="card stack">
        <h2 style="margin-top:0">Excel de entregas para Cobranzas</h2>
        <p class="muted" style="margin:0">Mismo formato que Libro2, con Cod. Vendedor, estatus y fecha/hora del estatus. Incluye las facturas cuya <strong>Fecha Estatus</strong> (último cambio de estatus) está entre Desde y Hasta, ambos días incluidos.</p>
        <div class="row"><label style="margin:0" for="xDesde">Desde</label><input type="date" id="xDesde" value="${hoy}" style="width:auto">
          <label style="margin:0" for="xHasta">Hasta</label><input type="date" id="xHasta" value="${hoy}" style="width:auto">
          <input type="checkbox" id="xFin"><label for="xFin" style="margin:0">Solo facturas con estatus final</label></div>
        <small id="xErr" class="lvl-error" role="alert"></small>
        <div class="row"><input id="xCli" type="search" placeholder="Cliente (contiene…)" style="flex:1 1 200px;width:auto" aria-label="Filtrar por cliente">
          <select id="xCond" style="width:auto" aria-label="Filtrar por conductor"><option value="">Todos los conductores</option></select></div>
        <div class="row"><button type="button" class="primary" id="btnXls">Descargar Excel</button>
          <button type="button" id="btnVer">Visualizar por pantalla</button>
          ${puede("notificaciones.enviar") ? `<button type="button" id="btnMailCob" ${cfg.correo ? "" : "disabled"}>Enviar por correo a Cobranzas</button>` : ""}</div>
      </section>
      ${puede("notificaciones.enviar") ? `<section class="card stack" style="margin-top:12px">
        <h2 style="margin-top:0">Avisar a vendedores</h2>
        <p class="muted" style="margin:0">Cada vendedor recibe solo el estatus de sus facturas (sin montos). Requiere el correo del vendedor en la sección Vendedores.</p>
        <div class="row"><select id="nModo" style="width:auto"><option value="en_curso">Facturas por entregar</option><option value="final">Facturas con estatus final</option></select>
          <label style="margin:0" for="nDesde">Desde</label><input type="date" id="nDesde" value="${hoy}" style="width:auto">
          <label style="margin:0" for="nHasta">Hasta</label><input type="date" id="nHasta" value="${hoy}" style="width:auto"></div>
        <small id="nErr" class="lvl-error" role="alert"></small>
        <div class="row"><button type="button" id="btnSim">Vista previa</button><button type="button" class="primary" id="btnEnv" ${cfg.correo ? "" : "disabled"}>Enviar correos</button>
          ${cfg.correo ? "" : '<small class="muted">El envío de correos está desactivado (EMAIL_ENABLED). Puede usar la vista previa.</small>'}</div>
        <div id="nOut"></div>
      </section>` : ""}
      <section class="card stack" style="margin-top:12px" id="secInd">
        <h2 style="margin-top:0">Indicador de Gestión</h2>
        <p class="muted" style="margin:0">Tiempo entre procesos de cada factura (Factura → Preparado → Por Entregar → Entregado o Devolución) en <strong>días hábiles</strong> (lunes a viernes de 7:00 a 16:00): no cuentan noches, fines de semana ni feriados. Incluye la suma y el promedio de los tiempos.</p>
        <div class="row"><select id="iCampo" style="width:auto" aria-label="Fecha sobre la que se filtra"></select>
          <label style="margin:0" for="iDesde">Desde</label><input type="date" id="iDesde" value="${hoy.slice(0, 8)}01" style="width:auto">
          <label style="margin:0" for="iHasta">Hasta</label><input type="date" id="iHasta" value="${hoy}" style="width:auto"></div>
        <small id="iErr" class="lvl-error" role="alert"></small>
        <fieldset class="multi-estatus"><legend>Estatus (ninguno marcado = todos)</legend><div class="row" id="iEst"></div></fieldset>
        <div class="row"><input id="iCli" type="search" placeholder="Cliente (contiene…)" style="flex:1 1 200px;width:auto" aria-label="Cliente">
          <select id="iVend" style="width:auto" aria-label="Vendedor"><option value="">Todos los vendedores</option></select>
          <select id="iZona" style="width:auto" aria-label="Zona (Destino)"><option value="">Todas las zonas</option></select>
          <select id="iEmp" style="width:auto" aria-label="Empresa de transporte"><option value="">Todas las empresas de transporte</option></select></div>
        <div class="row"><button type="button" class="primary" id="iVer">Visualizar por pantalla</button>
          <button type="button" id="iXls">Descargar Excel</button><button type="button" id="iPrint">Imprimir</button></div>
        <div id="iOut" class="stack"></div>
      </section>
      ${puede("metricas.ver") ? `<section class="card stack" style="margin-top:12px">
        <h2 style="margin-top:0">Métricas</h2>
        <div class="row"><input type="date" id="mDesde" style="width:auto" aria-label="Desde"><input type="date" id="mHasta" style="width:auto" aria-label="Hasta"><button type="button" id="btnMet">Calcular</button></div>
        <small id="mErr" class="lvl-error" role="alert"></small>
        <div id="mOut"></div>
      </section>` : ""}`;
    /** Valida un par Desde/Hasta: ambos obligatorios, Desde ≤ Hasta, máximo 366 días. Muestra el error. */
    const rango = (dSel, hSel, errSel, opcional = false) => {
      const d = $(dSel).value, h = $(hSel).value, err = $(errSel);
      let msg = "";
      if (!opcional && (!d || !h)) msg = "Indique las fechas Desde y Hasta.";
      else if (d && h && d > h) msg = `La fecha Desde (${fmtFecha(d)}) no puede ser posterior a Hasta (${fmtFecha(h)}).`;
      else if (d && h && (new Date(h) - new Date(d)) / 864e5 >= 366) msg = "El rango no puede superar 366 días.";
      err.textContent = msg;
      [$(dSel), $(hSel)].forEach((i) => i.setAttribute("aria-invalid", msg ? "true" : "false"));
      return !msg;
    };
    // Cada calendario limita al otro (no deja elegir un Hasta anterior a Desde)
    const enlazar = (dSel, hSel, errSel, opcional) => {
      const sync = () => { $(hSel).min = $(dSel).value || ""; $(dSel).max = $(hSel).value || ""; rango(dSel, hSel, errSel, opcional); };
      [dSel, hSel].forEach((s) => $(s).addEventListener("change", sync));
      sync();
    };
    enlazar("#xDesde", "#xHasta", "#xErr");
    if ($("#nDesde")) enlazar("#nDesde", "#nHasta", "#nErr");
    if ($("#mDesde")) enlazar("#mDesde", "#mHasta", "#mErr", true);
    const rangoX = () => rango("#xDesde", "#xHasta", "#xErr");
    $("#xTabla") || $("#btnVer").closest("section").insertAdjacentHTML("beforeend", '<div id="xTabla" class="stack"></div>');
    api("/api/admin/conductores?incluir_inactivos=true").then((cs) => {
      $("#xCond").innerHTML += cs.map((c) => `<option value="${c.id}">${esc(c.nombre)}</option>`).join("");
    }).catch(() => {});
    // Mismos filtros para el Excel y para la vista en pantalla: lo que se ve es lo que se descarga
    const q = () => {
      const p = new URLSearchParams({ desde: $("#xDesde").value, hasta: $("#xHasta").value, solo_finales: $("#xFin").checked });
      if ($("#xCli").value.trim()) p.set("cliente", $("#xCli").value.trim());
      if ($("#xCond").value) p.set("conductor_id", $("#xCond").value);
      return p.toString();
    };
    const tabla = { visible: false, orden: null, desc: false, offset: 0, limit: 50 };
    const FMT = {
      monto: (x) => esc(fmtMonto(x)),
      pct: (x) => (x === null || x === undefined ? "" : esc((Number(x) * 100).toLocaleString("es-VE", { maximumFractionDigits: 2 }) + " %")),
      fecha: (x) => esc(fmtFecha(x)),
      fechahora: (x) => esc(fmtMomento(x)),
      entero: (x) => esc(x ?? ""),
      texto: (x) => esc(x ?? ""),
    };
    const CLAVE_ESTATUS = Object.fromEntries(Object.entries(ESTATUS).map(([k, t]) => [t, k]));
    const celda = (col, fila) => col.clave === "estatus" ? badge(CLAVE_ESTATUS[fila.estatus] || fila.estatus) : FMT[col.tipo](fila[col.clave]);
    const NUM = new Set(["monto", "pct", "entero"]);
    async function verTabla(offset = 0) {
      if (!rangoX()) return;
      tabla.visible = true; tabla.offset = offset;
      const box = $("#xTabla");
      box.innerHTML = '<div class="procesando" role="status"><span class="spinner" aria-hidden="true"></span><span>Cargando reporte…</span></div>';
      try {
        const p = new URLSearchParams(q());
        p.set("limit", tabla.limit); p.set("offset", offset); p.set("desc", tabla.desc);
        if (tabla.orden) p.set("orden", tabla.orden);
        const r = await api("/api/reportes/cobranzas?" + p);
        if (!r.total) { box.innerHTML = '<div class="alert">No hay facturas con Fecha Estatus en el rango indicado.</div>'; return; }
        const flecha = (c) => tabla.orden === c ? (tabla.desc ? " ▼" : " ▲") : "";
        const aria = (c) => tabla.orden === c ? (tabla.desc ? "descending" : "ascending") : "none";
        box.innerHTML = `
          <div class="row between"><strong>${r.totales.facturas} factura(s) en ${r.totales.remesas} remesa(s)</strong>
            <label class="row" style="margin:0"><small>Filas por página</small><select id="xLim" style="width:auto">${[25, 50, 100, 200].map((n) => `<option ${n === tabla.limit ? "selected" : ""}>${n}</option>`).join("")}</select></label></div>
          <div class="table-wrap reporte"><table>
            <thead><tr>${r.columnas.map((c) => `<th aria-sort="${aria(c.clave)}" class="${NUM.has(c.tipo) ? "num" : ""}"><button type="button" class="sort" data-orden="${c.clave}">${esc(c.titulo)}${flecha(c.clave)}</button></th>`).join("")}</tr></thead>
            <tbody>${r.items.map((f) => `<tr>${r.columnas.map((c) => `<td class="${NUM.has(c.tipo) ? "num" : ""}">${celda(c, f)}</td>`).join("")}</tr>`).join("")}</tbody>
            <tfoot><tr>${r.columnas.map((c) => `<td class="num"><strong>${["total_fact", "total_neto", "total_flete"].includes(c.clave) ? esc(fmtMonto(r.totales[c.clave])) : c.clave === "remesa_numero" ? "TOTALES" : ""}</strong></td>`).join("")}</tr></tfoot>
          </table></div>
          ${paginador(r.total, offset, tabla.limit)}`;
        $("#xLim").onchange = () => { tabla.limit = Number($("#xLim").value); verTabla(0); };
      } catch (e) { box.innerHTML = `<div class="alert err">${esc(e.message)}</div>`; }
    }
    $("#btnVer").onclick = () => verTabla(0);
    $("#xTabla").addEventListener("click", (e) => {
      const s = e.target.closest("button.sort");
      if (s) {
        tabla.desc = tabla.orden === s.dataset.orden ? !tabla.desc : false;
        tabla.orden = s.dataset.orden;
        verTabla(0);
      } else if (e.target.dataset.pag) verTabla(Number(e.target.dataset.pag));
    });
    let tCli = null;
    const refrescar = () => { if (tabla.visible) { clearTimeout(tCli); tCli = setTimeout(() => verTabla(0), 350); } };
    ["#xDesde", "#xHasta", "#xFin", "#xCond"].forEach((s) => $(s).addEventListener("change", refrescar));
    $("#xCli").addEventListener("input", refrescar);
    $("#btnXls").onclick = () => rangoX() && TL.download("/api/reportes/cobranzas.xlsx?" + q(), "Entregas_Cobranzas.xlsx").catch((e) => toast(e.message));
    if ($("#btnMailCob")) $("#btnMailCob").onclick = async () => {
      if (!rangoX()) return;
      try {
        const r = await api("/api/reportes/notificar/cobranzas", { method: "POST", body: { desde: $("#xDesde").value, hasta: $("#xHasta").value, solo_finales: $("#xFin").checked } });
        toast(`Enviado a ${r.para.join(", ")}: ${r.facturas} facturas`);
      } catch (e) { toast(e.message); }
    };
    // Desglose: una línea por factura, agrupadas por vendedor (el vendedor ocupa las filas de sus facturas)
    const desglose = (r) => !r.desglose.length
      ? '<div class="alert">No hay facturas de vendedores con Fecha Estatus en el rango indicado.</div>'
      : `<div class="table-wrap reporte"><table class="desglose">
        <thead><tr><th>Vendedor</th><th>Correo</th><th>Factura</th><th>Pedido</th><th>Cliente</th><th>Estatus</th><th>Fecha estatus</th><th class="hide-sm">Remesa</th></tr></thead>
        <tbody>${r.desglose.map((v) => v.facturas.map((f, i) => `<tr class="${i === 0 ? "grupo" : ""}">
          ${i === 0 ? `<td rowspan="${v.facturas.length}"><strong class="mono">${esc(v.codigo)}</strong>${v.nombre ? `<div>${esc(v.nombre)}</div>` : '<div class="muted">No está en Vendedores</div>'}
            <small class="muted">${v.facturas.length} factura(s)</small></td>
            <td rowspan="${v.facturas.length}">${v.correo ? esc(v.correo) : '<span class="lvl-advertencia">Sin correo: no se le enviará</span>'}</td>` : ""}
          <td class="mono">${esc(f.numero)}</td><td class="mono">${esc(f.pedido || "")}</td><td>${esc(f.cliente || "")}</td>
          <td>${badge(f.estatus)}</td><td>${esc(fmtFechaHora(f.estatus_at))}</td><td class="mono hide-sm">${esc(f.remesa || "")}</td></tr>`).join("")).join("")}</tbody>
      </table></div>`;
    const notificar = async (simular) => {
      if (!rango("#nDesde", "#nHasta", "#nErr")) return;
      $("#nOut").innerHTML = '<div class="procesando" role="status"><span class="spinner" aria-hidden="true"></span><span>Preparando…</span></div>';
      const r = await api("/api/reportes/notificar/vendedores", { method: "POST", body: { modo: $("#nModo").value, desde: $("#nDesde").value, hasta: $("#nHasta").value, simular } });
      $("#nOut").innerHTML = `<div class="alert">${r.vendedores} vendedor(es) · ${r.facturas} factura(s)${simular ? " · vista previa: no se envió nada" : ` · ${r.enviados.length} correo(s) enviado(s)`}${r.sin_correo.length ? ` · sin correo: ${esc(r.sin_correo.join(", "))}` : ""}
        ${r.errores.length ? `<div class="lvl-error">Errores: ${esc(r.errores.map((x) => x.codigo + ": " + x.error).join("; "))}</div>` : ""}</div>
        ${desglose(r)}
        ${r.vista_previa.length ? `<h3>Correos que se enviarían</h3>${r.vista_previa.map((p) => `<details class="card"><summary>${esc(p.codigo)} → ${esc(p.para.join(", "))} · ${esc(p.asunto)}</summary><pre style="white-space:pre-wrap">${esc(p.texto)}</pre></details>`).join("")}` : ""}`;
    };
    if ($("#btnSim")) {
      const fallo = (e) => { $("#nOut").innerHTML = `<div class="alert err">${esc(e.message)}</div>`; };
      $("#btnSim").onclick = () => notificar(true).catch(fallo);
      $("#btnEnv").onclick = () => { if (rango("#nDesde", "#nHasta", "#nErr") && confirm("¿Enviar los correos a los vendedores?")) notificar(false).catch(fallo); };
    }
    indicadorGestion(rango).catch((e) => { $("#iOut").innerHTML = `<div class="alert err">${esc(e.message)}</div>`; });
    if (!$("#btnMet")) return;
    $("#btnMet").onclick = async () => {
      if (!rango("#mDesde", "#mHasta", "#mErr", true)) return;
      const p = new URLSearchParams();
      if ($("#mDesde").value) p.set("desde", $("#mDesde").value);
      if ($("#mHasta").value) p.set("hasta", $("#mHasta").value);
      const m = await api("/api/reportes/metricas?" + p);
      $("#mOut").innerHTML = `<table>
        <tr><td>Período</td><td>${esc(fmtFecha(m.desde))} – ${esc(fmtFecha(m.hasta))}</td></tr>
        <tr><td>Cambios de estatus</td><td>${m.cambios_de_estatus}</td></tr>
        <tr><td>% registrados el mismo día (meta ${m.meta_pct_mismo_dia} %)</td><td><strong>${m.pct_mismo_dia ?? "—"} %</strong></td></tr>
        <tr><td>Cambios sin señal sincronizados</td><td>${m.cambios_sin_senal_sincronizados}</td></tr>
        <tr><td>Consultas públicas (encontradas)</td><td>${m.consultas_publicas} (${m.consultas_publicas_encontradas})</td></tr>
        ${Object.entries(m.facturas_por_estatus).map(([k, n]) => `<tr><td>Facturas ${esc(k)}</td><td>${n}</td></tr>`).join("")}</table>`;
    };
    $("#btnMet").click();
  }

  // ------------------------------------------------------------------ Indicador de Gestión
  /** Reporte de tiempos entre procesos (días hábiles de 9 h, lunes a viernes): pantalla, Excel e impresión. */
  async function indicadorGestion(rango) {
    const op = await api("/api/reportes/indicador/opciones");
    $("#iCampo").innerHTML = op.campos_fecha.map((c) => `<option value="${esc(c.clave)}">${esc(c.titulo)}</option>`).join("");
    $("#iEst").innerHTML = op.estatus.map((e) => `<label class="chk"><input type="checkbox" value="${esc(e.clave)}"> ${esc(e.titulo)}</label>`).join("");
    $("#iVend").innerHTML += op.vendedores.map((v) => `<option value="${esc(v.codigo)}">${esc(v.codigo)} · ${esc(v.nombre)}</option>`).join("");
    $("#iZona").innerHTML += op.zonas.map((z) => `<option>${esc(z)}</option>`).join("");
    $("#iEmp").innerHTML += op.empresas.map((e) => `<option value="${e.id}">${esc(e.nombre)}</option>`).join("");
    if (op.empresas.length < 2) $("#iEmp").classList.add("hidden");  // Transportista: solo la suya
    $("#iDesde").max = $("#iHasta").value;
    const valido = () => rango("#iDesde", "#iHasta", "#iErr");
    // Filtros del reporte (sin la selección hecha con clic en la gráfica ni la paginación)
    const params = () => {
      const p = new URLSearchParams({ desde: $("#iDesde").value, hasta: $("#iHasta").value, campo_fecha: $("#iCampo").value });
      $$("#iEst input:checked").forEach((c) => p.append("estatus", c.value));
      if ($("#iCli").value.trim()) p.set("cliente", $("#iCli").value.trim());
      for (const [id, k] of [["#iVend", "cod_vendedor"], ["#iZona", "zona"], ["#iEmp", "empresa_id"]]) if ($(id).value) p.set(k, $(id).value);
      return p;
    };
    const conSeleccion = (p) => {
      if (st.sel) { p.set("serie", st.sel.serie); if (st.sel.transporte) p.set("transporte", st.sel.transporte); }
      return p;
    };
    const DUR = ["t_preparado", "t_por_entregar", "t_entrega", "t_total"];
    const NUM = new Set(["monto", "duracion"]);
    const FMT = {
      monto: (x) => esc(fmtMonto(x)), fecha: (x) => esc(fmtFecha(x)), fechahora: (x) => esc(fmtMomento(x)),
      duracion: (x) => esc(x ?? ""), texto: (x) => esc(x ?? ""),
    };
    const celda = (c, f) => c.clave === "estatus" ? badge(f.estatus_clave) : c.clave === "numero"
      ? `<a href="#" class="mono" data-fact="${f.factura_id}">${esc(f.numero)}</a>` : FMT[c.tipo](f[c.clave]);
    // SUMA / PROMEDIO: solo de los tiempos entre procesos
    const pie = (r, rotulo, campo) => `<tr><td><strong>${rotulo}</strong></td>${r.columnas.slice(1).map((c) => {
      if (DUR.includes(c.clave)) return `<td class="num dur"><strong>${esc(r.resumen.duraciones[c.clave][campo] || "—")}</strong></td>`;
      return "<td></td>";
    }).join("")}</tr>`;
    const kpis = (s) => `<div class="ind-kpis">${[["Facturas", s.facturas], ["Preparado", s.preparadas], ["Por Entregar", s.por_entregar], ["Entregadas", s.entregadas],
      ["Devoluciones", s.devoluciones], ["Promedio de entrega (hábil)", s.promedio_entrega || "—"]]
      .map(([t, n]) => `<div class="ind-kpi"><small>${esc(t)}</small><strong>${esc(n)}</strong></div>`).join("")}</div>`;
    // sel: clic en la gráfica (serie y, si fue una barra, la empresa de transporte) → la tabla muestra esas facturas
    const st = { orden: null, desc: false, limit: 50, charts: {}, g: null, dona: null, sel: null, firma: null, visible: false, pedido: 0 };
    // Gráfica «Indicador de Gestión»: barras por empresa de transporte y desglose (dona) por empresa emisora
    const graficaHtml = (g) => !g.empresas.length ? "" : `<section class="ind-grafica" aria-label="Gráfica Indicador de Gestión">
        <div class="ind-scroll"><div class="ind-lienzo" style="${window.innerWidth >= 640 ? `min-width:${g.empresas.length * 130}px` : ""}">
          <canvas id="iBar" role="img" aria-label="Gráfica de barras: facturas por estatus y empresa de transporte"></canvas></div></div>
        <small class="muted">Dentro de cada barra: cantidad de facturas (negro) y porcentaje (amarillo). <strong>Entregado incluye las devoluciones.</strong> Clic en una barra o en un estatus de la leyenda: abajo se muestran esas facturas y su desglose por empresa emisora.</small>
        <div id="iDona" class="ind-dona hidden">
          <div class="row between"><small class="muted">Desglose por empresa emisora · clic en otra barra o estatus para cambiarlo</small>
            <button type="button" class="small" id="iDonaX">Cerrar desglose</button></div>
          <div id="iDonaMsg" class="alert hidden"></div>
          <div class="ind-lienzo-dona"><canvas id="iDonaC" role="img" aria-label="Gráfica de dona: facturas por empresa emisora"></canvas></div>
        </div></section>`;
    const cerrarDona = () => {
      if (st.charts.dona) st.charts.dona.destroy();
      st.charts.dona = null; st.dona = null;
      const box = $("#iDona"); if (box) box.classList.add("hidden");
    };
    async function abrirDona(estatus, transporte, desplazar = true) {
      const box = $("#iDona");
      if (!box || !st.g) return;
      if (st.charts.dona) { st.charts.dona.destroy(); st.charts.dona = null; }
      st.dona = { estatus, transporte };
      box.classList.remove("hidden");
      const vacio = !IndicadorGrafica.sectoresDe(st.g, estatus, transporte).length;
      $("#iDonaMsg").classList.toggle("hidden", !vacio);
      $("#iDonaC").parentElement.classList.toggle("hidden", vacio);
      if (vacio) {
        const t = st.g.estatus.find((e) => e.clave === estatus)?.titulo || estatus;
        $("#iDonaMsg").textContent = `No hay facturas en «${t}»${transporte ? " para " + transporte : ""}.`;
      } else {
        st.charts.dona = await IndicadorGrafica.dona($("#iDonaC"), st.g, estatus, transporte);
      }
      if (desplazar) box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    // Clic en la gráfica: desglose (dona) + la tabla de abajo pasa a mostrar esas facturas
    async function elegir(serie, transporte) {
      st.sel = { serie, transporte };
      await abrirDona(serie, transporte);
      ver(0, true);
    }
    function quitarSeleccion() { cerrarDona(); st.sel = null; ver(0, true); }
    async function pintarGrafica(g) {
      Object.values(st.charts).forEach((c) => c && c.destroy());
      st.charts = {}; st.g = g;
      if (!g.empresas.length) return;
      try {
        st.charts.bar = await IndicadorGrafica.barras($("#iBar"), g, elegir);
        if (st.dona) await abrirDona(st.dona.estatus, st.dona.transporte, false);
      } catch (e) { $(".ind-grafica").innerHTML = `<div class="alert err">${esc(e.message)}</div>`; }
    }
    function tablaHtml(r, offset) {
      const flecha = (c) => st.orden === c ? (st.desc ? " ▼" : " ▲") : "";
      const chip = r.seleccion ? `<div class="ind-sel"><span>Mostrando: <strong>${esc(r.seleccion.texto)}</strong> · ${r.total} factura(s)</span>
          <button type="button" class="small" id="iSelX">Ver todas las facturas</button></div>` : "";
      if (!r.total) return `${chip}<div class="alert">No hay facturas en esta selección.</div>`;
      return `${chip}
        <div class="row between"><small class="muted">${esc(r.filtro)}</small>
          <label class="row" style="margin:0"><small>Filas por página</small><select id="iLim" style="width:auto">${[25, 50, 100, 200].map((n) => `<option ${n === st.limit ? "selected" : ""}>${n}</option>`).join("")}</select></label></div>
        <div class="table-wrap reporte indicador"><table>
          <thead><tr>${r.columnas.map((c) => `<th class="${NUM.has(c.tipo) ? "num" : ""}${DUR.includes(c.clave) ? " dur" : ""}"><button type="button" class="sort" data-orden="${c.clave}">${esc(c.titulo)}${flecha(c.clave)}</button></th>`).join("")}</tr></thead>
          <tbody>${r.items.map((f) => `<tr>${r.columnas.map((c) => `<td class="${NUM.has(c.tipo) ? "num" : ""}${DUR.includes(c.clave) ? " dur" : ""}">${celda(c, f)}</td>`).join("")}</tr>`).join("")}</tbody>
          <tfoot>${pie(r, "SUMA", "suma")}${pie(r, "PROMEDIO", "promedio")}</tfoot>
        </table></div>
        ${paginador(r.total, offset, st.limit)}`;
    }
    /** soloTabla: la gráfica no cambia (paginar, ordenar o elegir una barra); si cambian los filtros se rehace todo. */
    async function ver(offset = 0, soloTabla = false) {
      if (!valido()) return;
      st.visible = true;
      const firma = params().toString();
      const rehacer = !soloTabla || firma !== st.firma || !$("#iGraf");
      const pedido = ++st.pedido;  // si llega una respuesta vieja (filtros cambiados rápido) se ignora
      if (rehacer) {
        $("#iOut").innerHTML = '<div id="iGraf"></div><div id="iTab" class="stack"></div>';
        $("#iGraf").innerHTML = '<div class="procesando" role="status"><span class="spinner" aria-hidden="true"></span><span>Calculando tiempos…</span></div>';
      } else {
        $("#iTab").classList.add("cargando");
      }
      try {
        const p = conSeleccion(params());
        p.set("limit", st.limit); p.set("offset", offset); p.set("desc", st.desc);
        if (st.orden) p.set("orden", st.orden);
        const r = await api("/api/reportes/indicador?" + p);
        if (pedido !== st.pedido) return;
        if (!r.kpis.facturas) {
          Object.values(st.charts).forEach((c) => c && c.destroy()); st.charts = {}; st.firma = firma;
          $("#iOut").innerHTML = '<div id="iGraf"></div><div id="iTab"><div class="alert">No hay facturas con esos filtros.</div></div>';
          return;
        }
        if (rehacer) {
          $("#iGraf").innerHTML = `${kpis(r.kpis)}${graficaHtml(r.grafica)}`;
          st.firma = firma;
          await pintarGrafica(r.grafica);
        }
        $("#iTab").innerHTML = tablaHtml(r, offset);
        $("#iTab").classList.remove("cargando");
        if ($("#iLim")) $("#iLim").onchange = () => { st.limit = Number($("#iLim").value); ver(0, true); };
      } catch (e) {
        if (pedido === st.pedido) ($("#iTab") || $("#iOut")).innerHTML = `<div class="alert err">${esc(e.message)}</div>`;
      }
    }
    // Filtros nuevos: todo se recalcula y la gráfica empieza sin selección ni desglose
    const reiniciar = () => { st.dona = null; st.sel = null; st.orden = null; ver(0); };
    $("#iVer").onclick = reiniciar;
    // Cambios en los filtros: la información y las gráficas se actualizan al instante (sin volver a pulsar)
    let espera = null;
    const alCambiar = (demora) => () => {
      if (!st.visible) return;
      clearTimeout(espera);
      espera = setTimeout(() => { if (valido()) reiniciar(); }, demora);
    };
    ["#iCampo", "#iDesde", "#iHasta", "#iVend", "#iZona", "#iEmp"].forEach((x) => $(x).addEventListener("change", alCambiar(150)));
    $("#iEst").addEventListener("change", alCambiar(150));
    $("#iCli").addEventListener("input", alCambiar(450));
    ["#iDesde", "#iHasta"].forEach((x) => $(x).addEventListener("change", () => { $("#iHasta").min = $("#iDesde").value; $("#iDesde").max = $("#iHasta").value; valido(); }));
    $("#iOut").addEventListener("click", (e) => {
      if (e.target.id === "iDonaX" || e.target.id === "iSelX") { quitarSeleccion(); return; }
      const s = e.target.closest("button.sort");
      if (s) { st.desc = st.orden === s.dataset.orden ? !st.desc : false; st.orden = s.dataset.orden; ver(0, true); return; }
      if (e.target.dataset.pag) { ver(Number(e.target.dataset.pag), true); return; }
      if (e.target.dataset.fact) { e.preventDefault(); verFactura(Number(e.target.dataset.fact)); }
    });
    // Excel: las mismas facturas que muestra la tabla (con la selección de la gráfica, si la hay)
    $("#iXls").onclick = () => valido() && TL.download("/api/reportes/indicador.xlsx?" + conSeleccion(params()), "Indicador_Gestion.xlsx").catch((e) => toast(e.message));
    // Imprimir: gráfica, desglose abierto y todas las filas de la tabla (no solo la página)
    $("#iPrint").onclick = async () => {
      if (!valido()) return;
      try {
        const p = conSeleccion(params()); p.set("limit", 5000); p.set("offset", 0);
        if (st.orden) { p.set("orden", st.orden); p.set("desc", st.desc); }
        const r = await api("/api/reportes/indicador?" + p);
        if (!r.kpis.facturas) { toast("No hay facturas con esos filtros"); return; }
        const txt = (c, f) => c.clave === "estatus" ? esc(f.estatus) : FMT[c.tipo](f[c.clave]);
        let graficas = "";
        try {
          if (r.grafica.empresas.length) graficas += `<img class="graf" src="${await IndicadorGrafica.imagenBarras(r.grafica)}" alt="Gráfica Indicador de Gestión">`;
          if (st.dona && IndicadorGrafica.sectoresDe(r.grafica, st.dona.estatus, st.dona.transporte).length) {
            graficas += `<img class="graf dona" src="${await IndicadorGrafica.imagenDona(r.grafica, st.dona.estatus, st.dona.transporte)}" alt="Desglose por empresa emisora">`;
          }
        } catch (_) { graficas = ""; }
        const k = r.kpis;
        const doc = `<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Indicador de Gestión</title><style>
          body{font:10px Arial,sans-serif;margin:12px;color:#000} h1{font-size:15px;margin:0 0 4px} p{margin:2px 0}
          table{border-collapse:collapse;width:100%;margin-top:8px} th,td{border:1px solid #888;padding:2px 4px;vertical-align:top}
          th{background:#d9d9d9} td.num{text-align:right;white-space:nowrap} tfoot td{background:#ddebf7;font-weight:bold}
          .k{display:inline-block;margin-right:14px} @page{size:landscape;margin:10mm}
          .graf{display:block;width:100%;max-width:1000px;margin:10px auto;page-break-inside:avoid} .graf.dona{max-width:780px}
          .graf.dona{page-break-after:always}</style></head><body>
          <h1>Indicador de Gestión</h1><p>${esc(r.filtro)}</p>
          <p>Generado: ${esc(new Date().toLocaleString("es-VE"))} · Tiempos en ${esc(r.jornada || "días hábiles")}, sin feriados</p>
          <p>${[["Facturas", k.facturas], ["Entregadas", k.entregadas], ["Por Entregar", k.por_entregar], ["Preparado", k.preparadas], ["Devoluciones", k.devoluciones], ["Promedio de entrega", k.promedio_entrega || "—"]].map(([t, n]) => `<span class="k"><b>${t}:</b> ${esc(n)}</span>`).join("")}</p>
          ${graficas}
          ${r.seleccion ? `<p><b>Facturas de: ${esc(r.seleccion.texto)}</b></p>` : ""}
          ${r.total > r.items.length ? `<p><b>Se imprimen las primeras ${r.items.length} de ${r.total} facturas.</b></p>` : ""}
          <table><thead><tr>${r.columnas.map((c) => `<th>${esc(c.titulo)}</th>`).join("")}</tr></thead>
          <tbody>${r.items.map((f) => `<tr>${r.columnas.map((c) => `<td class="${NUM.has(c.tipo) ? "num" : ""}">${txt(c, f)}</td>`).join("")}</tr>`).join("")}</tbody>
          <tfoot>${pie(r, "SUMA", "suma")}${pie(r, "PROMEDIO", "promedio")}</tfoot></table></body></html>`;
        const fr = document.createElement("iframe");
        fr.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0";
        document.body.appendChild(fr);
        fr.onload = () => { fr.contentWindow.focus(); fr.contentWindow.print(); setTimeout(() => fr.remove(), 60000); };
        fr.srcdoc = doc;
      } catch (e) { toast(e.message); }
    };
  }

  // ------------------------------------------------------------------ sesión
  $("#btnSalir").onclick = async () => { await TL.cerrarSesion(); location.replace("/"); };
  $("#btnClave").onclick = () => {
    const campos = [{ name: "clave_actual", label: "Clave actual", type: "password", required: true },
      { name: "clave_nueva", label: "Clave nueva (mínimo 6)", type: "password", required: true, min: 6 }];
    formulario("Cambiar mi clave", campos, async (fd) => {
      const r = await api("/api/me/clave", { method: "POST", body: datos(fd, campos) });
      TL.setSession(r.access_token, r.usuario); toast("Clave actualizada");
      const aviso = $("#avisoClave"); if (aviso) aviso.remove();
    });
    // Entrar con huella: equipos donde está activada (quitarla) y activarla en este equipo
    const huella = document.createElement("div");
    huella.className = "stack";
    $("#dlgBody").appendChild(huella);
    Biometria.seccion(huella, TL.user() || me);
  };
  // Clave temporal: aviso con los accesos que le quedan (hasta que la cambie)
  if (me.debe_cambiar_clave) {
    $("#view").insertAdjacentHTML("beforebegin", `<div class="wrap">${TL.avisoClaveTemporal(me)}</div>`);
    $("#avisoClaveBtn").onclick = () => $("#btnClave").click();
  }

  ir(location.hash.slice(1) || "inicio");
})();
