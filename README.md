# Gelbooru → Discord (cada 5 minutos, con GitHub Actions)

Cada 5 minutos, un workflow de GitHub Actions pide a la API pública de Gelbooru un
post aleatorio (imagen o video) **por cada canal configurado** y lo envía por webhook
a Discord. Cada canal tiene sus propios tags y recuerda lo que ya envió para no repetir.

## Archivos

| Archivo | Para qué sirve |
|---|---|
| `channels.json` | Lista de canales: tags, rating y secret del webhook de cada uno |
| `gelbooru_webhook.py` | Busca los posts, descarga el archivo y lo envía al webhook |
| `.github/workflows/gelbooru.yml` | Programa la ejecución y conserva el historial entre ejecuciones |
| `requirements.txt` | Dependencias (`requests`) |

## Puesta en marcha

1. **Crea el webhook en Discord:** Ajustes del canal → Integraciones → Webhooks →
   Nuevo webhook → *Copiar URL del webhook*.
2. **Crea un repositorio público en GitHub** y sube estos archivos.
   (Público = minutos de Actions gratis e ilimitados.)
3. En **Settings → Secrets and variables → Actions → Secrets**, crea el secret
   `DISCORD_WEBHOOK_1` con la URL copiada.
4. Edita `channels.json` y pon tus tags en el primer canal (`"tags": "scenery"` es solo
   un ejemplo).
5. En la pestaña **Actions**, abre *Gelbooru a Discord* y pulsa **Run workflow** para
   probarlo. A partir de ahí se ejecuta solo.

## Añadir otro canal con otros tags

1. Crea el webhook del nuevo canal en Discord (paso 1 de arriba).
2. Crea un secret nuevo con otro nombre, por ejemplo `DISCORD_WEBHOOK_2`.
   El workflow ya tiene preparados `DISCORD_WEBHOOK_10`. Si
   necesitas más, añade una línea por secret en el paso *Publicar en Discord* del
   archivo `.github/workflows/gelbooru.yml`.
3. Añade el canal en `channels.json`:

```json
{
  "defaults": { "rating": "g", "max_upload_mb": 10 },
  "channels": [
    { "name": "paisajes", "webhook_env": "DISCORD_WEBHOOK_URL", "tags": "scenery" },
    { "name": "miku", "webhook_env": "DISCORD_WEBHOOK_2", "tags": "hatsune_miku", "rating": "s" }
  ]
}
```

| Campo | Obligatorio | Descripción |
|---|---|---|
| `name` | Sí | Nombre único del canal. Identifica su historial: si lo cambias, empieza de cero. |
| `webhook_env` | Sí | Nombre del secret que contiene la URL del webhook. |
| `tags` | No | Tags separados por espacios (o una lista JSON). |
| `rating` | No | `g` general, `s` sensitive, `q` questionable, `e` explicit. Varios con coma (`"g,s"`); `"any"` desactiva el filtro. |
| `max_upload_mb` | No | Tamaño máximo del archivo a subir. Si pesa más, o Discord lo rechaza, se publica el enlace. |
| `enabled` | No | `false` para pausar un canal sin borrarlo. |

Los valores de `defaults` se aplican a todos los canales y cada canal puede
sobrescribirlos. Cada ejecución publica **un post por canal**.

## Usar tu cuenta de Gelbooru

1. Entra a tu perfil en <https://gelbooru.com/profile> y pulsa **Generate API key**.
2. En el repositorio, ve a Settings → Secrets and variables → Actions → Secrets y crea:
   - `GELBOORU_LOGIN`: tu id de usuario de Gelbooru.
   - `GELBOORU_API_KEY`: la clave generada.
3. Lanza el workflow a mano y revisa el log: si todo va bien aparece
   `Gelbooru: sesión iniciada como <tu usuario> (<nivel>)`. Si las credenciales
   fallan, el log lo indica.

## Historial de posts enviados

El script guarda los IDs ya enviados de cada canal en `state/posted.json` (hasta 5.000
por canal). El workflow conserva ese archivo entre ejecuciones con la caché de
GitHub Actions: al empezar restaura el último guardado y al terminar guarda uno nuevo.

- Cuando ya se enviaron todos los posts que cumplen la búsqueda, el historial de ese
  canal se reinicia y vuelve a empezar el ciclo.
- Si la caché se pierde (por ejemplo, tras una pausa larga del repositorio), lo único
  que ocurre es que pueden repetirse posts; no hay otro efecto.
- Cambiar los tags de un canal no borra su historial. Para empezar de cero, cambia el
  `name` del canal.

## Cosas a tener en cuenta

- **Contenido para adultos:** si usas rating `q` o `e`, Discord exige que el canal esté
  marcado como *con restricción de edad*.
- **Repositorios privados:** cada ejecución consume al menos 1 minuto y son unas 8.600
  al mes, más que los minutos gratuitos de una cuenta personal. Mantén el repositorio
  público.
- **Puntualidad:** GitHub no garantiza la hora exacta de los `cron`; en momentos de
  mucha carga una ejecución puede retrasarse o saltarse. Además, GitHub desactiva los
  workflows programados de un repositorio público tras 60 días sin actividad: si pasa,
  reactívalo desde la pestaña Actions.
- **Errores por canal:** si un canal falla (webhook borrado, secret ausente, tags sin
  resultados), los demás se publican igual y la ejecución aparece en rojo en Actions.

## Probar en local

```bash
pip install -r requirements.txt

# Solo busca y muestra los posts, sin enviar nada ni guardar historial:
DRY_RUN=1 python gelbooru_webhook.py

# Envío real (una variable por cada "webhook_env" de channels.json):
DISCORD_WEBHOOK_1="https://discord.com/api/webhooks/..." python gelbooru_webhook.py
```
