"""Проверка email: синтаксис и (по желанию) наличие почтового сервера у домена."""

from __future__ import annotations

import functools
import re

_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_DOMAIN = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})"
EMAIL_RE = re.compile(rf"^{_LOCAL}@{_DOMAIN}$")


def normalize(email: str) -> str:
    return (email or "").strip().strip("<>").strip().lower()


def syntax_error(email: str) -> str | None:
    """None — адрес корректен, иначе причина."""
    e = normalize(email)
    if not e:
        return "пустой email"
    if len(e) > 254:
        return "слишком длинный адрес"
    if e.count("@") != 1:
        return "нет «@» или их несколько"
    try:
        e.encode("ascii")
    except UnicodeEncodeError:
        local, domain = e.split("@")
        try:
            e = local.encode("ascii").decode() + "@" + domain.encode("idna").decode()
        except (UnicodeError, UnicodeEncodeError):
            return "недопустимые символы"
    if not EMAIL_RE.match(e):
        return "некорректный формат"
    return None


@functools.lru_cache(maxsize=4096)
def domain_accepts_mail(domain: str) -> bool | None:
    """True/False — есть ли MX/A у домена; None — проверить нельзя (нет dnspython)."""
    try:
        import dns.resolver  # type: ignore
    except ImportError:
        return None
    for rtype in ("MX", "A", "AAAA"):
        try:
            if dns.resolver.resolve(domain, rtype, lifetime=5):
                return True
        except Exception:
            continue
    return False


def check(email: str, check_dns: bool = False) -> str | None:
    err = syntax_error(email)
    if err or not check_dns:
        return err
    ok = domain_accepts_mail(normalize(email).split("@")[1])
    if ok is False:
        return "домен не принимает почту (нет MX/A записи)"
    return None
