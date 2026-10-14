#!/usr/bin/env python3
"""
Publica posts aleatorios de Gelbooru (imágenes y videos) en uno o varios canales
de Discord mediante webhooks. Pensado para ejecutarse cada 5 minutos con
GitHub Actions.

Los canales se definen en `channels.json` (name, webhook_env, tags, rating,
enabled). Las URLs de los webhooks van en secrets; cada canal
indica en `webhook_env` el nombre del secret.

Cada mensaje lleva la imagen incrustada y un texto «Ver en Gelbooru» con el
enlace oculto. El script descarga el archivo y lo adjunta dentro del propio embed,
así Discord lo muestra aunque no pueda cargarlo desde Gelbooru.

Variables de entorno
--------------------
DISCORD_WEBHOOK_1, DISCORD_WEBHOOK_2, …  URLs de webhook (las que cites en channels.json).
GELBOORU_LOGIN      Tu user_id numérico de Gelbooru (Opciones de cuenta).
GELBOORU_API_KEY    Tu api_key de Gelbooru (Opciones de cuenta).
CHANNELS_FILE       Por defecto: channels.json
STATE_FILE          Por defecto: state/posted.json
DRY_RUN             "1" para buscar y mostrar sin enviar a Discord ni guardar historial.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

GELBOORU = "https://gelbooru.com"
USER_AGENT = "Mozilla/5.0 (compatible; gelbooru-discord-webhook/3.0)"

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "avif"}
VIDEO_EXTS = {"mp4", "webm"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# En Gelbooru el rating se escribe con la palabra completa.
RATING_NAMES = {
    "g": "general", "s": "sensitive", "q": "questionable", "e": "explicit",
    "general": "general", "sensitive": "sensitive",
    "questionable": "questionable", "explicit": "explicit",
}
ALL_RATINGS = ["general", "sensitive", "questionable", "explicit"]

BATCH_SIZE = 40
MAX_QUERIES = 3
MAX_POSTS_TRIED = 10
MAX_HISTORY = 5000


class ConfigError(Exception):
    """Error en channels.json."""


class WebhookError(Exception):
    """El webhook es inválido o fue eliminado: no tiene sentido reintentar."""


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
def load_channels(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"No existe {path}.")
    except ValueError as exc:
        raise ConfigError(f"{path} no es un JSON válido: {exc}")

    defaults = {"rating": "g", "max_upload_mb": 10, "enabled": True}
    defaults.update(raw.get("defaults", {}))

    channels, names = [], set()
    for index, item in enumerate(raw.get("channels", []), start=1):
        channel = {**defaults, **item}
        name = str(channel.get("name", "")).strip()
        if not name:
            raise ConfigError(f"El canal #{index} no tiene 'name'.")
        if name in names:
            raise ConfigError(f"El nombre de canal '{name}' está repetido.")
        names.add(name)
        if not channel["enabled"]:
            continue
        if not channel.get("webhook_env"):
            raise ConfigError(f"El canal '{name}' no tiene 'webhook_env'.")
        if isinstance(channel.get("tags"), list):
            channel["tags"] = " ".join(channel["tags"])
        channel["name"] = name
        channels.append(channel)

    if not channels:
        raise ConfigError("No hay ningún canal activo en la configuración.")
    return channels


def rating_filters(value) -> list[str]:
    """
    Convierte "g", "s,q,e" o "general,sensitive" en filtros que Gelbooru entiende.
    Gelbooru NO acepta listas con coma (rating:s,q,e) y repetir rating:x rating:y
    daría 0 resultados, así que varios ratings se expresan excluyendo los que sobran:
    "s,q,e" -> -rating:general   |   "g,s" -> -rating:questionable -rating:explicit
    """
    wanted: list[str] = []
    for item in str(value or "").lower().replace(" ", "").split(","):
        if not item:
            continue
        if item in ("any", "all", "none"):
            return []
        name = RATING_NAMES.get(item)
        if not name:
            raise ConfigError(f"Rating desconocido '{item}'. Usa g, s, q, e (o 'any').")
        if name not in wanted:
            wanted.append(name)
    if not wanted or len(wanted) == len(ALL_RATINGS):
        return []
    if len(wanted) == 1:
        return [f"rating:{wanted[0]}"]
    return [f"-rating:{name}" for name in ALL_RATINGS if name not in wanted]


def build_query(channel: dict) -> str:
    tags = str(channel.get("tags") or "").strip()
    parts = [tags] if tags else []
    parts += rating_filters(channel.get("rating"))
    parts.append("sort:random")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Historial
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict[str, list[int]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): [int(i) for i in v] for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except (ValueError, TypeError, AttributeError) as exc:
        print(f"Historial ilegible ({exc}); empiezo de cero.")
        return {}


def save_state(path: Path, state: dict[str, list[int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Gelbooru
# --------------------------------------------------------------------------- #
def gelbooru_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def auth_params() -> dict:
    """Gelbooru autentica con user_id + api_key como parámetros de la URL."""
    user_id, api_key = env("GELBOORU_LOGIN"), env("GELBOORU_API_KEY")
    if user_id and api_key:
        return {"user_id": user_id, "api_key": api_key}
    if user_id or api_key:
        print("Aviso: hay que definir GELBOORU_LOGIN y GELBOORU_API_KEY juntos; se ignoran.")
    else:
        print("Gelbooru: sin credenciales (la API puede exigirlas).")
    return {}


def fetch_random_posts(session: requests.Session, query: str, auth: dict) -> list[dict]:
    response = session.get(
        f"{GELBOORU}/index.php",
        params={
            "page": "dapi", "s": "post", "q": "index", "json": 1,
            "tags": query, "limit": BATCH_SIZE, **auth,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Gelbooru respondió {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
    except ValueError:
        # Normalmente es una página HTML (bloqueo de Cloudflare o credenciales rechazadas).
        raise RuntimeError(f"Gelbooru no devolvió JSON: {response.text[:300]!r}")
    if isinstance(data, dict):
        if "post" not in data:  # sin resultados, o mensaje de error de la API
            message = data.get("message") or data.get("error")
            if message:
                raise RuntimeError(f"Gelbooru: {message}")
            return []
        data = data["post"]
        if isinstance(data, dict):  # un solo resultado
            data = [data]
    return data if isinstance(data, list) else []


def normalize(post: dict) -> dict:
    """Adapta un post de Gelbooru a los campos que usa el resto del script."""
    file_url = (post.get("file_url") or "").strip()
    ext = file_url.rsplit(".", 1)[-1].lower().split("?")[0] if "." in file_url else ""
    sample = (post.get("sample_url") or "").strip()
    return {
        "id": int(post["id"]),
        "file_url": file_url,
        "file_ext": ext,
        "sample_url": sample if sample and sample != file_url else "",
        "deleted": post.get("status") == "deleted",
    }


def is_usable(post: dict) -> bool:
    return not post["deleted"] and bool(post["file_url"]) and post["file_ext"] in MEDIA_EXTS


def download(url: str, max_bytes: int) -> Optional[bytes]:
    """Descarga el archivo; devuelve None si supera max_bytes o no se pudo bajar."""
    try:
        with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=120) as r:
            r.raise_for_status()
            buffer = bytearray()
            for chunk in r.iter_content(chunk_size=1 << 16):
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    return None
            return bytes(buffer)
    except requests.RequestException as exc:
        print(f"No se pudo descargar {url}: {exc}")
        return None


def fetch_media(post: dict, max_bytes: int) -> Optional[tuple[str, bytes]]:
    """Devuelve (nombre, bytes) del archivo; en imágenes pesadas prueba la versión reducida."""
    candidates = [post["file_url"]]
    if post["file_ext"] in IMAGE_EXTS and post["sample_url"]:
        candidates.append(post["sample_url"])
    for url in candidates:
        data = download(url, max_bytes)
        if data is not None:
            ext = url.rsplit(".", 1)[-1].lower().split("?")[0]
            return f"gelbooru_{post['id']}.{ext}", data
    return None


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def send_to_discord(
    webhook_url: str, payload: dict, attachment: Optional[tuple[str, bytes]] = None
) -> requests.Response:
    for _ in range(4):
        if attachment:
            name, data = attachment
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            response = requests.post(
                webhook_url, params={"wait": "true"},
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": (name, data, mime)}, timeout=120,
            )
        else:
            response = requests.post(webhook_url, params={"wait": "true"}, json=payload, timeout=30)

        if response.status_code == 429:
            try:
                wait = float(response.json().get("retry_after", 2))
            except ValueError:
                wait = 2.0
            print(f"Rate limit de Discord, esperando {wait:.1f}s…")
            time.sleep(min(wait, 30))
            continue
        return response
    return response


def check_webhook(response: requests.Response) -> None:
    if response.status_code in (401, 403, 404):
        raise WebhookError(
            f"Discord respondió {response.status_code}: el webhook no es válido o fue eliminado."
        )


def md_url(url: str) -> str:
    """Escapa lo que rompería un enlace Markdown [texto](url)."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def build_message(post: dict, attachment: Optional[str] = None) -> dict:
    """
    `attachment` es el nombre del archivo que se sube junto al mensaje (o None).

    Imágenes: embed con la imagen y debajo el texto «Ver en Gelbooru» (enlace al post).
      - con adjunto: la imagen se muestra dentro del embed (attachment://nombre);
      - sin adjunto: se usa la URL de Gelbooru (Discord tiene que poder cargarla).
    Videos: un embed no reproduce video. Con adjunto, Discord muestra el reproductor y el
      texto enlaza al post; sin adjunto, el texto enlaza al archivo y Discord lo despliega.
    """
    page = f"{GELBOORU}/index.php?page=post&s=view&id={post['id']}"
    page_link = f"[Ver en Gelbooru]({md_url(page)})"
    payload: dict = {"allowed_mentions": {"parse": []}}
    if post["file_ext"] in IMAGE_EXTS:
        image_url = f"attachment://{attachment}" if attachment else post["file_url"]
        payload["embeds"] = [{"description": page_link, "image": {"url": image_url}}]
    elif attachment:
        payload["content"] = page_link
    else:
        payload["content"] = f"[Ver en Gelbooru]({md_url(post['file_url'])})"
    return payload


def publish(webhook_url: str, post: dict, max_bytes: int, dry_run: bool, label: str) -> bool:
    post_id = post["id"]
    media = fetch_media(post, max_bytes)
    payload = build_message(post, media[0] if media else None)

    if dry_run:
        kind = f"adjunto {media[0]} ({len(media[1]) / 1e6:.2f} MB)" if media else "solo URL"
        print(f"[{label}] [DRY_RUN] post #{post_id} → {kind}\n{json.dumps(payload, ensure_ascii=False)}")
        return True

    response = send_to_discord(webhook_url, payload, media)
    check_webhook(response)
    if media and response.status_code == 413:
        print(f"[{label}] Discord rechazó el archivo por tamaño (post #{post_id}); envío solo la URL.")
        media = None
        response = send_to_discord(webhook_url, build_message(post), None)
        check_webhook(response)
    if not response.ok:
        print(f"[{label}] Discord respondió {response.status_code}: {response.text[:300]}")
        return False
    how = f"con archivo adjunto ({media[0]})" if media else "solo con la URL"
    print(f"[{label}] Publicado post #{post_id} {how}. Archivo original: {post['file_url']}")
    return True


# --------------------------------------------------------------------------- #
# Lógica por canal
# --------------------------------------------------------------------------- #
def post_to_channel(session, channel, history, dry_run, auth) -> tuple[bool, list[int]]:
    label = channel["name"]
    query = build_query(channel)
    max_bytes = int(float(channel["max_upload_mb"]) * 1024 * 1024)

    webhook_url = env(channel["webhook_env"])
    if not webhook_url and not dry_run:
        print(
            f"[{label}] Falta el secret {channel['webhook_env']} con la URL del webhook "
            "(revisa que el nombre coincida con 'webhook_env' en channels.json y con el workflow).",
            file=sys.stderr,
        )
        return False, history

    print(f"[{label}] Buscando: {query}")
    seen = set(history)
    tried = 0
    last_usable: list[dict] = []

    for _ in range(MAX_QUERIES):
        raw_posts = fetch_random_posts(session, query, auth)
        if not raw_posts:
            print(f"[{label}] La búsqueda no devolvió resultados. Revisa los tags/rating.", file=sys.stderr)
            return False, history

        usable = [p for p in map(normalize, raw_posts) if is_usable(p)]
        last_usable = usable
        for post in (p for p in usable if p["id"] not in seen):
            if tried >= MAX_POSTS_TRIED:
                break
            tried += 1
            try:
                if publish(webhook_url, post, max_bytes, dry_run, label):
                    return True, (history + [post["id"]])[-MAX_HISTORY:]
            except requests.RequestException as exc:
                print(f"[{label}] Fallo con el post #{post['id']}: {exc}")

        if tried:
            break
        if len(raw_posts) < BATCH_SIZE:
            break

    if tried == 0 and last_usable:
        print(f"[{label}] Ya se enviaron todos los posts disponibles; reinicio el historial.")
        for post in last_usable[:MAX_POSTS_TRIED]:
            try:
                if publish(webhook_url, post, max_bytes, dry_run, label):
                    return True, [post["id"]]
            except requests.RequestException as exc:
                print(f"[{label}] Fallo con el post #{post['id']}: {exc}")

    print(f"[{label}] No se pudo publicar ningún post en esta ejecución.", file=sys.stderr)
    return False, history


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    dry_run = env("DRY_RUN") == "1"
    config_path = Path(env("CHANNELS_FILE", "channels.json"))
    state_path = Path(env("STATE_FILE", "state/posted.json"))

    try:
        channels = load_channels(config_path)
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 1

    session = gelbooru_session()
    auth = auth_params()
    state = load_state(state_path)
    failures = 0

    for index, channel in enumerate(channels):
        label = channel["name"]
        if index:
            time.sleep(1)
        try:
            ok, history = post_to_channel(session, channel, state.get(label, []), dry_run, auth)
        except WebhookError as exc:
            print(f"[{label}] {exc}", file=sys.stderr)
            ok, history = False, state.get(label, [])
        except (requests.RequestException, RuntimeError) as exc:
            print(f"[{label}] Error: {exc}", file=sys.stderr)
            ok, history = False, state.get(label, [])

        if ok and not dry_run:
            state[label] = history
            save_state(state_path, state)
        failures += 0 if ok else 1

    if failures:
        print(f"\n{failures} de {len(channels)} canal(es) con errores.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
