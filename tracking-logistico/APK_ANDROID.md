# APK para los teléfonos de los conductores

> **APK vigente: v1.24.1** (30/10/2026) en `..\APK\v1.24.1\` · paquete `com.egci.tracking` · código 12401 ·
> dominio `https://tracking-logistico-tawny.vercel.app`. La llave de firma está en
> `..\APK\v1.24.1\LLAVE_DE_FIRMA_no_compartir\` (guárdela: se necesita para futuras versiones con
> «Signing key: Use mine»). `android\assetlinks.json` declara esta app (y la del 14/10/2026); al publicar
> con `scripts\publicar_vercel.bat` se copia sola a la variable `ANDROID_ASSETLINKS_JSON` de Vercel.
> La versión del sistema se muestra en la app del conductor (pie de la lista y «Mis datos»).

La app del conductor es una aplicación web instalable (PWA). El APK la "envuelve" con la tecnología
oficial de Google para esto (*Trusted Web Activity*): se instala como cualquier app, abre a pantalla
completa y **se actualiza sola** cada vez que usted publica en Vercel, sin reinstalar nada.

Antes de empezar, el programa debe estar publicado (ver `DESPLIEGUE_VERCEL.md`). En lo que sigue,
`https://tracking-logistico.vercel.app` representa **su** dirección.

## 1. Generar el APK con PWABuilder (gratis, unos 5 minutos)

1. Entre en <https://www.pwabuilder.com>.
2. Escriba `https://tracking-logistico.vercel.app/conductor` y presione **Start**.
   La app ya cumple los requisitos (manifiesto, íconos, service worker y HTTPS).
3. Presione **Package for stores**. En **Android**, presione **Generate Package**.
4. En la ventana de opciones:
   - **Package ID**: `com.egci.tracking` (o el que prefiera; anótelo).
   - **App name**: `Tracking Logístico`. **Launcher name**: `Tracking`.
   - **Signing key**: **New** (la primera vez).
   - Deje lo demás como viene y presione **Download Package**.
5. Descomprima el ZIP. Contiene:
   - el **`.apk`**, para instalar directo en los teléfonos;
   - el **`.aab`**, solo si algún día lo publica en Google Play;
   - **`assetlinks.json`**, el paso 2;
   - **`signing.keystore`** y **`signing-key-info.txt`**: la llave de la app. **Guárdelos en un lugar seguro**,
     porque se necesitan para generar versiones futuras con la misma identidad.

## 2. Conectar el APK con su dominio (para que abra sin barra del navegador)

1. Abra `assetlinks.json` con el Bloc de notas y copie **todo** el contenido.
2. En Vercel, entre al proyecto, luego **Settings → Environment Variables**, y cree la variable
   `ANDROID_ASSETLINKS_JSON` en **Production**, con lo que copió como valor.
3. Publique de nuevo (**Deployments → … → Redeploy**, o `npx vercel@latest deploy --prod`).
4. Compruebe que `https://tracking-logistico.vercel.app/.well-known/assetlinks.json` muestra ese contenido.

Si se salta este paso, el APK funciona igual, pero con una barra superior que muestra la dirección.

## 3. Instalar en cada teléfono Android

1. Envíe el archivo `.apk` al teléfono por WhatsApp, Google Drive, correo o cable USB.
2. Ábralo. Android pedirá permitir **"Instalar apps desconocidas"** para la app desde la que lo abrió
   (WhatsApp, Archivos, etc.). Acepte y presione **Instalar**.
   Si aparece un aviso de Play Protect, elija **Instalar de todas formas**: es su propia app.
3. Abra **Tracking**. El conductor entra con su **cédula** y la clave temporal que le entregó Transporte,
   y crea su clave personal.

Requisito: tener **Google Chrome** instalado y actualizado. Viene en casi todos los Android.

## Alternativa sin APK (sirve también en iPhone)

Abra `https://tracking-logistico.vercel.app/conductor` en Chrome (Android) o Safari (iPhone), luego
**menú → Agregar a pantalla de inicio**. Queda un ícono igual al de una app, con el mismo funcionamiento
sin señal.

## Versiones nuevas

- Los cambios del programa llegan solos al APK, porque carga la app publicada en Vercel.
- Solo se genera un APK nuevo si cambia el nombre, el ícono o el dominio. En ese caso, en PWABuilder
  elija **Signing key: Mine**, suba su `signing.keystore` con los datos de `signing-key-info.txt` y
  aumente el **App version code**.
