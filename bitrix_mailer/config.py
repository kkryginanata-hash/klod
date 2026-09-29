"""Загрузка настроек (config.toml) и описания кампании (campaigns/*.toml)."""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class Settings:
    webhook_url: str
    requests_per_second: float = 2.0
    db_path: str = "data/mailer.sqlite"
    reports_dir: str = "reports"
    send_via: str = "bitrix"
    from_address: str = ""
    from_name: str = ""
    reply_to: str = ""
    responsible_id: int | None = None
    rate_per_minute: float = 30.0
    check_dns: bool = False
    smtp: dict = field(default_factory=dict)


@dataclass
class Campaign:
    name: str
    key: str
    subject: str
    body_html: str
    contact_mode: str = "primary"
    filter: dict = field(default_factory=dict)
    raw_filter: dict = field(default_factory=dict)
    dedupe_email: bool = True
    company_fallback: bool = False  # сделка без контакта → email компании сделки
    bitrix_history_subject: str = ""
    after_send: dict = field(default_factory=dict)  # изменить сделку после отправки: {"Стадия": "..."}


def load_settings(path: str | Path) -> Settings:
    p = Path(path)
    data = tomllib.loads(p.read_text("utf-8")) if p.exists() else {}
    b, st, snd, smtp = (data.get(k, {}) for k in ("bitrix", "storage", "send", "smtp"))
    webhook = os.environ.get("BITRIX_WEBHOOK_URL") or b.get("webhook_url", "")
    if not webhook:
        raise ConfigError("не задан вебхук: BITRIX_WEBHOOK_URL или [bitrix].webhook_url в config.toml")
    smtp = dict(smtp)
    smtp["password"] = os.environ.get("SMTP_PASSWORD", smtp.get("password", ""))
    via = snd.get("via", "bitrix")
    if via not in ("bitrix", "smtp"):
        raise ConfigError("[send].via: bitrix или smtp")
    if via == "smtp" and not smtp.get("host"):
        raise ConfigError("для [send].via = \"smtp\" заполните секцию [smtp]")
    return Settings(
        webhook_url=webhook,
        requests_per_second=float(b.get("requests_per_second", 2)),
        db_path=st.get("path", "data/mailer.sqlite"),
        reports_dir=st.get("reports_dir", "reports"),
        send_via=via,
        from_address=snd.get("from", ""),
        from_name=snd.get("from_name", ""),
        reply_to=snd.get("reply_to", ""),
        responsible_id=int(snd["responsible_id"]) if snd.get("responsible_id") else None,
        rate_per_minute=float(snd.get("rate_per_minute", 30)),
        check_dns=bool(snd.get("check_dns", False)),
        smtp=smtp,
    )


def load_campaign(path: str | Path) -> Campaign:
    p = Path(path)
    data = tomllib.loads(p.read_text("utf-8"))
    body = data.get("body", "")
    if data.get("template"):
        tpl = Path(data["template"])
        if not tpl.is_absolute() and not tpl.exists():
            tpl = p.parent / tpl
        body = tpl.read_text("utf-8")
    if not body.strip():
        raise ConfigError(f"{p}: нужен body = \"\"\"...\"\"\" или template = \"путь.html\"")
    subject = data.get("subject", "").strip()
    if not subject:
        raise ConfigError(f"{p}: не задана тема письма (subject)")
    flt = dict(data.get("filter", {}))
    raw = flt.pop("raw", {}) or {}
    key = data.get("key") or "auto-" + hashlib.sha1((subject + "\n" + body).encode()).hexdigest()[:12]
    history = data.get("history", {})
    return Campaign(
        name=data.get("name", p.stem),
        key=key,
        subject=subject,
        body_html=body,
        contact_mode=data.get("contact_mode", "primary"),
        filter=flt,
        raw_filter=raw,
        dedupe_email=bool(data.get("dedupe_email", True)),
        company_fallback=bool(data.get("company_fallback", False)),
        bitrix_history_subject=history.get("bitrix_subject_contains", ""),
        after_send=dict(data.get("after_send", {})),
    )
