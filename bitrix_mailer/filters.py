"""Динамический фильтр сделок: человекочитаемые условия → фильтр crm.deal.list.

Условия задаются в файле кампании или в командной строке, например::

    Воронка = Холодная
    Стадия = Свободные
    Ответственный = Иван Иванов
    Сумма > 100000
    Дата создания = последние 30 дней
    Источник = Звонок, Сайт          (несколько значений → «любое из»)
    UF_CRM_1700000000 = Да           (любое поле сделки по коду…)
    Регион = Москва                  (…или по названию из crm.deal.fields)

Названия воронок, стадий, пользователей и значений списков переводятся в ID
через API (только чтение). Ничего не зашито в код: список полей берётся из
crm.deal.fields конкретного портала.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from .client import BitrixClient

OPERATORS = (">=", "<=", "!=", ">", "<", "=", "~")
BITRIX_PREFIX = {"=": "=", "!=": "!", ">": ">", ">=": ">=", "<": "<", "<=": "<=", "~": "%"}

ALIASES = {
    "CATEGORY_ID": ["воронка", "направление", "pipeline", "category"],
    "STAGE_ID": ["стадия", "стадия сделки", "stage"],
    "ASSIGNED_BY_ID": ["ответственный", "assigned", "responsible"],
    "OPPORTUNITY": ["сумма", "amount", "opportunity"],
    "DATE_CREATE": ["дата создания", "создана", "created"],
    "DATE_MODIFY": ["дата изменения", "изменена", "modified"],
    "CLOSEDATE": ["дата завершения", "предполагаемая дата закрытия", "closedate"],
    "TITLE": ["название", "title"],
    "SOURCE_ID": ["источник", "source"],
    "CURRENCY_ID": ["валюта", "currency"],
    "CLOSED": ["закрыта", "closed"],
    "TYPE_ID": ["тип", "тип сделки", "type"],
    "COMPANY_ID": ["компания", "company"],
    "CONTACT_ID": ["контакт", "contact"],
    "CREATED_BY_ID": ["кем создана", "создатель", "created by"],
}
_ALIAS_INDEX = {a: code for code, names in ALIASES.items() for a in names}

RELATIVE_RE = re.compile(r"^(?:последни[ехй]|last)\s+(\d+)\s*(?:дн\w*|день|days?)$", re.I)
RANGE_RE = re.compile(r"^(?:с|from)\s+(\S+)\s+(?:по|to)\s+(\S+)$", re.I)
EMPTY_WORDS = {"пусто", "не заполнено", "empty", "null"}


class FilterError(ValueError):
    pass


@dataclass
class Condition:
    field: str
    op: str
    value: Any  # str | list[str]

    def __str__(self) -> str:
        v = ", ".join(self.value) if isinstance(self.value, list) else self.value
        return f"{self.field} {self.op} {v}"


@dataclass
class ResolvedFilter:
    bitrix: dict = field(default_factory=dict)
    explained: list[str] = field(default_factory=list)  # «Воронка = Холодная → CATEGORY_ID=4»


def parse_expression(expr: str) -> Condition:
    """«Сумма>100000», «Воронка = Холодная», «Источник=Звонок,Сайт»."""
    best: tuple[int, str] | None = None
    for op in OPERATORS:  # самый левый оператор; при равенстве — двухсимвольный
        idx = expr.find(op)
        if idx > 0 and (best is None or idx < best[0]):
            best = (idx, op)
    if best:
        idx, op = best
        name, value = expr[:idx].strip(), expr[idx + len(op):].strip()
        return Condition(name, op, _split_values(value))
    raise FilterError(f"не понимаю условие «{expr}»: нужен оператор {' '.join(OPERATORS)}")


def conditions_from_mapping(mapping: dict) -> list[Condition]:
    """Секция [filter] файла кампании. Значение может начинаться с оператора."""
    out = []
    for name, raw in mapping.items():
        if name == "raw":
            continue
        if isinstance(raw, list):
            out.append(Condition(name, "=", [str(x) for x in raw]))
            continue
        s = str(raw).strip()
        op = "="
        for candidate in OPERATORS:
            if s.startswith(candidate):
                op, s = candidate, s[len(candidate):].strip()
                break
        out.append(Condition(name, op, _split_values(s)))
    return out


def _split_values(value: str) -> Any:
    parts = [p.strip() for p in value.split(",")] if "," in value and not RANGE_RE.match(value) else [value]
    parts = [p for p in parts if p]
    return parts if len(parts) > 1 else (parts[0] if parts else "")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower().replace("ё", "е"))


class FilterResolver:
    def __init__(self, client: BitrixClient, today: dt.date | None = None):
        self.client = client
        self.today = today or dt.date.today()
        self._fields: dict | None = None
        self._categories: dict[str, str] | None = None
        self._stages: dict[str, list[tuple[str, str]]] = {}
        self._users: list[dict] | None = None

    # --- справочники (только чтение) --------------------------------------
    def fields(self) -> dict:
        if self._fields is None:
            self._fields = self.client.call("crm.deal.fields") or {}
        return self._fields

    def categories(self) -> dict[str, str]:
        """ID воронки → название."""
        if self._categories is None:
            cats: dict[str, str] = {}
            try:
                res = self.client.call("crm.category.list", {"entityTypeId": 2}) or {}
                for c in res.get("categories", []):
                    cats[str(c["id"])] = c["name"]
            except Exception:  # старые порталы
                for c in self.client.call("crm.dealcategory.list", {}) or []:
                    cats[str(c["ID"])] = c["NAME"]
            cats.setdefault("0", "Общая")
            self._categories = cats
        return self._categories

    def stages(self, category_id: str) -> list[tuple[str, str]]:
        """[(STATUS_ID, NAME)] стадий воронки."""
        if category_id not in self._stages:
            entity = "DEAL_STAGE" if category_id == "0" else f"DEAL_STAGE_{category_id}"
            rows = self.client.call("crm.status.list", {"filter": {"ENTITY_ID": entity}}) or []
            self._stages[category_id] = [(r["STATUS_ID"], r["NAME"]) for r in rows]
        return self._stages[category_id]

    def users(self) -> list[dict]:
        if self._users is None:
            users, start = [], 0
            while True:
                data = self.client.call_raw("user.get", {"start": start})
                users.extend(data.get("result") or [])
                if data.get("next") is None:
                    break
                start = int(data["next"])
            self._users = users
        return self._users

    # --- разрешение условий -----------------------------------------------
    def field_code(self, name: str) -> str:
        n = _norm(name)
        if n in _ALIAS_INDEX:
            return _ALIAS_INDEX[n]
        fields = self.fields()
        if name.upper() in fields:
            return name.upper()
        for code, meta in fields.items():
            labels = {meta.get(k) for k in ("title", "formLabel", "listLabel", "filterLabel")}
            if n in {_norm(x) for x in labels if x}:
                return code
        raise FilterError(
            f"поле «{name}» не найдено среди полей сделки; "
            "список полей: python -m bitrix_mailer fields"
        )

    def resolve(self, conditions: list[Condition], raw: dict | None = None) -> ResolvedFilter:
        result = ResolvedFilter()
        cat_ids: list[str] | None = None
        # Воронку разрешаем первой: от неё зависят коды стадий.
        ordered = sorted(conditions, key=lambda c: self.field_code(c.field) != "CATEGORY_ID")
        for cond in ordered:
            code = self.field_code(cond.field)
            values = cond.value if isinstance(cond.value, list) else [cond.value]
            if code == "CATEGORY_ID":
                ids = [self._category_id(v) for v in values]
                if cond.op == "=":
                    cat_ids = ids
                self._put(result, cond, code, ids)
            elif code == "STAGE_ID":
                ids = [sid for v in values for sid in self._stage_ids(v, cat_ids)]
                self._put(result, cond, code, ids)
            elif code in ("ASSIGNED_BY_ID", "CREATED_BY_ID", "MODIFY_BY_ID"):
                self._put(result, cond, code, [self._user_id(v) for v in values])
            elif self._is_date(code):
                self._put_date(result, cond, code, values)
            else:
                self._put(result, cond, code, [self._scalar(code, v) for v in values])
        for k, v in (raw or {}).items():
            result.bitrix[k] = v
            result.explained.append(f"(как есть) {k} = {v}")
        return result

    def _put(self, result: ResolvedFilter, cond: Condition, code: str, ids: list) -> None:
        if len(ids) == 1 and isinstance(ids[0], str) and _norm(ids[0]) in EMPTY_WORDS:
            ids = [""]
        if len(ids) > 1:
            if cond.op not in ("=", "!="):
                raise FilterError(f"{cond}: список значений допустим только с = или !=")
            key = ("!" if cond.op == "!=" else "") + code
            value: Any = ids
        else:
            key = BITRIX_PREFIX[cond.op] + code
            value = ids[0]
            if cond.op == "~":
                value = str(value)
        if key in result.bitrix:
            raise FilterError(f"условие на {code} с оператором {cond.op} задано дважды")
        result.bitrix[key] = value
        result.explained.append(f"{cond} → {key} = {value}")

    def _put_date(self, result: ResolvedFilter, cond: Condition, code: str, values: list[str]) -> None:
        if len(values) != 1:
            raise FilterError(f"{cond}: для даты нужно одно значение")
        v = values[0].strip()
        m = RELATIVE_RE.match(v)
        if m:
            since = self.today - dt.timedelta(days=int(m.group(1)))
            self._set(result, cond, ">=" + code, since.isoformat() + "T00:00:00")
            return
        m = RANGE_RE.match(v)
        if m:
            self._set(result, cond, ">=" + code, _date(m.group(1)) + "T00:00:00")
            self._set(result, cond, "<=" + code, _date(m.group(2)) + "T23:59:59")
            return
        if _norm(v) in ("сегодня", "today"):
            self._set(result, cond, ">=" + code, self.today.isoformat() + "T00:00:00")
            return
        d = _date(v)
        if cond.op == "=":
            self._set(result, cond, ">=" + code, d + "T00:00:00")
            self._set(result, cond, "<=" + code, d + "T23:59:59")
        elif cond.op in (">", "<="):
            self._set(result, cond, BITRIX_PREFIX[cond.op] + code, d + "T23:59:59")
        else:
            self._set(result, cond, BITRIX_PREFIX[cond.op] + code, d + "T00:00:00")

    def _set(self, result: ResolvedFilter, cond: Condition, key: str, value: Any) -> None:
        if key in result.bitrix:
            raise FilterError(f"условие {key} задано дважды")
        result.bitrix[key] = value
        result.explained.append(f"{cond} → {key} = {value}")

    def _is_date(self, code: str) -> bool:
        return self.fields().get(code, {}).get("type") in ("date", "datetime") or code in (
            "DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "BEGINDATE")

    def _category_id(self, value: str) -> str:
        cats = self.categories()
        if str(value).isdigit() and str(value) in cats:
            return str(value)
        matches = [cid for cid, name in cats.items() if _norm(name) == _norm(value)]
        if not matches:
            raise FilterError(f"воронка «{value}» не найдена. Есть: {', '.join(cats.values())}")
        return matches[0]

    def _stage_ids(self, value: str, cat_ids: list[str] | None) -> list[str]:
        cats = cat_ids or list(self.categories())
        found, known = [], []
        for cid in cats:
            for sid, name in self.stages(cid):
                known.append(name)
                if _norm(name) == _norm(value) or sid == value:
                    found.append(sid)
        if not found:
            raise FilterError(f"стадия «{value}» не найдена. Есть: {', '.join(dict.fromkeys(known))}")
        return found

    def _user_id(self, value: str) -> str:
        if str(value).isdigit():
            return str(value)
        n = _norm(value)
        matches = []
        for u in self.users():
            first, last = u.get("NAME") or "", u.get("LAST_NAME") or ""
            variants = {_norm(f"{first} {last}"), _norm(f"{last} {first}"), _norm(u.get("EMAIL") or "")}
            if n in variants:
                matches.append(str(u["ID"]))
        if not matches:
            raise FilterError(f"пользователь «{value}» не найден")
        if len(matches) > 1:
            raise FilterError(f"найдено несколько пользователей «{value}» (ID {', '.join(matches)}), укажите ID")
        return matches[0]

    def _scalar(self, code: str, value: str) -> Any:
        meta = self.fields().get(code, {})
        items = meta.get("items") or []
        if items:  # список: значение по названию
            for it in items:
                if _norm(it.get("VALUE", "")) == _norm(value) or str(it.get("ID")) == str(value):
                    return str(it["ID"])
            raise FilterError(f"значение «{value}» не найдено в списке поля {code}")
        if code == "SOURCE_ID" or meta.get("type") == "crm_status":
            entity = meta.get("statusType") or "SOURCE"
            for r in self.client.call("crm.status.list", {"filter": {"ENTITY_ID": entity}}) or []:
                if _norm(r["NAME"]) == _norm(value) or r["STATUS_ID"] == value:
                    return r["STATUS_ID"]
        if meta.get("type") == "boolean" or code == "CLOSED":
            if _norm(value) in ("да", "yes", "y", "1", "true"):
                return "Y" if code == "CLOSED" else "1"
            if _norm(value) in ("нет", "no", "n", "0", "false"):
                return "N" if code == "CLOSED" else "0"
        if meta.get("type") in ("double", "integer", "money") or code == "OPPORTUNITY":
            num = str(value).replace(" ", "").replace(" ", "").replace(",", ".")
            try:
                float(num)
            except ValueError as e:
                raise FilterError(f"{code}: «{value}» не число") from e
            return num
        return value


def _date(s: str) -> str:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    raise FilterError(f"не понимаю дату «{s}» (ожидается ГГГГ-ММ-ДД, ДД.ММ.ГГГГ или «последние N дней»)")
