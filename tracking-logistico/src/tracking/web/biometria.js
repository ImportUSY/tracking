/* Ingreso con la huella o el rostro del teléfono (WebAuthn / llaves de acceso).
 * La huella nunca sale del teléfono: este solo firma un reto del servidor después de pedirla.
 * En el teléfono se recuerda qué cuenta tiene la huella activada (para mostrar «Entrar con huella»). */
"use strict";

const Biometria = (() => {
  const KEY = "tl_biometria"; // { username, nombre, credencial_id, id }
  const NO_KEY = "tl_biometria_no"; // usuarios que respondieron «Ahora no» en este teléfono

  const aBytes = (b64) => {
    const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((b64.length + 3) % 4));
    return Uint8Array.from(s, (c) => c.charCodeAt(0));
  };
  const aB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  function leer(k, def) { try { return JSON.parse(localStorage.getItem(k) || "null") ?? def; } catch (_) { return def; } }
  function escribir(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* sin almacenamiento */ } }

  let cacheDisponible = null;
  /** ¿El teléfono tiene huella/rostro/PIN utilizable aquí? (exige https o localhost) */
  async function disponible() {
    if (cacheDisponible !== null) return cacheDisponible;
    cacheDisponible = false;
    try {
      if (window.isSecureContext && window.PublicKeyCredential
          && PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable) {
        const cfg = await fetch("/api/config", { cache: "no-store" }).then((r) => r.json()).catch(() => ({}));
        cacheDisponible = cfg.biometria !== false && await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
      }
    } catch (_) { cacheDisponible = false; }
    return cacheDisponible;
  }

  function guardada() { return leer(KEY, null); }
  function olvidar() { escribir(KEY, null); }
  function rechazada(username) { return leer(NO_KEY, []).includes(String(username).toLowerCase()); }
  function rechazar(username) {
    const l = leer(NO_KEY, []); const u = String(username).toLowerCase();
    if (!l.includes(u)) { l.push(u); escribir(NO_KEY, l); }
  }
  /** Ofrecer activarla: hay huella en el teléfono, no la rechazó y no la tiene ya esta cuenta. */
  async function conviene(u) {
    if (!u || u.debe_cambiar_clave || rechazada(u.username)) return false;
    const g = guardada();
    if (g && g.username === u.username) return false;
    return disponible();
  }

  function mensajeError(e) {
    if (e && e.name === "NotAllowedError") return "Se canceló o no se reconoció la huella. Intente de nuevo.";
    if (e && e.name === "InvalidStateError") {
      return "Este equipo ya tenía la huella activada para su usuario. Quítelo de la lista «Entrar con huella» (en «Mi clave» o «Mis datos») y vuelva a activarla.";
    }
    if (e && e.name === "SecurityError") return "La huella solo funciona abriendo la app por su dirección segura (https).";
    return (e && e.message) || "No se pudo usar la huella";
  }

  /** Activa la huella en este teléfono para el usuario con sesión abierta (pide su clave actual). */
  async function activar(clave, usuario) {
    const { reto_id, opciones: o } = await TL.api("/api/me/biometria/opciones", { method: "POST", body: { clave } });
    const publicKey = Object.assign({}, o, {
      challenge: aBytes(o.challenge),
      user: Object.assign({}, o.user, { id: aBytes(o.user.id) }),
      excludeCredentials: (o.excludeCredentials || []).map((c) => Object.assign({}, c, { id: aBytes(c.id) })),
    });
    const cred = await navigator.credentials.create({ publicKey });
    const r = cred.response;
    const credencial = {
      id: cred.id, rawId: aB64(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: aB64(r.clientDataJSON), attestationObject: aB64(r.attestationObject),
        transports: r.getTransports ? r.getTransports() : [],
      },
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
      authenticatorAttachment: cred.authenticatorAttachment || undefined,
    };
    const out = await TL.api("/api/me/biometria", { method: "POST", body: { reto_id, credencial } });
    escribir(KEY, { username: usuario.username, nombre: usuario.nombre, credencial_id: aB64(cred.rawId), id: out.id });
    return out;
  }

  /** Entra con la huella activada en este teléfono. Devuelve la sesión (igual que /api/auth/login). */
  async function entrar() {
    const g = guardada();
    if (!g) throw new Error("Este teléfono no tiene la huella activada");
    const { reto_id, opciones: o } = await TL.api("/api/auth/biometria/opciones", { method: "POST", onUnauthorized: false });
    const publicKey = Object.assign({}, o, {
      challenge: aBytes(o.challenge),
      allowCredentials: [{ type: "public-key", id: aBytes(g.credencial_id) }],
    });
    const cred = await navigator.credentials.get({ publicKey });
    const r = cred.response;
    const credencial = {
      id: cred.id, rawId: aB64(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: aB64(r.clientDataJSON), authenticatorData: aB64(r.authenticatorData),
        signature: aB64(r.signature), userHandle: r.userHandle ? aB64(r.userHandle) : undefined,
      },
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
      authenticatorAttachment: cred.authenticatorAttachment || undefined,
    };
    try {
      return await TL.api("/api/auth/biometria/ingreso", { method: "POST", body: { reto_id, credencial }, onUnauthorized: false });
    } catch (e) {
      // La oficina le restableció la clave o la quitó desde otro equipo: ya no sirve en este teléfono
      if (e.status === 401 && /ya no está activada/.test(e.message)) olvidar();
      throw e;
    }
  }

  /** Sección «Entrar con huella» de «Mi clave» (panel) y «Mis datos» (conductor): equipos donde está
   *  activada (cada uno con «Quitar», que la borra del servidor) y «Activar huella en este equipo». */
  async function seccion(caja, usuario) {
    const esc = TL.esc;
    let lista;
    try { lista = await TL.api("/api/me/biometria"); } catch (_) { caja.innerHTML = ""; return; }
    const puede = await disponible();
    if (!lista.length && !puede) { caja.innerHTML = ""; return; }
    const g = guardada();
    const deAqui = (c) => !!(g && g.username === usuario.username && g.id === c.id);
    const filas = lista.map((c) => `<tr><td>${esc(c.dispositivo || "Dispositivo")}${deAqui(c) ? " <strong>(este equipo)</strong>" : ""}<br>
        <small class="muted">Activada ${esc(TL.fmtFechaHora(c.created_at))} · último uso: ${esc(TL.fmtFechaHora(c.ultimo_uso_at) || "nunca")}</small></td>
        <td class="num"><button type="button" class="small danger" data-quitar="${c.id}">Quitar</button></td></tr>`).join("");
    caja.innerHTML = `<hr><h3 style="margin:0">Entrar con huella</h3>
      ${lista.length ? `<table><tbody>${filas}</tbody></table>` : '<p class="muted" style="margin:0">No la tiene activada en ningún equipo.</p>'}
      ${puede && !lista.some(deAqui) ? `<p class="muted" style="margin:0">Entre sin escribir la clave, con la huella o el rostro de este equipo. La huella no sale del equipo.</p>
        <div><label for="bioClave">Su clave actual (para activarla aquí)</label><input id="bioClave" type="password" autocomplete="current-password"></div>
        <div><button type="button" class="primary" data-activar>Activar huella en este equipo</button></div>` : ""}
      <div class="alert hidden" data-msg role="status"></div>`;
    const aviso = (clase, texto) => { const m = caja.querySelector("[data-msg]"); m.className = "alert " + clase; m.textContent = texto; };
    caja.querySelectorAll("[data-quitar]").forEach((b) => {
      b.onclick = async () => {
        const c = lista.find((x) => String(x.id) === b.dataset.quitar);
        if (!confirm(`¿Quitar la huella de «${c.dispositivo || "Dispositivo"}»? En ese equipo volverá a entrar con su clave.`)) return;
        try {
          await TL.api(`/api/me/biometria/${c.id}`, { method: "DELETE" });
          if (deAqui(c)) olvidar();
          await seccion(caja, usuario);
          TL.toast("Huella quitada");
        } catch (e) { aviso("err", navigator.onLine ? mensajeError(e) : "Necesita señal para quitar la huella"); }
      };
    });
    const btn = caja.querySelector("[data-activar]");
    if (btn) {
      btn.onclick = async () => {
        const clave = caja.querySelector("#bioClave").value;
        if (!clave) { aviso("err", "Escriba su clave actual"); return; }
        btn.disabled = true;
        try {
          await activar(clave, usuario);
          await seccion(caja, usuario);
          TL.toast("Huella activada en este equipo");
        } catch (e) {
          aviso("err", navigator.onLine ? mensajeError(e) : "Necesita señal para activar la huella");
          btn.disabled = false;
        }
      };
    }
  }

  return { disponible, guardada, olvidar, rechazada, rechazar, conviene, activar, entrar, seccion, mensajeError };
})();
