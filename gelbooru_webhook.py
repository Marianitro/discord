#!/usr/bin/env python3
"""
Publica posts aleatorios de Danbooru (imágenes y videos) en uno o varios canales
de Discord mediante webhooks. Pensado para ejecutarse cada 5 minutos con
GitHub Actions.

Configuración
-------------
Los canales se definen en `channels.json` (tags, rating, tamaño máximo). Las URLs
de los webhooks NO van en ese archivo: cada canal indica en `webhook_env` el nombre
del secret/variable de entorno que contiene su URL.

Variables de entorno
--------------------
DISCORD_WEBHOOK_URL, DISCORD_WEBHOOK_2, …   URLs de webhook (las que cites en channels.json).
GELBOORU_LOGIN / GELBOORU_API_KEY           (opcionales) Tu cuenta de Danbooru.
CHANNELS_FILE                               Ruta del JSON de canales. Por defecto: channels.json
STATE_FILE                                  Historial de posts enviados. Por defecto: state/posted.json
DRY_RUN                                     "1" para buscar y mostrar sin enviar a Discord ni guardar historial.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

GELBOORU = "https://gelbooru.com"
USER_AGENT = "gelbooru-discord-webhook/2.0 (GitHub Actions)"

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "avif"}
VIDEO_EXTS = {"mp4", "webm"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

BATCH_SIZE = 40         # posts aleatorios que se piden por consulta
MAX_QUERIES = 3         # consultas a Danbooru por canal antes de rendirse
MAX_POSTS_TRIED = 10    # posts que se intentan descargar/enviar por canal
MAX_HISTORY = 5000      # IDs recordados por canal


class ConfigError(Exception):
    """Error en channels.json."""


class WebhookError(Exception):
    """El webhook es inválido o fue eliminado: no tiene sentido reintentar."""


# --------------------------------------------------------------------------- #
# Utilidades y configuración
# --------------------------------------------------------------------------- #
def env(name: str, default: str = "") -> str:
    """Lee una variable de entorno; vacía o ausente devuelve el valor por defecto."""
    value = os.environ.get(name, "").strip()
    return value or default


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


def build_query(channel: dict) -> str:
    tags = str(channel.get("tags") or "").strip()
    rating = str(channel.get("rating") or "").strip().lower()
    parts = [tags] if tags else []
    # `rating:` es un metatag gratuito: no cuenta para el límite de tags de Danbooru.
    if rating and rating not in ("any", "all", "none"):
        parts.append(f"rating:{rating}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Historial de posts enviados
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
    tmp.replace(path)  # escritura atómica


# --------------------------------------------------------------------------- #
# Danbooru
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
    login, api_key = env("GELBOORU_LOGIN"), env("GELBOORU_API_KEY")
    if login and api_key:
        session.auth = (login, api_key)
    elif login or api_key:
        print("Aviso: hay que definir GELBOORU_LOGIN y GELBOORU_API_KEY juntos; se ignoran.")
    return session


def check_account(session: requests.Session) -> bool:
    """Si hay credenciales, comprueba que funcionan. Devuelve False si son inválidas."""
    if not session.auth:
        print("Danbooru: sin cuenta (búsquedas anónimas).")
        return True
    try:
        response = session.get(f"{GELBOORU}/profile.json", timeout=30)
    except requests.RequestException as exc:
        print(f"Aviso: no se pudo verificar la cuenta de Danbooru ({exc}).")
        return True
    if response.status_code in (401, 403):
        print(
            "Las credenciales de Danbooru no son válidas. Revisa los secrets "
            "GELBOORU_LOGIN (tu nombre de usuario) y GELBOORU_API_KEY.",
            file=sys.stderr,
        )
        return False
    if response.ok:
        profile = response.json()
        print(f"Danbooru: sesión iniciada como {profile.get('name')} ({profile.get('level_string')}).")
    return True


def fetch_random_posts(session: requests.Session, query: str) -> list[dict]:
    response = session.get(
        f"{GELBOORU}/posts.json",
        params={"tags": query, "limit": BATCH_SIZE, "random": "true"},
        timeout=30,
    )
    if not response.ok:
        hint = ""
        if response.status_code == 422:
            hint = " (¿demasiados tags para el tipo de cuenta de Danbooru?)"
        raise RuntimeError(f"Danbooru respondió {response.status_code}{hint}: {response.text[:300]}")
    data = response.json()
    return data if isinstance(data, list) else []


def is_usable(post: dict) -> bool:
    """Descarta posts borrados/baneados, sin URL pública o con formatos no soportados."""
    if post.get("is_deleted") or post.get("is_banned"):
        return False
    ext = (post.get("file_ext") or "").lower()
    if ext == "zip":  # ugoira: la versión "large" es un webm
        return bool(post.get("large_file_url"))
    return bool(post.get("file_url")) and ext in MEDIA_EXTS


def choose_media(post: dict, max_bytes: int) -> Optional[tuple[str, str]]:
    """
    Devuelve (url, extensión) del archivo a subir, o None si no cabe en el límite.
    Para imágenes pesadas usa la versión "large" (reducida) de Danbooru.
    """
    ext = (post.get("file_ext") or "").lower()
    file_url = post.get("file_url")
    large_url = post.get("large_file_url")
    size = post.get("file_size") or 0

    if ext == "zip":
        return (large_url, "webm") if large_url else None
    if size <= max_bytes:
        return file_url, ext
    if ext in IMAGE_EXTS and large_url and large_url != file_url:
        return large_url, large_url.rsplit(".", 1)[-1].lower()
    return None


def download(url: str, max_bytes: int) -> Optional[bytes]:
    """Descarga el archivo; devuelve None si supera max_bytes."""
    # Sin credenciales: el CDN no las necesita.
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=120) as r:
        r.raise_for_status()
        buffer = bytearray()
        for chunk in r.iter_content(chunk_size=1 << 16):
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                return None
        return bytes(buffer)


def clean(tag_string: str, limit: int = 3) -> str:
    tags = [t.replace("_", " ") for t in (tag_string or "").split() if t]
    shown = ", ".join(tags[:limit])
    if len(tags) > limit:
        shown += f" (+{len(tags) - limit})"
    return shown


def build_caption(post: dict) -> str:
    lines = []
    artist = clean(post.get("tag_string_artist"))
    character = clean(post.get("tag_string_character"))
    if artist:
        lines.append(f"**Artista:** {artist}")
    if character:
        lines.append(f"**Personaje:** {character}")
    # Los <> evitan que Discord genere una vista previa duplicada del enlace.
    lines.append(f"<{GELBOORU}/posts/{post['id']}>")
    return "\n".join(lines)[:1900]


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def send_to_discord(
    webhook_url: str,
    content: str,
    filename: Optional[str] = None,
    data: Optional[bytes] = None,
) -> requests.Response:
    payload = {"content": content, "allowed_mentions": {"parse": []}}

    for _ in range(4):
        if data is not None:
            response = requests.post(
                webhook_url,
                params={"wait": "true"},
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": (filename, data)},
                timeout=120,
            )
        else:
            response = requests.post(
                webhook_url, params={"wait": "true"}, json=payload, timeout=30
            )

        if response.status_code == 429:  # rate limit: esperar lo que pida Discord
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


def publish(webhook_url: str, post: dict, max_bytes: int, dry_run: bool, label: str) -> bool:
    """Intenta publicar un post. Devuelve True si quedó publicado (o simulado)."""
    post_id = post["id"]
    caption = build_caption(post)
    media = choose_media(post, max_bytes)

    payload_file: Optional[tuple[str, bytes]] = None
    if media:
        url, ext = media
        content = download(url, max_bytes)
        if content is not None:
            payload_file = (f"gelbooru_{post_id}.{ext}", content)

    if dry_run:
        kind = (
            f"archivo {payload_file[0]} ({len(payload_file[1]) / 1e6:.2f} MB)"
            if payload_file
            else "solo enlace"
        )
        print(f"[{label}] [DRY_RUN] post #{post_id} → {kind}\n{caption}")
        return True

    if payload_file:
        response = send_to_discord(webhook_url, caption, payload_file[0], payload_file[1])
        check_webhook(response)
        if response.status_code == 413:
            print(f"[{label}] Discord rechazó el archivo por tamaño (post #{post_id}); envío el enlace.")
        elif not response.ok:
            print(f"[{label}] Discord respondió {response.status_code}: {response.text[:300]}")
            return False
        else:
            print(f"[{label}] Publicado post #{post_id} como archivo ({payload_file[0]}).")
            return True

    # Sin archivo (demasiado grande o rechazado): se publica el enlace directo al
    # medio para que Discord lo incruste, más la página del post.
    direct = (post.get("file_url") or post.get("large_file_url") or "").strip()
    page_link = f"{GELBOORU}/posts/{post_id}"
    fallback = f"{caption}\n{direct}" if direct else f"{caption}\n{page_link}"
    response = send_to_discord(webhook_url, fallback)
    check_webhook(response)
    if not response.ok:
        print(f"[{label}] Discord respondió {response.status_code}: {response.text[:300]}")
        return False
    print(f"[{label}] Publicado post #{post_id} como enlace.")
    return True


# --------------------------------------------------------------------------- #
# Lógica por canal
# --------------------------------------------------------------------------- #
def post_to_channel(
    session: requests.Session,
    channel: dict,
    history: list[int],
    dry_run: bool,
) -> tuple[bool, list[int]]:
    """
    Publica un post nuevo en el canal. Devuelve (publicado, historial_actualizado).
    Si ya se enviaron todos los posts que cumplen la búsqueda, reinicia el historial.
    """
    label = channel["name"]
    query = build_query(channel)
    max_bytes = int(float(channel["max_upload_mb"]) * 1024 * 1024)

    webhook_url = env(channel["webhook_env"])
    if not webhook_url and not dry_run:
        print(
            f"[{label}] Falta el secret/variable {channel['webhook_env']} con la URL del webhook.",
            file=sys.stderr,
        )
        return False, history

    print(f"[{label}] Buscando: {query or '(sin tags)'}")
    seen = set(history)
    tried = 0
    last_usable: list[dict] = []

    for _ in range(MAX_QUERIES):
        posts = fetch_random_posts(session, query)
        if not posts:
            print(f"[{label}] La búsqueda no devolvió resultados. Revisa los tags/rating.", file=sys.stderr)
            return False, history

        usable = [p for p in posts if is_usable(p)]
        last_usable = usable
        for post in (p for p in usable if p["id"] not in seen):
            if tried >= MAX_POSTS_TRIED:
                break
            tried += 1
            try:
                if publish(webhook_url, post, max_bytes, dry_run, label):
                    return True, (history + [post["id"]])[-MAX_HISTORY:]
            except requests.RequestException as exc:
                print(f"[{label}] Fallo con el post #{post.get('id')}: {exc}")

        if tried:
            break  # había posts nuevos pero no se pudo publicar ninguno: no reiniciar
        if len(posts) < BATCH_SIZE:
            break  # ya se recibió todo el conjunto de resultados

    if tried == 0 and last_usable:
        print(f"[{label}] Ya se enviaron todos los posts disponibles; reinicio el historial.")
        for post in last_usable[:MAX_POSTS_TRIED]:
            try:
                if publish(webhook_url, post, max_bytes, dry_run, label):
                    return True, [post["id"]]
            except requests.RequestException as exc:
                print(f"[{label}] Fallo con el post #{post.get('id')}: {exc}")

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
    if not check_account(session):
        return 1

    state = load_state(state_path)
    failures = 0

    for index, channel in enumerate(channels):
        label = channel["name"]
        if index:
            time.sleep(1)
        try:
            ok, history = post_to_channel(session, channel, state.get(label, []), dry_run)
        except WebhookError as exc:
            print(f"[{label}] {exc}", file=sys.stderr)
            ok, history = False, state.get(label, [])
        except (requests.RequestException, RuntimeError) as exc:
            print(f"[{label}] Error: {exc}", file=sys.stderr)
            ok, history = False, state.get(label, [])

        if ok and not dry_run:
            state[label] = history
            save_state(state_path, state)  # se guarda tras cada canal
        failures += 0 if ok else 1

    if failures:
        print(f"\n{failures} de {len(channels)} canal(es) con errores.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
