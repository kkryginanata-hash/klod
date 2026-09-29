"""Сбор получателей: сделки по фильтру → контакты → email → проверки → очередь.

Все обращения к Битрикс24 здесь — только чтение.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from . import validation
from .client import BitrixClient
from .storage import Storage

log = logging.getLogger(__name__)

CONTACT_MODES = {
    "primary": "только основному контакту сделки",
    "all": "всем связанным контактам",
    "valid_only": "всем связанным контактам с валидным email",
}

DEAL_SELECT = ["ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "ASSIGNED_BY_ID", "CONTACT_ID", "COMPANY_ID",
               "OPPORTUNITY", "CURRENCY_ID", "DATE_CREATE"]
CONTACT_SELECT = ["ID", "NAME", "LAST_NAME", "SECOND_NAME", "HONORIFIC", "POST", "COMPANY_ID", "EMAIL"]

REASON_NO_CONTACT = "у сделки нет связанного контакта"
REASON_CONTACT_MISSING = "контакт не найден (удалён или нет доступа)"
REASON_NO_EMAIL = "у контакта нет email"
REASON_ALREADY = "уже получал это письмо"
REASON_DUPLICATE = "email уже в очереди по другой сделке"


@dataclass
class Options:
    contact_mode: str = "primary"
    check_dns: bool = False
    dedupe_email: bool = True
    campaign_key: str = ""
    bitrix_history_subject: str = ""  # искать прошлые письма в CRM по части темы
    extra_deal_fields: list[str] = field(default_factory=list)
    extra_contact_fields: list[str] = field(default_factory=list)


@dataclass
class Collected:
    total_by_api: int | None
    deals: list[dict]
    rows: list[dict]  # строки для таблицы recipients
    stats: dict


def contact_name(c: dict) -> str:
    return " ".join(x for x in (c.get("LAST_NAME"), c.get("NAME"), c.get("SECOND_NAME")) if x).strip()


def pick_email(contact: dict, check_dns: bool) -> tuple[str | None, str | None]:
    """(email, None) — первый валидный адрес (рабочий в приоритете), иначе (None, причина)."""
    emails = [e for e in contact.get("EMAIL") or [] if (e.get("VALUE") or "").strip()]
    if not emails:
        return None, REASON_NO_EMAIL
    emails.sort(key=lambda e: e.get("VALUE_TYPE") != "WORK")
    first_error = None
    for e in emails:
        err = validation.check(e["VALUE"], check_dns)
        if err is None:
            return validation.normalize(e["VALUE"]), None
        first_error = first_error or f"невалидный email {e['VALUE']!r}: {err}"
    return None, first_error


def collect(
    client: BitrixClient,
    storage: Storage,
    bitrix_filter: dict,
    opts: Options,
    progress: Callable[[str], None] = lambda s: None,
) -> Collected:
    if opts.contact_mode not in CONTACT_MODES:
        raise ValueError(f"contact_mode должен быть одним из: {', '.join(CONTACT_MODES)}")

    # 1. Все сделки по фильтру (все страницы)
    total = client.count("crm.deal.list", bitrix_filter)
    progress(f"По фильтру в Битрикс24: {total} сделок, загружаю все страницы…")
    select = list(dict.fromkeys(DEAL_SELECT + opts.extra_deal_fields))
    deals = []
    for d in client.list_all("crm.deal.list", {"filter": bitrix_filter, "select": select}):
        deals.append(d)
        if len(deals) % 500 == 0:
            progress(f"  загружено сделок: {len(deals)}")
    progress(f"Загружено сделок: {len(deals)}")
    if total is not None and total != len(deals):
        log.warning("API сообщил %s сделок, загружено %s (данные менялись во время выборки?)", total, len(deals))

    # 2. Связанные контакты каждой сделки (пакетами по 50)
    links: dict[str, list[dict]] = {}
    cmds = {f"d{d['ID']}": ("crm.deal.contact.items.get", {"id": d["ID"]}) for d in deals}
    res, errs = client.batch(cmds)
    for d in deals:
        items = res.get(f"d{d['ID']}") or []
        if not items and d.get("CONTACT_ID") and str(d["CONTACT_ID"]) != "0":
            items = [{"CONTACT_ID": d["CONTACT_ID"], "IS_PRIMARY": "Y", "SORT": 0}]
        items = sorted(items, key=lambda x: (x.get("IS_PRIMARY") != "Y", int(x.get("SORT") or 0)))
        if items and not any(x.get("IS_PRIMARY") == "Y" for x in items):
            items[0] = {**items[0], "IS_PRIMARY": "Y"}  # основной не отмечен — первый по сортировке
        links[str(d["ID"])] = items
    if errs:
        log.warning("не удалось получить контакты для %s сделок: %s", len(errs), list(errs)[:5])

    # 3. Карточки контактов (email)
    wanted_ids = set()
    for d in deals:
        items = links[str(d["ID"])]
        chosen = items[:1] if opts.contact_mode == "primary" else items
        wanted_ids.update(str(x["CONTACT_ID"]) for x in chosen)
    contacts: dict[str, dict] = {}
    ids = sorted(wanted_ids, key=int)
    cselect = list(dict.fromkeys(CONTACT_SELECT + opts.extra_contact_fields))
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        for c in client.list_all("crm.contact.list", {"filter": {"@ID": chunk}, "select": cselect}):
            contacts[str(c["ID"])] = c
    progress(f"Загружено контактов: {len(contacts)}")

    # 4. История отправок в самом Битрикс24 (необязательно)
    crm_history = _bitrix_history(client, opts.bitrix_history_subject) if opts.bitrix_history_subject else {}

    # 5. Строки очереди
    rows: list[dict] = []
    queued_emails: dict[str, str] = {}
    s = dict(deals_found=total if total is not None else len(deals), deals_loaded=len(deals),
             deals_with_contacts=0, deals_without_contacts=0, deals_with_valid_email=0, deals_without_email=0,
             deals_to_send=0, recipients_checked=0, no_valid_email=0, already_sent=0, duplicates=0, to_send=0)
    for d in deals:
        did = str(d["ID"])
        items = links[did]
        base = {"deal_id": int(did), "deal_title": d.get("TITLE")}
        if not items:
            s["deals_without_contacts"] += 1
            rows.append({**base, "status": "skipped", "skip_reason": REASON_NO_CONTACT})
            continue
        s["deals_with_contacts"] += 1
        chosen = items[:1] if opts.contact_mode == "primary" else items
        deal_has_valid = deal_queued = False
        for link in chosen:
            cid = str(link["CONTACT_ID"])
            c = contacts.get(cid)
            row = {**base, "contact_id": int(cid), "is_primary": link.get("IS_PRIMARY") == "Y"}
            s["recipients_checked"] += 1
            if c is None:
                s["no_valid_email"] += 1
                rows.append({**row, "status": "skipped", "skip_reason": REASON_CONTACT_MISSING})
                continue
            row["contact_name"] = contact_name(c)
            email, err = pick_email(c, opts.check_dns)
            if email is None:
                s["no_valid_email"] += 1
                reason = err if opts.contact_mode != "valid_only" else f"исключён (нет валидного email): {err}"
                rows.append({**row, "status": "skipped", "skip_reason": reason})
                continue
            deal_has_valid = True
            row["email"] = email
            row["context"] = {"deal": d, "contact": c}
            prev = storage.already_sent(opts.campaign_key, email) if opts.campaign_key else None
            if prev is not None or email in crm_history:
                s["already_sent"] += 1
                when = prev["sent_at"] if prev is not None else crm_history[email]
                rows.append({**row, "status": "skipped", "skip_reason": f"{REASON_ALREADY} ({when})"})
                continue
            if opts.dedupe_email and email in queued_emails:
                s["duplicates"] += 1
                rows.append({**row, "status": "skipped",
                             "skip_reason": f"{REASON_DUPLICATE} #{queued_emails[email]}"})
                continue
            queued_emails[email] = did
            deal_queued = True
            s["to_send"] += 1
            rows.append({**row, "status": "queued"})
        if deal_has_valid:
            s["deals_with_valid_email"] += 1
        else:
            s["deals_without_email"] += 1
        if deal_queued:
            s["deals_to_send"] += 1
    return Collected(total, deals, rows, s)


def _bitrix_history(client: BitrixClient, subject_part: str) -> dict[str, str]:
    """email → дата: исходящие письма в CRM, в теме которых есть subject_part."""
    found: dict[str, str] = {}
    flt = {"TYPE_ID": 4, "DIRECTION": 2, "%SUBJECT": subject_part}
    for a in client.list_all("crm.activity.list", {"filter": flt, "select": ["ID", "SUBJECT", "CREATED", "COMMUNICATIONS"]}):
        for c in a.get("COMMUNICATIONS") or []:
            e = validation.normalize(c.get("VALUE", ""))
            if e:
                found.setdefault(e, f"в CRM, дело #{a['ID']} от {a.get('CREATED', '')}")
    return found
