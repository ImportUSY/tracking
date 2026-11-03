# Publicar Tracking Logístico en Vercel

Resultado: una dirección pública con HTTPS, por ejemplo **https://tracking-logistico.vercel.app**,
desde la que entran Transporte, Cobranzas, los conductores y los vendedores.

| Quién | Dirección |
|---|---|
| Supervisor, Transportista, Cobranzas | `https://<su-proyecto>.vercel.app/` → entra al **panel** |
| Conductores (teléfono) | `https://<su-proyecto>.vercel.app/conductor` (o el APK, ver `APK_ANDROID.md`) |
| Vendedores (sin clave) | `https://<su-proyecto>.vercel.app/tracking` |
| Documentación técnica de la API | `https://<su-proyecto>.vercel.app/api/docs` |

Cómo queda montado en Vercel:

- **Programa**: una función de Vercel (Python/FastAPI, archivo `index.py`).
- **Base de datos**: PostgreSQL en **Neon**, que se agrega desde Vercel (*Storage*). El plan gratuito alcanza para empezar.
- **Fotos**: se guardan dentro de la base, porque Vercel no tiene disco permanente.
- **Excel**: Vercel acepta como máximo 4,5 MB por envío y la REMESA pesa unos 9 MB. Por eso el navegador
  lee el libro y envía solo los valores de la hoja (unos 70 KB comprimidos). El resultado es idéntico
  al de leer el archivo en el servidor; está comprobado con su REMESA real.

---

## Opción A: con el script (recomendada, Windows)

1. Instale **Node.js LTS**: <https://nodejs.org>, o en PowerShell: `winget install OpenJS.NodeJS.LTS`.
2. Cree una cuenta gratuita en <https://vercel.com/signup> si no la tiene.
3. Abra PowerShell en la carpeta `tracking-logistico` y ejecute:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\deploy_vercel.ps1
   ```

   El script:
   1. abre el navegador para que **usted** inicie sesión en Vercel;
   2. crea y vincula el proyecto `tracking-logistico`;
   3. agrega la base de datos **Neon**: acepte los términos y elija la región **Washington D.C. (us-east)**, la más cercana a Venezuela;
   4. genera `SECRET_KEY` y le pide la clave del usuario `supervisor`;
   5. publica en producción y muestra la dirección final.

4. Abra la dirección, entre con `supervisor` y la clave que escribió, y siga el **Primer uso** del `README.md`
   (cargar conductores desde la hoja PLACAS, vendedores y luego la REMESA).

Para publicar cambios más adelante, basta con:
`npx vercel@latest deploy --prod`

## Opción B: desde la página de Vercel (con GitHub)

1. Suba la carpeta `tracking-logistico` a un repositorio de GitHub (puede ser privado).
2. En <https://vercel.com/new>, elija **Import Git Repository**, seleccione el repositorio y presione **Deploy**.
   Vercel detecta FastAPI solo, gracias a `index.py` y `pyproject.toml`.
3. En el proyecto, vaya a **Storage → Create Database → Neon → Continue**, elija la región us-east y en *Connect* marque **Production**.
4. En **Settings → Environment Variables (Production)**, agregue:

   | Variable | Valor |
   |---|---|
   | `SECRET_KEY` | Una clave larga y aleatoria. Genérela con `python -c "import secrets;print(secrets.token_urlsafe(48))"` o en <https://1password.com/password-generator> (40 caracteres o más). |
   | `BOOTSTRAP_ADMIN_PASSWORD` | La clave inicial del usuario `supervisor`. |
   | `APP_ENV` | `production` |

5. Vaya a **Deployments → … → Redeploy**. Cada `git push` vuelve a publicar automáticamente.

## Variables opcionales

| Variable | Para qué |
|---|---|
| `ANDROID_PACKAGE_NAME`, `ANDROID_SHA256_FINGERPRINTS` | Para que el APK abra la app a pantalla completa (ver `APK_ANDROID.md`). |
| `EMAIL_ENABLED=true`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `COBRANZAS_EMAILS` | Correos a vendedores y a Cobranzas. |
| `AI_REVIEW_ENABLED=true`, `ANTHROPIC_API_KEY` | Revisión del resumen de carga con Claude. |
| `TIMEZONE` | Por defecto `America/Caracas`. |

Después de cambiar variables, hay que volver a publicar (*Redeploy* o `npx vercel@latest deploy --prod`).

## Comprobar que funciona

- `https://<su-proyecto>.vercel.app/health` debe responder `{"status":"ok",...}`.
- Si responde **500** al abrir: en el proyecto, **Logs**. El mensaje más común es *"En Vercel se necesita
  PostgreSQL"* (falta conectar Neon) o *"SECRET_KEY es obligatorio"*.
- Si olvida la clave del supervisor: cambie `BOOTSTRAP_ADMIN_PASSWORD` **no** sirve, porque solo se usa cuando la
  base está vacía. Otro usuario Supervisor puede asignarle una clave nueva en *Usuarios → Nueva clave*.
  Si no hay ninguno, ejecute en su PC `DATABASE_URL=<la URL de Neon> tracking crear-supervisor`.

## Costos y límites (consultados en octubre de 2026)

- **Vercel Hobby (gratis)**: Vercel lo define como de *uso personal, no comercial*. Para la operación de una
  empresa corresponde el plan **Pro**: USD 20 al mes, más USD 20 por cada desarrollador adicional; los usuarios que solo miran no pagan.
- **Neon Free**: 1 GB de base por proyecto y 100 horas de cómputo al mes. La base se apaga sola tras 5 minutos sin uso
  y la primera consulta después tarda 1 o 2 segundos más.
  Cada foto ocupa unos 150 a 300 KB, así que caben unas 4.000 fotos además de los datos. Si se queda corto,
  suba de plan en Neon o use un servidor propio con Docker (ver `README.md`).
