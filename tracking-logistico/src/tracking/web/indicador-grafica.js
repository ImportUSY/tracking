/* Gráficas del «Indicador de Gestión».
 *
 *  1) Barras agrupadas (Chart.js 4, servido desde /static/vendor: sin CDN, cumple la CSP), con el estilo de
 *     un gráfico de Excel: título arriba, empresas de transporte en el eje X, leyenda abajo y, DENTRO de
 *     cada barra y en vertical, la cantidad de facturas en negro (abajo) y el porcentaje en amarillo (encima).
 *  2) Desglose: dona 3D (dibujo propio en canvas: relieve, brillo, sectores separados y números blancos
 *     grandes) con las empresas emisoras del estatus elegido. Clic en una barra → ese estatus en esa
 *     empresa de transporte; clic en la leyenda → ese estatus en todas.
 *  3) imagenBarras / imagenDona: las mismas gráficas como PNG para imprimir.
 *  Los datos llegan ya agregados desde la API (``grafica`` en /api/reportes/indicador).
 */
"use strict";
const IndicadorGrafica = (() => {
  const URL_CHART = "/static/vendor/chartjs-4.4.7/chart.umd.js";
  // Colores del gráfico de Excel: azul = Preparado, naranja = Por Entregar, gris = Entregado
  const COLOR = { recibida: "#4472C4", en_transito: "#ED7D31", entregada: "#A5A5A5", devolucion: "#C0392B" };
  // Dona: rojo coral, naranja, verde menta… como la imagen de referencia
  const PALETA = ["#EC6168", "#F5A623", "#8ED49A", "#5DA9E9", "#B28DFF", "#FFD166", "#06D6A0", "#F78FB3",
    "#4ECDC4", "#FF8C61", "#9AA5B1", "#C3E88D"];
  const FUENTE = "Calibri, Carlito, 'Segoe UI', system-ui, -apple-system, Roboto, Arial, sans-serif";
  const FUENTE_NUM = "'Arial Rounded MT Bold', 'Nunito', 'Segoe UI', system-ui, Arial, sans-serif";
  const AMARILLO = "#FFE800";
  const fmtPct = (p) => `${Number(p).toLocaleString("es-VE", { maximumFractionDigits: 1 })} %`;
  const tituloEstatus = (g, clave) => (g.estatus.find((e) => e.clave === clave) || {}).titulo || clave;

  let cargando = null;
  function cargar() {
    if (window.Chart) return Promise.resolve(window.Chart);
    if (!cargando) {
      cargando = new Promise((ok, mal) => {
        const s = document.createElement("script");
        s.src = URL_CHART;
        s.onload = () => ok(window.Chart);
        s.onerror = () => { cargando = null; mal(new Error("No se pudo cargar el componente de gráficas")); };
        document.head.appendChild(s);
      });
    }
    return cargando;
  }

  // ======================================================================= 1) BARRAS
  const fondo = {  // fondo blanco: se lee igual en modo oscuro y al imprimir
    id: "tlFondo",
    beforeDraw(chart) {
      const { ctx, width, height } = chart;
      ctx.save(); ctx.globalCompositeOperation = "destination-over";
      ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, width, height); ctx.restore();
    },
  };

  function texto(ctx, t, x, y, color, sombra) {
    if (sombra) { ctx.shadowColor = "rgba(0,0,0,0.55)"; ctx.shadowBlur = 2; }
    ctx.fillStyle = color; ctx.fillText(t, x, y);
    ctx.shadowColor = "transparent"; ctx.shadowBlur = 0;
  }

  // Dentro de cada barra, en vertical: cantidad (negro) abajo y porcentaje (amarillo) encima, como la imagen 1
  const etiquetasBarras = {
    id: "tlEtiquetasBarras",
    afterDatasetsDraw(chart) {
      const { ctx } = chart;
      const horizontal = chart.options.indexAxis === "y";
      chart.data.datasets.forEach((ds, di) => {
        const meta = chart.getDatasetMeta(di);
        if (meta.hidden) return;
        meta.data.forEach((barra, i) => {
          const n = ds.data[i];
          if (!n) return;
          const cant = String(n), pct = fmtPct(ds.pcts[i]);
          const { x, y, base, width, height } = barra.getProps(["x", "y", "base", "width", "height"], true);
          ctx.save();
          const tam = horizontal ? 12 : Math.max(10, Math.min(14, Math.round(width * 0.42)));
          ctx.font = `bold ${tam}px ${FUENTE}`;
          ctx.textBaseline = "middle"; ctx.textAlign = "left";
          const wC = ctx.measureText(cant).width, wP = ctx.measureText(pct).width, sep = 9;
          const necesario = 8 + wC + sep + wP + 6;
          if (horizontal) {  // teléfono: de izquierda a derecha dentro de la barra (o a su derecha si no cabe)
            const largo = x - base;
            const x0 = largo >= necesario ? base + 8 : x + 6;
            const fuera = largo < necesario;
            texto(ctx, cant, x0, y, "#000", false);
            texto(ctx, pct, x0 + wC + sep, y, fuera ? "#B8860B" : AMARILLO, !fuera);
            void height;
          } else {
            const alto = base - y;
            ctx.translate(x, alto >= necesario ? base - 8 : y - 6);  // dentro de la barra, o encima si es baja
            ctx.rotate(-Math.PI / 2);
            const fuera = alto < necesario;
            texto(ctx, cant, 0, 0, "#000", false);
            texto(ctx, pct, wC + sep, 0, fuera ? "#B8860B" : AMARILLO, !fuera);
          }
          ctx.restore();
        });
      });
    },
  };

  function configBarras(g, alElegir, animar, horizontal = false) {
    // Devolución solo aparece si hay devoluciones en el filtro (la imagen de referencia muestra 3 series)
    const series = g.estatus.filter((e) => e.clave !== "devolucion" || g.empresas.some((x) => x.cantidades.devolucion));
    const labels = g.empresas.map((e) => e.nombre);
    const max = Math.max(1, ...g.empresas.flatMap((e) => series.map((s) => e.cantidades[s.clave])));
    const gris = "#595959";
    const cat = { ticks: { color: gris, font: { size: 13, family: FUENTE }, autoSkip: false, maxRotation: 30 },
      grid: { display: false }, border: { color: "#d9d9d9" } };
    const val = { beginAtZero: true, suggestedMax: Math.ceil(max * 1.15), ticks: { precision: 0, color: gris, font: { size: 12, family: FUENTE } },
      grid: { color: "#d9d9d9" }, border: { display: false } };
    return {
      type: "bar",
      data: {
        labels,
        datasets: series.map((e) => ({
          label: e.titulo === "Entregada" ? "Entregado" : e.titulo,
          clave: e.clave,
          data: g.empresas.map((x) => x.cantidades[e.clave]),
          pcts: g.empresas.map((x) => x.porcentajes[e.clave]),
          devs: g.empresas.map((x) => (e.clave === "entregada" ? x.devoluciones || 0 : 0)),
          backgroundColor: COLOR[e.clave],
          hoverBackgroundColor: COLOR[e.clave],
          borderWidth: 0,
          categoryPercentage: 0.72,
          barPercentage: 0.82,
          maxBarThickness: horizontal ? 24 : 64,
        })),
      },
      options: {
        indexAxis: horizontal ? "y" : "x",
        responsive: true,
        maintainAspectRatio: false,
        animation: animar ? { duration: 500 } : false,
        layout: { padding: { top: 6, right: horizontal ? 80 : 16, left: 6, bottom: 4 } },
        plugins: {
          title: { display: true, text: "Indicador de Gestión", color: gris, font: { size: 20, weight: "normal", family: FUENTE }, padding: { top: 4, bottom: 14 } },
          legend: {
            position: "bottom",
            labels: { color: gris, font: { size: 13, family: FUENTE }, boxWidth: 11, boxHeight: 11, padding: 16 },
            onClick: (_e, item) => alElegir && alElegir(series[item.datasetIndex].clave, null),
            onHover: (e) => { e.native.target.style.cursor = "pointer"; },
          },
          tooltip: {
            callbacks: {
              label: (c) => ` ${c.dataset.label}: ${c.raw} factura(s) · ${fmtPct(c.dataset.pcts[c.dataIndex])}`
                + (c.dataset.devs[c.dataIndex] ? ` (incluye ${c.dataset.devs[c.dataIndex]} devolución(es))` : ""),
              footer: (items) => items.length ? `Total ${labels[items[0].dataIndex]}: ${g.empresas[items[0].dataIndex].total} · clic para ver el desglose` : "",
            },
          },
        },
        scales: horizontal ? { y: cat, x: val } : { x: cat, y: val },
        onClick: (_evt, els) => {
          if (els.length && alElegir) alElegir(series[els[0].datasetIndex].clave, labels[els[0].index]);
        },
        onHover: (evt, els) => { evt.native.target.style.cursor = els.length ? "pointer" : "default"; },
      },
      plugins: [fondo, etiquetasBarras],
    };
  }

  const esAngosto = (canvas) => (canvas.parentElement?.clientWidth || window.innerWidth) < 600;
  const altoHorizontal = (g) => Math.max(320, g.empresas.length * g.estatus.length * 28 + 140);
  async function barras(canvas, g, alElegir) {
    const Chart = await cargar();
    const horizontal = esAngosto(canvas);
    if (horizontal) canvas.parentElement.style.height = `${altoHorizontal(g)}px`;
    return new Chart(canvas, configBarras(g, alElegir, true, horizontal));
  }

  // ======================================================================= 2) DONA 3D
  function sectoresDe(g, estatus, transporte) {
    return (g.desglose[estatus] || {})[transporte || "*"] || [];
  }

  function tono(hex, f) {  // f < 1 oscurece, f > 1 aclara
    const n = parseInt(hex.slice(1), 16);
    const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => (f <= 1 ? v * f : v + (255 - v) * (f - 1)));
    return `rgb(${c.map((v) => Math.round(Math.max(0, Math.min(255, v)))).join(",")})`;
  }
  const TAU = Math.PI * 2;
  const norm = (a) => ((a % TAU) + TAU) % TAU;

  /** Dibuja la dona 3D en ``ctx`` (coordenadas CSS). ``estado``: {progreso 0..1, activo índice|-1}. */
  function pintarDona(ctx, W, H, d, estado) {
    const { sectores, titulo, subtitulo } = d;
    const total = sectores.reduce((s, x) => s + x.facturas, 0);
    // Fondo celeste con degradado (como la imagen de referencia)
    const fondoG = ctx.createLinearGradient(0, 0, 0, H);
    fondoG.addColorStop(0, "#A6DBFF"); fondoG.addColorStop(1, "#7CC4F5");
    ctx.fillStyle = fondoG; ctx.fillRect(0, 0, W, H);

    // Títulos
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillStyle = "#0E2A47"; ctx.font = `bold ${W < 520 ? 15 : 18}px ${FUENTE}`;
    ctx.fillText(titulo, W / 2, 12);
    ctx.fillStyle = "#1F4E79"; ctx.font = `13px ${FUENTE}`;
    ctx.fillText(subtitulo, W / 2, W < 520 ? 32 : 36);

    // Geometría: la leyenda va a la derecha (o abajo en pantallas angostas)
    const angosto = W < 620;
    const leyendaAlto = angosto ? sectores.length * 22 + 14 : 0;
    const areaW = angosto ? W : W * 0.62, areaTop = 64, areaH = H - areaTop - leyendaAlto - 10;
    const k = 0.58;                                   // inclinación (achatamiento vertical)
    const R = Math.max(40, Math.min(areaW * 0.40, areaH / (2 * k + 0.55)));
    const r = R * 0.5, prof = R * 0.26, sep = R * 0.07;
    const cx = areaW / 2 + (angosto ? 0 : 10), cy = areaTop + areaH / 2 - prof / 2;

    // Sombra en el «piso»
    ctx.save();
    ctx.fillStyle = "rgba(15, 50, 90, 0.28)"; ctx.filter = "blur(10px)";
    ctx.beginPath(); ctx.ellipse(cx, cy + prof + R * 0.12, R * 1.02, R * k * 1.0, 0, 0, TAU); ctx.fill();
    ctx.restore();
    ctx.filter = "none";

    // Ángulos (empieza arriba y va en sentido horario), con animación de barrido
    const p = estado.progreso;
    let a0 = -Math.PI / 2 - 0.35;
    const segs = sectores.map((s, i) => {
      const barrido = total ? (s.facturas / total) * TAU * p : 0;
      const seg = { i, s, a: a0, b: a0 + barrido, color: PALETA[i % PALETA.length] };
      a0 += barrido;
      const m = (seg.a + seg.b) / 2;
      const e = sectores.length > 1 ? (i === estado.activo ? sep * 2.2 : sep) : (i === estado.activo ? sep : 0);
      seg.dx = Math.cos(m) * e; seg.dy = Math.sin(m) * e * k; seg.m = m;
      return seg;
    });
    const pt = (s, rad, ang, z = 0) => [cx + s.dx + rad * Math.cos(ang), cy + s.dy + rad * k * Math.sin(ang) + z];

    function pared(s, rad, color, f = 1) {  // f < 1: pared interna, más oscura
      const ox = cx + s.dx, oy = cy + s.dy;
      const gr = ctx.createLinearGradient(ox - rad, 0, ox + rad, 0);
      gr.addColorStop(0, tono(color, 0.55 * f)); gr.addColorStop(0.45, tono(color, 0.86 * f)); gr.addColorStop(1, tono(color, 0.6 * f));
      ctx.fillStyle = gr;
      ctx.beginPath();
      ctx.ellipse(ox, oy, rad, rad * k, 0, s.a, s.b);
      const [bx, by] = pt(s, rad, s.b, prof); ctx.lineTo(bx, by);
      ctx.ellipse(ox, oy + prof, rad, rad * k, 0, s.b, s.a, true);
      ctx.closePath(); ctx.fill();
    }
    function tapa(s, ang, color) {  // corte radial (visible por la separación entre sectores)
      const [x1, y1] = pt(s, R, ang), [x2, y2] = pt(s, r, ang), [x3, y3] = pt(s, r, ang, prof), [x4, y4] = pt(s, R, ang, prof);
      ctx.fillStyle = tono(color, 0.7);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.lineTo(x3, y3); ctx.lineTo(x4, y4); ctx.closePath(); ctx.fill();
    }
    function cara(s, color) {
      const ox = cx + s.dx, oy = cy + s.dy;
      const gr = ctx.createRadialGradient(ox - R * 0.35, oy - R * k * 0.6, R * 0.1, ox, oy, R * 1.15);
      gr.addColorStop(0, tono(color, 1.28)); gr.addColorStop(0.55, color); gr.addColorStop(1, tono(color, 0.82));
      ctx.fillStyle = gr;
      ctx.beginPath();
      ctx.ellipse(ox, oy, R, R * k, 0, s.a, s.b);
      ctx.ellipse(ox, oy, r, r * k, 0, s.b, s.a, true);
      ctx.closePath(); ctx.fill();
      // Brillo (aspecto «plástico» de la referencia)
      ctx.save(); ctx.clip();
      const br = ctx.createLinearGradient(0, oy - R * k, 0, oy + R * k * 0.2);
      br.addColorStop(0, "rgba(255,255,255,0.42)"); br.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = br; ctx.fillRect(ox - R, oy - R * k, 2 * R, R * k * 1.2);
      ctx.restore();
      ctx.strokeStyle = "rgba(255,255,255,0.5)"; ctx.lineWidth = 1; ctx.stroke();
    }

    // Orden de pintado: primero lo del fondo (sectores de atrás), luego lo de adelante
    const orden = [...segs].filter((s) => s.b > s.a).sort((u, v) => Math.sin(u.m) - Math.sin(v.m));
    for (const s of orden) {
      pared(s, r, s.color, 0.78);          // pared interna (se ve por el hueco, al fondo)
      tapa(s, s.a, s.color); tapa(s, s.b, s.color);
      pared(s, R, s.color);                // pared externa (frente)
      cara(s, s.color);                    // cara superior
    }
    // Números blancos grandes con sombra, como la referencia; el porcentaje debajo
    if (p >= 0.999) {
      for (const s of segs) {
        const frac = total ? s.s.facturas / total : 0;
        if (frac < 0.035) continue;
        const [lx, ly] = pt(s, (R + r) / 2, s.m);
        const grande = Math.max(16, Math.min(R * 0.3, 44)) * (frac < 0.1 ? 0.75 : 1);
        ctx.save();
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.font = `900 ${grande}px ${FUENTE_NUM}`;
        ctx.shadowColor = "rgba(0,0,0,0.35)"; ctx.shadowBlur = 4; ctx.shadowOffsetX = 1.5; ctx.shadowOffsetY = 3;
        ctx.lineWidth = Math.max(2, grande / 10); ctx.strokeStyle = "rgba(210,210,210,0.9)";
        ctx.strokeText(String(s.s.facturas), lx, ly - grande * 0.18);
        ctx.fillStyle = "#ffffff"; ctx.fillText(String(s.s.facturas), lx, ly - grande * 0.18);
        ctx.font = `bold ${Math.round(grande * 0.38)}px ${FUENTE}`;
        ctx.shadowBlur = 2; ctx.shadowOffsetY = 1;
        ctx.fillText(fmtPct(s.s.porcentaje), lx, ly + grande * 0.5);
        ctx.restore();
      }
    }

    // Leyenda: empresa emisora, cantidad y porcentaje
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    const lx = angosto ? 20 : W * 0.64, ly0 = angosto ? H - leyendaAlto + 6 : Math.max(areaTop + 10, H / 2 - sectores.length * 13);
    sectores.forEach((s, i) => {
      const y = ly0 + i * (angosto ? 22 : 26);
      ctx.fillStyle = PALETA[i % PALETA.length];
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(lx, y - 7, 14, 14, 3); else ctx.rect(lx, y - 7, 14, 14);
      ctx.fill();
      ctx.fillStyle = i === estado.activo ? "#000" : "#0E2A47";
      ctx.font = `${i === estado.activo ? "bold " : ""}13px ${FUENTE}`;
      ctx.fillText(`${s.empresa}: ${s.facturas} (${fmtPct(s.porcentaje)})`, lx + 22, y);
    });
    // Total
    ctx.fillStyle = "#0E2A47"; ctx.font = `bold 13px ${FUENTE}`;
    ctx.fillText(`Total: ${total} factura(s)`, lx, angosto ? H - 8 : ly0 + sectores.length * 26 + 8);
    return { segs, cx, cy, R, r, k };
  }

  function sectorEn(geo, mx, my) {
    if (!geo) return -1;
    for (const s of geo.segs) {
      const x = mx - geo.cx - s.dx, y = (my - geo.cy - s.dy) / geo.k;
      const rad = Math.hypot(x, y);
      if (rad < geo.r || rad > geo.R) continue;
      const ang = norm(Math.atan2(y, x)), a = norm(s.a), largo = s.b - s.a;
      if (norm(ang - a) <= largo) return s.i;
    }
    return -1;
  }

  function datosDona(g, estatus, transporte) {
    return {
      sectores: sectoresDe(g, estatus, transporte),
      titulo: `${tituloEstatus(g, estatus)} · ${transporte || "todas las empresas de transporte"}`,
      subtitulo: "Facturas por empresa emisora",
    };
  }

  /** Dona 3D interactiva: resalta (separa) el sector bajo el cursor y muestra su detalle. */
  function dona(canvas, g, estatus, transporte) {
    const d = datosDona(g, estatus, transporte);
    const ctx = canvas.getContext("2d");
    const estado = { progreso: 0, activo: -1 };
    let geo = null, W = 0, H = 0, raf = 0, vivo = true;
    function medir() {
      const caja = canvas.parentElement.getBoundingClientRect();
      W = Math.max(280, caja.width); H = Math.max(260, caja.height);
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      canvas.style.width = `${W}px`; canvas.style.height = `${H}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    const pintar = () => { if (vivo) geo = pintarDona(ctx, W, H, d, estado); };
    medir();
    const t0 = performance.now();
    const animar = (t) => {
      const x = Math.min(1, (t - t0) / 750);
      estado.progreso = 1 - Math.pow(1 - x, 3);
      pintar();
      if (x < 1 && vivo) raf = requestAnimationFrame(animar);
    };
    raf = requestAnimationFrame(animar);
    const mover = (e) => {
      const b = canvas.getBoundingClientRect();
      const i = sectorEn(geo, e.clientX - b.left, e.clientY - b.top);
      canvas.style.cursor = i >= 0 ? "pointer" : "default";
      canvas.title = i >= 0 ? `${d.sectores[i].empresa}: ${d.sectores[i].facturas} factura(s) · ${fmtPct(d.sectores[i].porcentaje)}` : "";
      if (i !== estado.activo && estado.progreso >= 1) { estado.activo = i; pintar(); }
    };
    const salir = () => { if (estado.activo !== -1) { estado.activo = -1; pintar(); } };
    const ro = window.ResizeObserver ? new ResizeObserver(() => { medir(); pintar(); }) : null;
    canvas.addEventListener("mousemove", mover);
    canvas.addEventListener("click", mover);  // en el teléfono, tocar un sector lo resalta
    canvas.addEventListener("mouseleave", salir);
    if (ro) ro.observe(canvas.parentElement);
    return {
      destroy() {
        vivo = false; cancelAnimationFrame(raf);
        canvas.removeEventListener("mousemove", mover); canvas.removeEventListener("click", mover);
        canvas.removeEventListener("mouseleave", salir);
        if (ro) ro.disconnect();
      },
    };
  }

  // ======================================================================= 3) IMÁGENES PARA IMPRIMIR
  async function imagenBarras(g) {
    const Chart = await cargar();
    const ancho = Math.max(1000, g.empresas.length * 160), alto = 480;
    const caja = document.createElement("div");
    caja.style.cssText = `position:fixed;left:-10000px;top:0;width:${ancho}px;height:${alto}px`;
    const canvas = document.createElement("canvas");
    caja.appendChild(canvas); document.body.appendChild(caja);
    const config = configBarras(g, null, false);
    config.options.devicePixelRatio = 2;
    const chart = new Chart(canvas, config);
    const url = chart.toBase64Image("image/png", 1);
    chart.destroy(); caja.remove();
    return url;
  }
  async function imagenDona(g, estatus, transporte) {
    const W = 1000, H = 460, dpr = 2;
    const canvas = document.createElement("canvas");
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    pintarDona(ctx, W, H, datosDona(g, estatus, transporte), { progreso: 1, activo: -1 });
    return canvas.toDataURL("image/png");
  }

  return { cargar, barras, dona, sectoresDe, imagenBarras, imagenDona, esAngosto, COLOR };
})();
