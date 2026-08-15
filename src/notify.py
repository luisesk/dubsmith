import logging

import httpx

log = logging.getLogger(__name__)


def ntfy(url: str, topic: str, message: str, title: str = "plex-dub",
         priority: str = "default", token: str | None = None,
         user: str | None = None, password: str | None = None) -> None:
    """Publica no ntfy. Autentica por token (Bearer) ou por usuario/senha.

    O servidor de casa exige credencial no topico e recusa com 403 quem nao
    manda nenhuma. Como httpx nao levanta excecao em resposta 4xx, a versao
    anterior engolia esse 403 em silencio e todo aviso do dubsmith sumia sem
    deixar rastro no log. Por isso o status agora e conferido.
    """
    try:
        headers = {"Title": title, "Priority": priority}
        auth = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif user:
            auth = (user, password or "")
        r = httpx.post(
            f"{url.rstrip('/')}/{topic}",
            content=message.encode("utf-8"),
            headers=headers,
            auth=auth,
            timeout=10,
        )
        if r.status_code >= 400:
            # Sem o corpo da resposta: em 401/403 ele as vezes ecoa credencial.
            log.warning("ntfy recusou: HTTP %s (topico=%s)", r.status_code, topic)
    except Exception as e:
        log.warning("ntfy failed: %s", e)


def push(nt: dict, message: str, title: str = "plex-dub",
         priority: str = "default") -> None:
    """Publica usando o bloco `ntfy` do config.yml inteiro.

    Existe porque cada ponto de chamada repassava so o `token` e deixava
    `user`/`password` para tras. Como a config de casa autentica por
    usuario/senha, toda notificacao saia sem credencial e voltava 403.
    Passando o dict inteiro, um campo novo na config nao precisa ser
    replicado em cada chamada.
    """
    if not nt.get("url") or not nt.get("topic"):
        return
    ntfy(nt["url"], nt["topic"], message, title=title, priority=priority,
         token=nt.get("token"), user=nt.get("user"), password=nt.get("password"))
