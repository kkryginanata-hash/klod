"""Изменение полей существующих сделок по просьбе пользователя.

Порядок тот же, что у рассылки: план (что и как изменится) → подтверждение →
применение → журнал. Для каждого изменения сохраняются старые значения, поэтому
пакет можно откатить.

Изменения задаются так же, как фильтр, но только оператором «=»::

    Стадия = В работе
    Ответственный = Пётр Петров
    Сумма = 150000
    Регион = Казань

Создавать/удалять сущности, менять воронку, контакты и компанию сделки нельзя —
это блокирует клиент API (``client.DEAL_UPDATE_FORBIDDEN_FIELDS``).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .client import DEAL_UPDATE_FORBIDDEN_FIELDS, BitrixClient
from .filters import EMPTY_WORDS, Condition, FilterError, FilterResolver, _date, _norm, parse_expression
from .storage import Storage, now

FIELD_TITLES = {"STAGE_ID": "Стадия", "ASSIGNED_BY_ID": "Ответственный", "OPPORTUNITY": "Сумма",
                "TITLE": "Название", "CLOSEDATE": "Дата завершения", "SOURCE_ID": "Источник"}
DEAL_BASE_SELECT = ["ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "ASSIGNED_BY_ID"]


def parse_changes(items: list[str] | dict) -> list[Condition]:
    """«Стадия=В работе» (строки из --set) или секция [after_send] файла кампании."""
    if isinstance(items, dict):
        conds = [Condition(k, "=", str(v)) for k, v in items.items()]
    else:
        conds = [parse_expression(x) for x in items]
    for c in conds:
        if c.op != "=" or isinstance(c.value, list):
            raise FilterError(f"изменение «{c}»: нужно одно значение через «=», например «Стадия = В работе»")
    if not conds:
        raise FilterError("не указано, что менять (например --set \"Стадия=В работе\")")
    return conds


class ChangePlanner:
    def __init__(self, resolver: FilterResolver):
        self.r = resolver

    def codes(self, conds: list[Condition]) -> list[str]:
        codes = []
        for c in conds:
            code = self.r.field_code(c.field)
            if code in DEAL_UPDATE_FORBIDDEN_FIELDS:
                raise FilterError(f"поле «{c.field}» ({code}) менять запрещено")
            if code in codes:
                raise FilterError(f"поле «{c.field}» указано дважды")
            codes.append(code)
        return codes

    def display(self, code: str, value: Any, deal: dict) -> str:
        if value in (None, ""):
            return "—"
        if code == "STAGE_ID":
            for sid, name in self.r.stages(str(deal.get("CATEGORY_ID") or "0")):
                if sid == value:
                    return name
        if code in ("ASSIGNED_BY_ID", "CREATED_BY_ID"):
            for u in self.r.users():
                if str(u["ID"]) == str(value):
                    return f"{u.get('NAME', '')} {u.get('LAST_NAME', '')}".strip()
        items = self.r.fields().get(code, {}).get("items") or []
        for it in items:
            if str(it.get("ID")) == str(value):
                return it.get("VALUE", str(value))
        return str(value)

    def value_for(self, code: str, raw: str, deal: dict) -> Any:
        """Значение для конкретной сделки (стадия ищется в воронке этой сделки)."""
        if _norm(raw) in EMPTY_WORDS:
            return ""
        if code == "STAGE_ID":
            cat = str(deal.get("CATEGORY_ID") or "0")
            for sid, name in self.r.stages(cat):
                if _norm(name) == _norm(raw) or sid == raw:
                    return sid
            cat_name = self.r.categories().get(cat, cat)
            raise FilterError(f"в воронке «{cat_name}» нет стадии «{raw}»")
        if code in ("ASSIGNED_BY_ID",):
            return self.r._user_id(raw)
        if self.r._is_date(code):
            return _date(raw)
        return self.r._scalar(code, raw)

    def plan(self, deals: list[dict], conds: list[Condition]) -> list[dict]:
        codes = self.codes(conds)
        rows = []
        for d in deals:
            row = {"deal_id": int(d["ID"]), "deal_title": d.get("TITLE"), "fields": {}, "before": {}}
            labels, same = [], []
            try:
                for code, c in zip(codes, conds):
                    new = self.value_for(code, str(c.value), d)
                    old = d.get(code)
                    title = FIELD_TITLES.get(code) or c.field
                    if _same(old, new):
                        same.append(title)
                        continue
                    row["fields"][code] = new
                    row["before"][code] = "" if old is None else old
                    labels.append(f"{title}: {self.display(code, old, d)} → {self.display(code, new, d)}")
            except FilterError as e:
                rows.append({**row, "status": "skipped", "skip_reason": str(e), "label": ""})
                continue
            if not row["fields"]:
                rows.append({**row, "status": "skipped", "label": "",
                             "skip_reason": "уже в нужном состоянии (" + ", ".join(same) + ")"})
                continue
            rows.append({**row, "status": "planned", "label": "; ".join(labels), "skip_reason": None})
        return rows


def _same(old: Any, new: Any) -> bool:
    old = "" if old is None else str(old)
    new = "" if new is None else str(new)
    if old == new:
        return True
    try:
        return float(old) == float(new)
    except ValueError:
        pass
    # Даты: Битрикс24 отдаёт «2026-09-01T00:00:00+03:00», задаём «2026-09-01»
    return len(new) == 10 and old[:10] == new and old[10:11] == "T"


def load_deals(client: BitrixClient, bitrix_filter: dict, codes: list[str]) -> tuple[int | None, list[dict]]:
    total = client.count("crm.deal.list", bitrix_filter)
    select = list(dict.fromkeys(DEAL_BASE_SELECT + codes))
    return total, list(client.list_all("crm.deal.list", {"filter": bitrix_filter, "select": select}))


# --- применение и откат ----------------------------------------------------

def apply_one(client: BitrixClient, storage: Storage, row) -> tuple[bool, str]:
    """Применить одно запланированное изменение. Перед записью перечитывает сделку,
    чтобы в журнале остались актуальные старые значения (для отката)."""
    fields = json.loads(row["fields_json"])
    try:
        current = client.call("crm.deal.get", {"id": row["deal_id"]}) or {}
        if not current:
            raise RuntimeError("сделка не найдена")
        before = {k: ("" if current.get(k) is None else current.get(k)) for k in fields}
        client.call("crm.deal.update", {"id": row["deal_id"], "fields": fields,
                                        "params": {"REGISTER_SONET_EVENT": "Y"}})
    except Exception as e:  # noqa: BLE001 — ошибка одной сделки не останавливает пакет
        storage.update_deal_change(row["id"], status="failed", error=str(e)[:500])
        return False, str(e)
    storage.update_deal_change(row["id"], status="applied", before_json=json.dumps(before, ensure_ascii=False),
                               applied_at=now(), error=None)
    return True, ""


def apply_batch(client: BitrixClient, storage: Storage, batch_id: int, *, confirmed: bool,
                progress: Callable[[str], None] = print) -> dict:
    if not confirmed:
        raise PermissionError("изменения не подтверждены")
    rows = storage.deal_changes(batch_id, "planned")
    progress(f"Изменяю сделок: {len(rows)}")
    for i, row in enumerate(rows, 1):
        ok, err = apply_one(client, storage, row)
        progress(f"  [{i}/{len(rows)}] {'✓' if ok else '✗'} сделка #{row['deal_id']}: {row['label'] if ok else err}")
    storage.set_update_status(batch_id, "applied")
    return storage.change_counts(batch_id)


def undo_batch(client: BitrixClient, storage: Storage, batch_id: int, *, confirmed: bool,
               progress: Callable[[str], None] = print) -> dict:
    """Вернуть старые значения. Если поле после нас меняли — не трогаем."""
    if not confirmed:
        raise PermissionError("откат не подтверждён")
    rows = storage.deal_changes(batch_id, "applied")
    progress(f"Откатываю изменений: {len(rows)}")
    for row in rows:
        new, before = json.loads(row["fields_json"]), json.loads(row["before_json"])
        try:
            current = client.call("crm.deal.get", {"id": row["deal_id"]}) or {}
            changed = [k for k, v in new.items() if not _same(current.get(k), v)]
            if changed:
                storage.update_deal_change(row["id"], error=f"не откачено: поля {', '.join(changed)} уже изменены после нас")
                progress(f"  – сделка #{row['deal_id']}: пропущена, поля изменены после нас")
                continue
            client.call("crm.deal.update", {"id": row["deal_id"], "fields": before,
                                            "params": {"REGISTER_SONET_EVENT": "Y"}})
        except Exception as e:  # noqa: BLE001
            storage.update_deal_change(row["id"], error=f"откат: {e}"[:500])
            progress(f"  ✗ сделка #{row['deal_id']}: {e}")
            continue
        storage.update_deal_change(row["id"], status="undone", error=None)
        progress(f"  ✓ сделка #{row['deal_id']} возвращена")
    storage.set_update_status(batch_id, "undone")
    return storage.change_counts(batch_id)


def format_plan(storage: Storage, batch_id: int, limit: int = 20) -> str:
    b = storage.update_batch(batch_id)
    info = json.loads(b["info_json"])
    counts = storage.change_counts(batch_id)
    lines = [f"Изменение сделок #{batch_id}" + (f" (после отправки кампании #{b['campaign_id']})" if b["campaign_id"] else "")]
    if info.get("filter"):
        lines += ["Фильтр:"] + [f"  • {x}" for x in info["filter"]]
    lines += ["Что изменить:"] + [f"  • {x}" for x in info["set"]]
    if info.get("deals_found") is not None:
        lines.append(f"\nНайдено сделок:          {info['deals_found']}")
    lines += [f"Будет изменено:          {counts.get('planned', 0) + counts.get('applied', 0) + counts.get('failed', 0) + counts.get('undone', 0)}",
              f"Без изменений/пропуск:   {counts.get('skipped', 0)}"]
    planned = storage.deal_changes(batch_id, "planned")
    if planned:
        lines.append("\nПримеры:")
        lines += [f"  сделка #{r['deal_id']} «{r['deal_title']}»: {r['label']}" for r in planned[:limit]]
        if len(planned) > limit:
            lines.append(f"  … и ещё {len(planned) - limit}")
    reasons: dict[str, int] = {}
    for r in storage.deal_changes(batch_id, "skipped"):
        key = (r["skip_reason"] or "").split(" (")[0]
        reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        lines += ["\nПропущены:"] + [f"  – {k}: {v}" for k, v in reasons.items()]
    return "\n".join(lines)


def format_result(storage: Storage, batch_id: int) -> str:
    b = storage.update_batch(batch_id)
    c = storage.change_counts(batch_id)
    lines = [f"Изменение сделок #{batch_id} — статус: {b['status']}",
             f"Изменено:        {c.get('applied', 0)}",
             f"Ошибки:          {c.get('failed', 0)}",
             f"Откачено:        {c.get('undone', 0)}",
             f"Ожидают:         {c.get('planned', 0)}",
             f"Пропущено:       {c.get('skipped', 0)}"]
    bad = [r for r in storage.deal_changes(batch_id) if r["error"]]
    if bad:
        lines += ["\nТребуют внимания:"] + [f"  сделка #{r['deal_id']}: {r['error']}" for r in bad[:50]]
    return "\n".join(lines)
