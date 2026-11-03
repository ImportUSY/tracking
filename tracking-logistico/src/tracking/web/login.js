"use strict";
(() => {
  const { $, api, setSession, user, token } = TL;
  TL.registerSW();

  function destino(u) { return u.rol === "conductor" ? "/conductor" : "/panel"; }

  // Sesión vigente: ir directo (salvo que ya deba cambiar la clave temporal)
  const u0 = user();
  if (token() && u0 && !u0.cambio_clave_obligatorio) {
    location.replace(destino(u0));
    return;
  }

  let claveActual = "";
  let usuarioActual = null;

  // Huella activada en este teléfono: botón «Entrar con huella»
  async function mostrarBio() {
    const g = Biometria.guardada();
    if (!g || !(await Biometria.disponible())) { $("#bioBox").classList.add("hidden"); return; }
    $("#bioQuien").textContent = `Como ${g.nombre || g.username}`;
    $("#bioBox").classList.remove("hidden");
    $("#u").removeAttribute("autofocus");
    $("#btnBio").focus();
  }
  mostrarBio();
  $("#btnBio").addEventListener("click", async () => {
    const err = $("#errBio");
    err.classList.add("hidden");
    $("#btnBio").disabled = true;
    try {
      const r = await Biometria.entrar();
      setSession(r.access_token, r.usuario);
      location.replace(destino(r.usuario));
    } catch (e) {
      err.textContent = navigator.onLine ? Biometria.mensajeError(e) : "Sin conexión. Necesita señal para iniciar sesión.";
      err.classList.remove("hidden");
      if (!Biometria.guardada()) $("#btnBio").classList.add("hidden");
    } finally {
      $("#btnBio").disabled = false;
    }
  });
  $("#bioOlvidar").addEventListener("click", (ev) => {
    ev.preventDefault();
    Biometria.olvidar();
    $("#bioBox").classList.add("hidden");
    TL.toast("Listo: aquí ya no aparece el botón. Para quitar la huella del sistema, entre y use «Mi clave» (oficina) o «Mis datos» (conductor).", 9000);
    $("#u").focus();
  });

  // Después de entrar con la clave: ofrecer la huella una vez (si el teléfono la tiene)
  async function continuar(u) {
    if (claveActual && await Biometria.conviene(u)) {
      ["#login", "#avisoTemp", "#cambio"].forEach((s) => $(s).classList.add("hidden"));
      $("#ofrecerBio").classList.remove("hidden");
      $("#bioActivar").focus();
      return;
    }
    location.replace(destino(u));
  }
  $("#bioActivar").addEventListener("click", async () => {
    const err = $("#errOfrecer");
    err.classList.add("hidden");
    $("#bioActivar").disabled = true;
    try {
      await Biometria.activar(claveActual, usuarioActual);
      TL.toast("Huella activada: la próxima vez entre con ella");
      location.replace(destino(usuarioActual));
    } catch (e) {
      err.textContent = Biometria.mensajeError(e);
      err.classList.remove("hidden");
      $("#bioActivar").disabled = false;
    }
  });
  $("#bioAhoraNo").addEventListener("click", () => {
    Biometria.rechazar(usuarioActual.username);
    location.replace(destino(usuarioActual));
  });

  // Clave temporal: aviso con los accesos que le quedan, o cambio obligatorio si ya los usó
  function mostrarCambio(obligatorio, texto) {
    $("#login").classList.add("hidden"); $("#avisoTemp").classList.add("hidden");
    $("#cambio").classList.remove("hidden");
    $("#cambioTxt").textContent = texto;
    $("#cambioLuego").classList.toggle("hidden", obligatorio);
    $("#n1").focus();
  }
  function mostrarAviso(u) {
    const n = u.accesos_temporales_restantes;
    $("#avisoTempTxt").textContent = n > 0
      ? `Le ${n === 1 ? "queda 1 acceso" : `quedan ${n} accesos`} con esta clave. Después, el sistema le pedirá cambiarla para poder entrar.`
      : "Este es su último acceso con la clave temporal: la próxima vez deberá crear su clave personal para entrar.";
    $("#login").classList.add("hidden");
    $("#avisoTemp").classList.remove("hidden");
    $("#avisoCambiar").focus();
  }
  $("#avisoCambiar").addEventListener("click", () => mostrarCambio(false, "Escriba su nueva clave personal (mínimo 6 caracteres, distinta de la temporal)."));
  $("#avisoSeguir").addEventListener("click", () => location.replace(destino(usuarioActual)));
  $("#cambioLuego").addEventListener("click", () => location.replace(destino(usuarioActual)));

  $("#login").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = $("#err");
    err.classList.add("hidden");
    $("#btn").disabled = true;
    try {
      const username = $("#u").value.trim();
      claveActual = $("#p").value;
      const r = await api("/api/auth/login", { method: "POST", body: { username, password: claveActual }, onUnauthorized: false });
      setSession(r.access_token, r.usuario);
      usuarioActual = r.usuario;
      if (r.usuario.cambio_clave_obligatorio) {
        mostrarCambio(true, "Ya usó todos los accesos con la clave temporal. Cree su clave personal para continuar (mínimo 6 caracteres).");
      } else if (r.usuario.debe_cambiar_clave) {
        mostrarAviso(r.usuario);
      } else {
        await continuar(r.usuario);
      }
    } catch (e) {
      err.textContent = navigator.onLine ? e.message : "Sin conexión. Necesita señal para iniciar sesión la primera vez.";
      err.classList.remove("hidden");
    } finally {
      $("#btn").disabled = false;
    }
  });

  $("#cambio").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = $("#err2");
    err.classList.add("hidden");
    const n1 = $("#n1").value, n2 = $("#n2").value;
    if (n1 !== n2) { err.textContent = "Las claves no coinciden"; err.classList.remove("hidden"); return; }
    try {
      const r = await api("/api/me/clave", { method: "POST", body: { clave_actual: claveActual, clave_nueva: n1 } });
      setSession(r.access_token, r.usuario);
      claveActual = n1;
      usuarioActual = r.usuario;
      await continuar(r.usuario);
    } catch (e) {
      err.textContent = e.message; err.classList.remove("hidden");
    }
  });
})();
