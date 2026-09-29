"""Имитация REST API Битрикс24 для тестов (без сети)."""

from __future__ import annotations

import re
import urllib.parse

PAGE = 50


def _match(row: dict, flt: dict) -> bool:
    for key, want in flt.items():
        m = re.match(r"^(>=|<=|!=|=|!|>|<|@|%)?(.+)$", key)
        op, field = m.group(1) or "=", m.group(2)
        have = row.get(field)
        if op == "%":
            if str(want).lower() not in str(have or "").lower():
                return False
            continue
        if isinstance(want, list) or op == "@":
            vals = [str(x) for x in (want if isinstance(want, list) else [want])]
            hit = str(have) in vals
            if (op == "!") == hit:
                return False
            continue
        if op in (">", ">=", "<", "<="):
            try:
                a, b = float(have), float(want)
            except (TypeError, ValueError):
                a, b = str(have), str(want)
            if not {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]:
                return False
            continue
        if (str(have) == str(want)) == (op == "!"):
            return False
    return True


def _unflatten(qs: str) -> dict:
    out: dict = {}
    for k, v in urllib.parse.parse_qsl(qs, keep_blank_values=True):
        parts = re.findall(r"[^\[\]]+", k)
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return out


class FakeBitrix:
    def __init__(self):
        self.deals: list[dict] = []
        self.contacts: dict[int, dict] = {}
        self.links: dict[int, list[dict]] = {}
        self.activities: dict[int, dict] = {}
        self.comments: dict[int, dict] = {}
        self.calls: list[str] = []
        self.categories = [{"id": 0, "name": "Общая"}, {"id": 4, "name": "Холодная"}]
        self.stages = {
            "DEAL_STAGE": [{"STATUS_ID": "NEW", "NAME": "Новая"}],
            "DEAL_STAGE_4": [{"STATUS_ID": "C4:NEW", "NAME": "Свободные"},
                             {"STATUS_ID": "C4:WORK", "NAME": "В работе"}],
        }
        self.users = [{"ID": "7", "NAME": "Иван", "LAST_NAME": "Иванов"},
                      {"ID": "8", "NAME": "Пётр", "LAST_NAME": "Петров"}]
        self.fields = {
            "ID": {"type": "integer", "title": "ID"},
            "TITLE": {"type": "string", "title": "Название"},
            "OPPORTUNITY": {"type": "double", "title": "Сумма"},
            "DATE_CREATE": {"type": "datetime", "title": "Дата создания"},
            "UF_CRM_REGION": {"type": "enumeration", "title": "UF_CRM_REGION", "formLabel": "Регион",
                              "items": [{"ID": "11", "VALUE": "Москва"}, {"ID": "12", "VALUE": "Казань"}]},
        }

    def _list(self, rows, params):
        flt = params.get("filter") or {}
        rows = sorted((r for r in rows if _match(r, flt)), key=lambda r: int(r["ID"]))
        start = int(params.get("start", 0))
        sel = params.get("select") or []
        pick = (lambda r: {k: r.get(k) for k in sel}) if sel else (lambda r: dict(r))
        if start == -1:
            return {"result": [pick(r) for r in rows[:PAGE]]}
        page = rows[start : start + PAGE]
        res = {"result": [pick(r) for r in page], "total": len(rows)}
        if start + PAGE < len(rows):
            res["next"] = start + PAGE
        return res

    def __call__(self, method: str, params: dict) -> dict:
        self.calls.append(method)
        if method == "batch":
            result, errors = {}, {}
            for key, cmd in params["cmd"].items():
                m, _, qs = cmd.partition("?")
                r = self(m, _unflatten(qs))
                if "error" in r:
                    errors[key] = r
                else:
                    result[key] = r["result"]
            return {"result": {"result": result, "result_error": errors}}
        if method == "crm.deal.list":
            return self._list(self.deals, params)
        if method == "crm.contact.list":
            return self._list(list(self.contacts.values()), params)
        if method == "crm.deal.contact.items.get":
            return {"result": self.links.get(int(params["id"]), [])}
        if method == "crm.category.list":
            return {"result": {"categories": self.categories}}
        if method == "crm.status.list":
            return {"result": self.stages.get(params["filter"]["ENTITY_ID"], [])}
        if method == "crm.deal.fields":
            return {"result": self.fields}
        if method == "user.get":
            return {"result": self.users}
        if method == "crm.deal.get":
            d = next((x for x in self.deals if x["ID"] == str(params["id"])), None)
            return {"result": dict(d)} if d else {"error": "NOT_FOUND", "error_description": "Not found"}
        if method == "crm.deal.update":
            d = next((x for x in self.deals if x["ID"] == str(params["id"])), None)
            if d is None:
                return {"error": "NOT_FOUND", "error_description": "Not found"}
            d.update({k: str(v) for k, v in params["fields"].items()})
            return {"result": True}
        if method == "crm.activity.add":
            aid = len(self.activities) + 1000
            self.activities[aid] = {"ID": str(aid), **{k: str(v) if not isinstance(v, (list, dict)) else v
                                                     for k, v in params["fields"].items()}}
            return {"result": aid}
        if method == "crm.activity.get":
            return {"result": self.activities.get(int(params["id"]))}
        if method == "crm.activity.list":
            return self._list(list(self.activities.values()), params)
        if method == "crm.timeline.comment.add":
            cid = len(self.comments) + 500
            self.comments[cid] = {"ID": str(cid), **params["fields"]}
            return {"result": cid}
        if method == "crm.timeline.comment.get":
            return {"result": self.comments.get(int(params["id"]))}
        return {"error": "ERROR_METHOD_NOT_FOUND", "error_description": method}


def cold_portal() -> FakeBitrix:
    """80 сделок «Холодная / Свободные»: 4 без контакта, 3 без валидного email,
    остальные 73 — с email; + 30 сделок в другой стадии."""
    fb = FakeBitrix()
    for i in range(1, 81):
        deal = {"ID": str(i), "TITLE": f"Сделка {i}", "CATEGORY_ID": "4", "STAGE_ID": "C4:NEW",
                "ASSIGNED_BY_ID": "7" if i % 2 else "8", "OPPORTUNITY": str(i * 5000),
                "CURRENCY_ID": "RUB", "CONTACT_ID": None, "DATE_CREATE": "2026-09-01T10:00:00+03:00",
                "UF_CRM_REGION": "11"}
        fb.deals.append(deal)
        if i <= 4:
            continue  # без контакта
        cid = 1000 + i
        email = [{"VALUE": f"client{i}@example.com", "VALUE_TYPE": "WORK"}]
        if i in (5, 6):
            email = []
        if i == 7:
            email = [{"VALUE": "broken@", "VALUE_TYPE": "WORK"}]
        fb.contacts[cid] = {"ID": str(cid), "NAME": f"Имя{i}", "LAST_NAME": f"Фамилия{i}", "EMAIL": email}
        deal["CONTACT_ID"] = str(cid)
        fb.links[i] = [{"CONTACT_ID": cid, "SORT": 10, "IS_PRIMARY": "Y"}]
    for i in range(81, 111):
        fb.deals.append({"ID": str(i), "TITLE": f"Другая {i}", "CATEGORY_ID": "4", "STAGE_ID": "C4:WORK",
                         "ASSIGNED_BY_ID": "7", "OPPORTUNITY": "1", "CONTACT_ID": None})
    return fb
