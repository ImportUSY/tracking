/* Lector mínimo de .xlsx en el navegador (sin librerías externas).
 *
 * Por qué existe: en Vercel cada petición admite como máximo 4,5 MB y la hoja REMESA real pesa ~9 MB
 * (tiene siete hojas, fórmulas y estilos). Aquí se descomprime el libro en el propio navegador, se
 * extraen solo los valores de la hoja elegida y se envían al servidor (≈0,3 MB comprimidos).
 *
 * Soporta: cadenas compartidas, texto en línea, resultados de fórmulas, números, booleanos y errores
 * (#N/A...). Las fechas llegan como número de serie de Excel y el servidor las convierte.
 */
"use strict";

const XlsxLite = (() => {
  const SIG_EOCD = 0x06054b50, SIG_CEN = 0x02014b50, SIG_LOC = 0x04034b50;

  async function readZipIndex(file) {
    const tailSize = Math.min(file.size, 66000);
    const tail = new DataView(await file.slice(file.size - tailSize).arrayBuffer());
    let eocd = -1;
    for (let i = tail.byteLength - 22; i >= 0; i--) {
      if (tail.getUint32(i, true) === SIG_EOCD) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error("El archivo no es un Excel .xlsx válido");
    const count = tail.getUint16(eocd + 10, true);
    const cdSize = tail.getUint32(eocd + 12, true);
    const cdOffset = tail.getUint32(eocd + 16, true);
    const cd = new DataView(await file.slice(cdOffset, cdOffset + cdSize).arrayBuffer());
    const dec = new TextDecoder();
    const entries = new Map();
    let p = 0;
    for (let n = 0; n < count; n++) {
      if (cd.getUint32(p, true) !== SIG_CEN) throw new Error("Excel dañado (directorio ZIP)");
      const method = cd.getUint16(p + 10, true);
      const compSize = cd.getUint32(p + 20, true);
      const nameLen = cd.getUint16(p + 28, true);
      const extraLen = cd.getUint16(p + 30, true);
      const commentLen = cd.getUint16(p + 32, true);
      const localOffset = cd.getUint32(p + 42, true);
      const name = dec.decode(new Uint8Array(cd.buffer, cd.byteOffset + p + 46, nameLen));
      entries.set(name, { method, compSize, localOffset });
      p += 46 + nameLen + extraLen + commentLen;
    }
    return entries;
  }

  async function readEntry(file, entries, name) {
    const e = entries.get(name);
    if (!e) return null;
    const head = new DataView(await file.slice(e.localOffset, e.localOffset + 30).arrayBuffer());
    if (head.getUint32(0, true) !== SIG_LOC) throw new Error("Excel dañado (cabecera ZIP)");
    const start = e.localOffset + 30 + head.getUint16(26, true) + head.getUint16(28, true);
    const blob = file.slice(start, start + e.compSize);
    if (e.method === 0) return new TextDecoder().decode(await blob.arrayBuffer());
    if (e.method !== 8) throw new Error("Compresión no soportada en el Excel");
    if (typeof DecompressionStream === "undefined") throw new Error("Este navegador no puede leer Excel. Actualícelo.");
    const stream = blob.stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return await new Response(stream).text();
  }

  const ENT = { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" };
  function unescapeXml(s) {
    if (s.indexOf("&") < 0) return s;
    return s.replace(/&(#x[0-9a-fA-F]+|#\d+|lt|gt|amp|quot|apos);/g, (_, c) => {
      if (c[0] === "#") return String.fromCodePoint(c[1] === "x" ? parseInt(c.slice(2), 16) : parseInt(c.slice(1), 10));
      return ENT[c];
    });
  }
  function attrs(s) {
    const o = {};
    s.replace(/([\w:]+)="([^"]*)"/g, (_, k, v) => { o[k] = unescapeXml(v); return ""; });
    return o;
  }
  function textOf(xml) {
    let out = "";
    xml.replace(/<rPh\b[\s\S]*?<\/rPh>/g, "").replace(/<t\b[^>]*>([\s\S]*?)<\/t>/g, (_, t) => { out += t; return ""; });
    return unescapeXml(out);
  }
  function colIndex(letters) {
    let n = 0;
    for (let i = 0; i < letters.length; i++) n = n * 26 + (letters.charCodeAt(i) - 64);
    return n - 1;
  }
  function norm(v) {
    return String(v ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9#%]+/g, " ").trim();
  }

  async function open(file) {
    const entries = await readZipIndex(file);
    const wbXml = await readEntry(file, entries, "xl/workbook.xml");
    if (!wbXml) throw new Error("El archivo no es un libro de Excel (.xlsx)");
    const relsXml = (await readEntry(file, entries, "xl/_rels/workbook.xml.rels")) || "";
    const rels = {};
    relsXml.replace(/<Relationship\b([^>]*?)\/?>/g, (_, a) => { const r = attrs(a); rels[r.Id] = r.Target; return ""; });
    const sheets = [];
    wbXml.replace(/<sheet\b([^>]*?)\/?>/g, (_, a) => {
      const r = attrs(a);
      let target = rels[r["r:id"]] || "";
      target = target.startsWith("/") ? target.slice(1) : "xl/" + target.replace(/^\.\//, "");
      sheets.push({ name: r.name, path: target });
      return "";
    });
    let shared = null;
    async function sharedStrings() {
      if (shared) return shared;
      const xml = (await readEntry(file, entries, "xl/sharedStrings.xml")) || "";
      shared = [];
      xml.replace(/<si>([\s\S]*?)<\/si>/g, (_, si) => { shared.push(textOf(si)); return ""; });
      return shared;
    }

    /** Devuelve filas densas (la fila 1 del Excel es el índice 0); filas vacías = []. */
    async function readSheet(name, { maxCols = 30, maxRows = Infinity } = {}) {
      const sh = sheets.find((s) => s.name === name);
      if (!sh) throw new Error(`La hoja «${name}» no existe`);
      const xml = await readEntry(file, entries, sh.path);
      if (xml === null) throw new Error(`No se pudo leer la hoja «${name}»`);
      const ss = await sharedStrings();
      const rows = [];
      const rowRe = /<row\b([^>]*?)(?:\/>|>([\s\S]*?)<\/row>)/g;
      const cellRe = /<c\b([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g;
      let m, next = 1;
      while ((m = rowRe.exec(xml)) !== null) {
        const ra = /\br="(\d+)"/.exec(m[1]);
        const rnum = ra ? parseInt(ra[1], 10) : next;
        next = rnum + 1;
        if (rnum > maxRows) break;
        const vals = [];
        if (m[2]) {
          let c, col = 0;
          cellRe.lastIndex = 0;
          while ((c = cellRe.exec(m[2])) !== null) {
            const ref = /\br="([A-Z]+)\d+"/.exec(c[1]);
            const ci = ref ? colIndex(ref[1]) : col;
            col = ci + 1;
            if (ci >= maxCols || !c[2]) continue;
            const t = (/\bt="(\w+)"/.exec(c[1]) || [])[1] || "n";
            let v = null;
            if (t === "inlineStr") v = textOf(c[2]);
            else {
              const vm = /<v\b[^>]*>([\s\S]*?)<\/v>/.exec(c[2]);
              if (vm) {
                const raw = unescapeXml(vm[1]);
                if (t === "s") v = ss[parseInt(raw, 10)] ?? null;
                else if (t === "b") v = raw === "1";
                else if (t === "str" || t === "e") v = raw;
                else { const num = Number(raw); v = Number.isFinite(num) ? num : raw; }
              }
            }
            if (v === null || v === "") continue;
            while (vals.length < ci) vals.push(null);
            vals[ci] = v;
          }
        }
        while (rows.length < rnum - 1) rows.push([]);
        rows.push(vals);
      }
      return rows;
    }

    /** Elige la hoja de remesas: nombre con "remesa" primero, y que tenga Chofer + encabezado Factura. */
    async function detectRemesaSheet() {
      const orden = [...sheets].sort((a, b) => (norm(b.name).includes("remesa") ? 1 : 0) - (norm(a.name).includes("remesa") ? 1 : 0));
      for (const s of orden) {
        const rows = await readSheet(s.name, { maxRows: 400 });
        let chofer = false, factura = false;
        for (const r of rows) {
          const first = r.find((v) => v !== null && v !== "");
          if (first !== undefined && norm(first).startsWith("chofer") && r.filter((v) => v !== null).length > 1) chofer = true;
          if (r.some((v) => typeof v === "string" && norm(v) === "factura")) factura = true;
        }
        if (chofer && factura) return s.name;
      }
      return null;
    }

    async function sha256() {
      if (!(crypto && crypto.subtle)) return null;
      const buf = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
    }

    return { sheetNames: sheets.map((s) => s.name), readSheet, detectRemesaSheet, sha256 };
  }

  /** Comprime un objeto JSON con gzip si el navegador lo permite (el servidor detecta el formato). */
  async function gzipJson(obj) {
    const json = JSON.stringify(obj);
    if (typeof CompressionStream === "undefined") return new Blob([json], { type: "application/json" });
    const stream = new Blob([json]).stream().pipeThrough(new CompressionStream("gzip"));
    return new Blob([await new Response(stream).arrayBuffer()], { type: "application/json" });
  }

  return { open, gzipJson, norm };
})();
