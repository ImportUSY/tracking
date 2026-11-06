"use strict";
(() => {
  const { $, esc, fmtMomento } = TL;
  const COLORES = { "Preparado": "recibida", "Por Entregar": "en_transito", "No Entregado": "no_entregado", "Entregada": "entregada", "Devolución": "devolucion" };

  // Proof of Delivery: la API adjunta al hito de cierre (Entregada / Devolución) una URL firmada y
  // temporal. Se muestra igual que en el panel interno: miniatura bajo el evento; al tocarla se abre completa.
  // Cada hito trae todas sus fotos (h.fotos); las respuestas anteriores solo traían evidencia_url.
  const URL_EVIDENCIA = "/api/publico/evidencias/";
  function fotoHito(h) {
    const fotos = (h.fotos && h.fotos.length) ? h.fotos : (h.evidencia_url ? [{ url: h.evidencia_url }] : []);
    return fotos.filter((x) => typeof x.url === "string" && x.url.startsWith(URL_EVIDENCIA)).map((x) => `<div class="evidencia">
        <a href="${esc(x.url)}" target="_blank" rel="noopener" title="Ver foto completa">
          <img class="thumb" src="${esc(x.url)}" alt="Foto: ${esc(h.estatus)}" loading="lazy">
        </a>
        ${x.capturada_at ? `<div><small class="muted">Foto tomada: ${esc(TL.fmtFechaHora(x.capturada_at))}</small></div>` : ""}
      </div>`).join("");
  }
  // Si el enlace vence o la foto no carga, se oculta sin romper la tarjeta
  document.addEventListener("error", (e) => {
    const img = e.target;
    if (img instanceof HTMLImageElement && img.classList.contains("thumb")) {
      const box = img.closest(".evidencia");
      if (box) box.innerHTML = '<small class="muted">Foto no disponible. Vuelva a consultar para verla.</small>';
    }
  }, true);

  async function consultar(q) {
    const out = $("#out");
    out.innerHTML = '<div class="card muted">Buscando…</div>';
    try {
      const res = await fetch("/api/publico/tracking?q=" + encodeURIComponent(q), { cache: "no-store" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "No se pudo consultar. Intente de nuevo.");
      if (!data.encontrado) { out.innerHTML = `<div class="card">${esc(data.mensaje)}</div>`; return; }
      out.innerHTML = data.resultados.map((r) => `
        <article class="card stack">
          <div class="row between">
            <div><strong class="mono">${esc(r.numero)}</strong><br><small>${esc(r.empresa || "")}</small></div>
            <span class="badge s-${COLORES[r.estatus] || "recibida"}" style="font-size:.95rem">${esc(r.estatus)}</span>
          </div>
          <div class="grid">
            <div><small>Cliente</small><div>${esc(r.cliente || "-")}</div></div>
            <div><small>Pedido</small><div class="mono">${esc(r.pedido || "-")}</div></div>
            <div><small>Tracking</small><div class="mono">${esc(r.tracking || "Aún no asignado")}</div></div>
            <div><small>Destino</small><div>${esc(r.destino || "-")}</div></div>
          </div>
          <div class="hist-tiempos">
            <div>
              <small>Historial</small>
              <ul class="timeline">${r.historial.map((h) => `<li>
                <strong>${esc(h.estatus)}</strong> · ${esc(fmtMomento(h.fecha))}
                ${h.observacion ? `<div class="obs-entrega"><small>Observación de la entrega:</small> ${esc(h.observacion)}</div>` : ""}
                ${fotoHito(h)}</li>`).join("")}</ul>
            </div>
            ${TL.cuadroTiempos(r.tiempos)}
          </div>
        </article>`).join("");
    } catch (e) {
      out.innerHTML = `<div class="alert err">${esc(e.message)}</div>`;
    }
  }

  $("#f").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#q").value.trim();
    history.replaceState(null, "", "?q=" + encodeURIComponent(q));
    consultar(q);
  });
  const inicial = new URLSearchParams(location.search).get("q");
  if (inicial) { $("#q").value = inicial; consultar(inicial); }
})();
