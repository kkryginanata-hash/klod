"""Аудит контактных данных по сделкам: у каких компаний и их контактов нет ни почты, ни телефона.

Только чтение: сделки по фильтру → компании сделок → контакты компании и сделки →
email/телефон. Компания считается «без связи», если ни в её карточке, ни у одного
её контакта нет валидного email и телефона.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import validation
from .client import BitrixClient
from .recipients import contact_name

DEAL_SELECT = ["ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID"]
COMPANY_SELECT = ["ID", "TITLE", "EMAIL", "PHONE", "WEB", "INDUSTRY", "ASSIGNED_BY_ID"]
CONTACT_SELECT = ["ID", "NAME", "LAST_NAME", "SECOND_NAME", "POST", "EMAIL", "PHONE"]

STATUS_NONE = "нет ни почты, ни телефона"
STATUS_NO_EMAIL = "только телефон"
STATUS_NO_PHONE = "только почта"
STATUS_OK = "есть почта и телефон"
STATUSES = (STATUS_NONE, STATUS_NO_EMAIL, STATUS_NO_PHONE, STATUS_OK)


def valid_emails(entity: dict) -> list[str]:
    out = []
    for e in entity.get("EMAIL") or []:
        v = (e.get("VALUE") or "").strip()
        if v and validation.syntax_error(v) is None:
            out.append(validation.normalize(v))
    return out


def valid_phones(entity: dict) -> list[str]:
    """Телефоны, в которых не меньше 7 цифр (отсекает «-», «нет», «0» и т.п.)."""
    out = []
    for p in entity.get("PHONE") or []:
        v = (p.get("VALUE") or "").strip()
        if len(re.sub(r"\D", "", v)) >= 7:
            out.append(v)
    return out


def _status(has_email: bool, has_phone: bool) -> str:
    if has_email and has_phone:
        return STATUS_OK
    if has_email:
        return STATUS_NO_PHONE
    if has_phone:
        return STATUS_NO_EMAIL
    return STATUS_NONE


@dataclass
class AuditResult:
    deals: int
    rows: list[dict]          # одна строка на компанию (или на сделку без компании)
    stats: dict
    industries: Counter


def _load_by_ids(client: BitrixClient, method: str, ids: set[str], select: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    ordered = sorted(ids, key=int)
    for i in range(0, len(ordered), 50):
        for row in client.list_all(method, {"filter": {"@ID": ordered[i : i + 50]}, "select": select}):
            out[str(row["ID"])] = row
    return out


def _links(client: BitrixClient, method: str, ids: list[str]) -> dict[str, list[str]]:
    res, _ = client.batch({f"x{i}": (method, {"id": i}) for i in ids})
    return {i: [str(x["CONTACT_ID"]) for x in res.get(f"x{i}") or []] for i in ids}


def run(client: BitrixClient, bitrix_filter: dict,
        progress: Callable[[str], None] = lambda s: None) -> AuditResult:
    deals = list(client.list_all("crm.deal.list", {"filter": bitrix_filter, "select": DEAL_SELECT}))
    progress(f"Загружено сделок: {len(deals)}")
    deal_ids = [str(d["ID"]) for d in deals]
    deal_contacts = _links(client, "crm.deal.contact.items.get", deal_ids)
    for d in deals:
        cid = str(d.get("CONTACT_ID") or "0")
        if not deal_contacts[str(d["ID"])] and cid != "0":
            deal_contacts[str(d["ID"])] = [cid]

    company_ids = {str(d["COMPANY_ID"]) for d in deals if str(d.get("COMPANY_ID") or "0") != "0"}
    companies = _load_by_ids(client, "crm.company.list", company_ids, COMPANY_SELECT)
    company_contacts = _links(client, "crm.company.contact.items.get", sorted(company_ids, key=int))
    progress(f"Загружено компаний: {len(companies)}")

    contact_ids = {c for ids in deal_contacts.values() for c in ids}
    contact_ids |= {c for ids in company_contacts.values() for c in ids}
    contacts = _load_by_ids(client, "crm.contact.list", contact_ids, CONTACT_SELECT)
    progress(f"Загружено контактов: {len(contacts)}")

    # Группируем сделки по компании; сделки без компании — каждая отдельной строкой.
    groups: dict[str, dict] = {}
    for d in deals:
        did = str(d["ID"])
        comp = str(d.get("COMPANY_ID") or "0")
        key = f"c{comp}" if comp != "0" else f"d{did}"
        g = groups.setdefault(key, {"company_id": comp if comp != "0" else "", "deals": [], "contacts": []})
        g["deals"].append(d)
        g["contacts"] += deal_contacts[did]
        if comp != "0":
            g["contacts"] += company_contacts.get(comp, [])

    industry_names = {str(x.get("STATUS_ID")): x.get("NAME", "")
                      for x in client.call("crm.status.list", {"filter": {"ENTITY_ID": "INDUSTRY"}}) or []}

    rows: list[dict] = []
    industries: Counter = Counter()
    for g in groups.values():
        comp = companies.get(g["company_id"]) if g["company_id"] else None
        cids = list(dict.fromkeys(g["contacts"]))
        found = [contacts[c] for c in cids if c in contacts]
        c_emails = [e for c in found for e in valid_emails(c)]
        c_phones = [p for c in found for p in valid_phones(c)]
        comp_emails = valid_emails(comp) if comp else []
        comp_phones = valid_phones(comp) if comp else []
        industry = industry_names.get(str((comp or {}).get("INDUSTRY") or ""), (comp or {}).get("INDUSTRY") or "")
        if comp:
            industries[industry or "не указана"] += 1
        d0 = g["deals"][0]
        rows.append({
            "company_id": g["company_id"],
            "company": (comp or {}).get("TITLE", "") if g["company_id"] else "(сделка без компании)",
            "company_missing": bool(g["company_id"]) and comp is None,
            "web": ", ".join(w.get("VALUE", "") for w in (comp or {}).get("WEB") or []),
            "industry": industry,
            "deal_ids": ", ".join(str(d["ID"]) for d in g["deals"]),
            "deal_title": d0.get("TITLE", ""),
            "stage": d0.get("STAGE_ID", ""),
            "assigned_by_id": d0.get("ASSIGNED_BY_ID", ""),
            "contacts": len(cids),
            "contact_names": "; ".join(contact_name(c) or f"#{c['ID']}" for c in found),
            "company_emails": ", ".join(comp_emails),
            "company_phones": ", ".join(comp_phones),
            "contact_emails": ", ".join(dict.fromkeys(c_emails)),
            "contact_phones": ", ".join(dict.fromkeys(c_phones)),
            "status": _status(bool(comp_emails or c_emails), bool(comp_phones or c_phones)),
        })

    with_company = [r for r in rows if r["company_id"]]
    stats = {
        "deals": len(deals),
        "companies": len(with_company),
        "deals_without_company": len(rows) - len(with_company),
        "companies_without_contacts": sum(1 for r in with_company if not r["contacts"] and r["status"] == STATUS_NONE),
        **{s: sum(1 for r in with_company if r["status"] == s) for s in STATUSES},
        "no_company_none": sum(1 for r in rows if not r["company_id"] and r["status"] == STATUS_NONE),
    }
    return AuditResult(len(deals), rows, stats, industries)


def format_result(res: AuditResult, top: int = 30) -> str:
    s = res.stats
    lines = [
        f"Сделок по фильтру:                     {s['deals']}",
        f"Компаний в этих сделках:               {s['companies']}",
        f"  — нет ни почты, ни телефона:         {s[STATUS_NONE]}  (из них без единого контакта: "
        f"{s['companies_without_contacts']})",
        f"  — только телефон, нет почты:         {s[STATUS_NO_EMAIL]}",
        f"  — только почта, нет телефона:        {s[STATUS_NO_PHONE]}",
        f"  — есть и почта, и телефон:           {s[STATUS_OK]}",
        f"Сделок без компании:                   {s['deals_without_company']}"
        f"  (из них без почты и телефона у контактов: {s['no_company_none']})",
    ]
    none = [r for r in res.rows if r["company_id"] and r["status"] == STATUS_NONE]
    if none:
        lines.append(f"\nКомпании без почты и телефона (первые {min(top, len(none))} из {len(none)}):")
        for r in none[:top]:
            who = r["contact_names"] or "контактов нет"
            lines.append(f"  #{r['company_id']:<6} {r['company'][:50]:<50} сделки {r['deal_ids']}; {who}")
    if res.industries:
        lines.append("\nОтрасли компаний (поле «Сфера деятельности»):")
        for name, n in res.industries.most_common(15):
            lines.append(f"  {n:>5}  {name}")
    return "\n".join(lines)


def export_csv(res: AuditResult, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(res.rows[0]) if res.rows else ["company_id", "status"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        w.writeheader()
        order = {s: i for i, s in enumerate(STATUSES)}
        w.writerows(sorted(res.rows, key=lambda r: (order[r["status"]], r["company"])))
    return path
