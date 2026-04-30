import logging

import httpx

log = logging.getLogger(__name__)


def ntfy(url: str, topic: str, message: str, title: str = "plex-dub",
         priority: str = "default", token: str | None = None) -> None:
    try:
        headers = {"Title": title, "Priority": priority}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        httpx.post(
            f"{url.rstrip('/')}/{topic}",
            content=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
    except Exception as e:
        log.warning("ntfy failed: %s", e)
