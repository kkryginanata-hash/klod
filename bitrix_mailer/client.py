"""REST-клиент Битрикс24 (входящий вебхук) с защитой от изменения CRM.

Защита работает на уровне клиента: любой метод, которого нет в белом списке,
отклоняется ещё до отправки HTTP-запроса. Разрешены только чтение и два вида
записи в Timeline существующих сущностей:

* ``crm.activity.add`` — только исходящее письмо (TYPE_ID=4, DIRECTION=2),
  привязанное к существующей сделке или контакту;
* ``crm.timeline.comment.add`` — только комментарий к сделке или контакту.

Создание лидов/сделок/контактов/компаний, смена стадии, воронки и
ответственного невозможны: методы ``*.add``/``*.update``/``*.delete`` для этих
сущностей в список не входят.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)

READ_METHODS = frozenset(
    {
        "profile",
        "user.get",
        "crm.deal.list",
        "crm.deal.get",
        "crm.deal.fields",
        "crm.deal.contact.items.get",
        "crm.contact.list",
        "crm.contact.get",
        "crm.category.list",
        "crm.dealcategory.list",
        "crm.status.list",
        "crm.activity.list",
        "crm.activity.get",
        "crm.timeline.comment.get",
        "crm.timeline.comment.list",
    }
)

WRITE_METHODS = frozenset({"crm.activity.add", "crm.timeline.comment.add"})

# Типы сущностей CRM
ENTITY_DEAL = 2
ENTITY_CONTACT = 3
# crm.activity: TYPE_ID=4 — e-mail, DIRECTION=2 — исходящее
ACTIVITY_EMAIL = 4
DIRECTION_OUTGOING = 2

RETRY_ERRORS = {"QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT", "INTERNAL_SERVER_ERROR"}


class BitrixError(RuntimeError):
    def __init__(self, code: str, description: str = "", method: str = ""):
        super().__init__(f"{method}: {code} {description}".strip())
        self.code = code
        self.description = description
        self.method = method


class CrmWriteForbidden(RuntimeError):
    """Попытка вызвать метод, который может изменить CRM."""


def check_method_allowed(method: str, params: dict | None = None) -> None:
    """Бросает CrmWriteForbidden, если вызов может изменить данные CRM."""
    params = params or {}
    if method == "batch":
        for key, cmd in (params.get("cmd") or {}).items():
            inner = cmd.split("?", 1)[0]
            if inner not in READ_METHODS:
                raise CrmWriteForbidden(f"batch[{key}]: в пакете разрешено только чтение, получен {inner}")
        return
    if method in READ_METHODS:
        return
    if method == "crm.activity.add":
        f = params.get("fields") or {}
        if int(f.get("TYPE_ID", 0)) != ACTIVITY_EMAIL or int(f.get("DIRECTION", 0)) != DIRECTION_OUTGOING:
            raise CrmWriteForbidden("crm.activity.add разрешён только для исходящего письма (TYPE_ID=4, DIRECTION=2)")
        if int(f.get("OWNER_TYPE_ID", 0)) not in (ENTITY_DEAL, ENTITY_CONTACT) or not int(f.get("OWNER_ID", 0)):
            raise CrmWriteForbidden("письмо должно быть привязано к существующей сделке или контакту")
        for c in f.get("COMMUNICATIONS") or []:
            if int(c.get("ENTITY_TYPE_ID", 0)) != ENTITY_CONTACT or not int(c.get("ENTITY_ID", 0)):
                # Без ENTITY_ID Битрикс24 может создать новый контакт/лид по адресу.
                raise CrmWriteForbidden("получатель письма должен быть существующим контактом (ENTITY_ID)")
        return
    if method == "crm.timeline.comment.add":
        f = params.get("fields") or {}
        if str(f.get("ENTITY_TYPE", "")).lower() not in ("deal", "contact") or not int(f.get("ENTITY_ID", 0)):
            raise CrmWriteForbidden("комментарий допускается только к существующей сделке или контакту")
        return
    raise CrmWriteForbidden(f"метод {method} запрещён: ассистент не изменяет CRM")


def build_query(params: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Кодирование параметров в стиле PHP http_build_query (нужно для batch)."""
    pairs: list[tuple[str, str]] = []
    if isinstance(params, dict):
        items = params.items()
    elif isinstance(params, (list, tuple)):
        items = enumerate(params)
    else:
        return [(prefix, "" if params is None else str(params))]
    for k, v in items:
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, (dict, list, tuple)):
            pairs.extend(build_query(v, key))
        else:
            pairs.append((key, "" if v is None else str(v)))
    return pairs


class BitrixClient:
    """Клиент входящего вебхука: https://<портал>/rest/<user>/<token>/"""

    def __init__(
        self,
        webhook_url: str,
        requests_per_second: float = 2.0,
        max_retries: int = 6,
        timeout: float = 60.0,
        transport: Callable[[str, dict], dict] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base = webhook_url.rstrip("/") + "/"
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self.max_retries = max_retries
        self.timeout = timeout
        self._transport = transport or self._http
        self._sleep = sleep
        self._last_call = 0.0
        self.calls = 0

    # --- транспорт -------------------------------------------------------
    def _http(self, method: str, params: dict) -> dict:
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + method + ".json",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except ValueError:
                return {"error": f"HTTP_{e.code}", "error_description": raw[:300]}

    def _throttle(self) -> None:
        if not self.min_interval:
            return
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def call_raw(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        check_method_allowed(method, params)
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.calls += 1
            data = self._transport(method, params)
            err = data.get("error")
            if not err:
                return data
            code = str(err)
            if (code in RETRY_ERRORS or code.startswith("HTTP_5")) and attempt < self.max_retries:
                log.warning("%s: %s, повтор через %.0f с", method, code, delay)
                self._sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise BitrixError(code, str(data.get("error_description", "")), method)
        raise BitrixError("RETRIES_EXHAUSTED", "", method)

    def call(self, method: str, params: dict | None = None) -> Any:
        return self.call_raw(method, params).get("result")

    # --- постраничное чтение ----------------------------------------------
    def count(self, method: str, filter_: dict) -> int | None:
        data = self.call_raw(method, {"filter": filter_, "select": ["ID"], "start": 0})
        total = data.get("total")
        return int(total) if total is not None else None

    def list_all(self, method: str, params: dict | None = None) -> Iterator[dict]:
        """Все записи списочного метода, без ограничения первыми 50.

        По умолчанию — «быстрая» пагинация по ID (>ID + start=-1), которую
        рекомендует Битрикс24 для больших выборок: она не пропускает и не
        дублирует записи и не считает COUNT на каждой странице. Если в фильтре
        уже есть условие на ID, используется обычная пагинация по ``start``.
        """
        params = dict(params or {})
        filter_ = dict(params.pop("filter", {}) or {})
        select = list(params.pop("select", []) or [])
        if select and "ID" not in select:
            select.append("ID")
        uses_id = any(k.lstrip("=!<>@%") == "ID" for k in filter_)
        if uses_id:
            start = 0
            while True:
                data = self.call_raw(method, {**params, "filter": filter_, "select": select,
                                              "order": {"ID": "ASC"}, "start": start})
                yield from data.get("result") or []
                if data.get("next") is None:
                    return
                start = int(data["next"])
        last_id = 0
        while True:
            page_filter = {**filter_, ">ID": last_id}
            data = self.call_raw(method, {**params, "filter": page_filter, "select": select,
                                          "order": {"ID": "ASC"}, "start": -1})
            rows = data.get("result") or []
            yield from rows
            if len(rows) < 50:
                return
            last_id = int(rows[-1]["ID"])

    def batch(self, commands: dict[str, tuple[str, dict]]) -> tuple[dict, dict]:
        """Пакетный запрос (до 50 команд). Возвращает (результаты, ошибки)."""
        results: dict = {}
        errors: dict = {}
        items = list(commands.items())
        for i in range(0, len(items), 50):
            chunk = items[i : i + 50]
            cmd = {key: m + "?" + urllib.parse.urlencode(build_query(p)) for key, (m, p) in chunk}
            res = self.call("batch", {"halt": 0, "cmd": cmd}) or {}
            results.update(res.get("result") or {})
            errs = res.get("result_error") or {}
            if isinstance(errs, dict):
                errors.update(errs)
        return results, errors
