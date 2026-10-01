"""Сценарий кампании: предпросмотр → подтверждение → очередь отправки → отчёт."""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import template
from .client import BitrixClient, CrmWriteForbidden
from .config import Campaign, Settings
from .filters import Condition, FilterResolver, conditions_from_mapping
from .recipients import CONTACT_MODES, Options, collect
from .senders import Message
from .storage import Storage, now
from . import updates

log = logging.getLogger(__name__)

CONTACT_PLACEHOLDER_FIELDS = {"NAME", "LAST_NAME", "SECOND_NAME", "HONORIFIC", "POST", "COMPANY_ID", "EMAIL", "ID"}


@dataclass
class Preview:
    campaign_id: int
    stats: dict
    filter_lines: list[str]
    csv_path: Path
    sample: dict | None


def preview(
    client: BitrixClient,
    storage: Storage,
    settings: Settings,
    camp: Campaign,
    extra_conditions: list[Condition] | None = None,
    contact_mode: str | None = None,
    progress: Callable[[str], None] = print,
) -> Preview:
    mode = contact_mode or camp.contact_mode
    if mode not in CONTACT_MODES:
        raise ValueError(f"contact_mode: {', '.join(CONTACT_MODES)}")

    fields_used = template.placeholders(camp.subject, camp.body_html)
    after_conds = updates.parse_changes(camp.after_send) if camp.after_send else []
    conditions = conditions_from_mapping(camp.filter) + list(extra_conditions or [])
    if not conditions and not camp.raw_filter:
        raise ValueError("фильтр пуст — рассылка по всем сделкам портала запрещена; задайте условия")
    resolver = FilterResolver(client)
    resolved = resolver.resolve(conditions, camp.raw_filter)
    for line in resolved.explained:
        progress(f"  фильтр: {line}")
    planner = updates.ChangePlanner(resolver)
    after_codes = planner.codes(after_conds)  # проверка полей до загрузки сделок

    opts = Options(
        contact_mode=mode,
        check_dns=settings.check_dns,
        dedupe_email=camp.dedupe_email,
        campaign_key=camp.key,
        bitrix_history_subject=camp.bitrix_history_subject,
        extra_deal_fields=sorted({f for e, f in fields_used if e == "deal"} | set(after_codes)),
        extra_contact_fields=sorted(f for e, f in fields_used if e == "contact" and f not in CONTACT_PLACEHOLDER_FIELDS),
    )
    col = collect(client, storage, resolved.bitrix, opts, progress)

    filter_info = {"conditions": [str(c) for c in conditions], "raw": camp.raw_filter,
                   "bitrix": resolved.bitrix, "explained": resolved.explained}
    cid = storage.create_campaign(key=camp.key, name=camp.name, subject=camp.subject, body_html=camp.body_html,
                                  filter_info=filter_info, contact_mode=mode, send_via=settings.send_via)
    storage.add_recipients(cid, col.rows)
    storage.set_stats(cid, col.stats)
    csv_path = export_csv(storage, cid, settings.reports_dir)

    if after_conds:
        # Изменения «после отправки» планируются сейчас и показываются в предпросмотре;
        # применяются только к сделкам, по которым письмо действительно ушло.
        send_deals = {r["deal_id"] for r in col.rows if r["status"] == "queued"}
        rows = planner.plan([d for d in col.deals if int(d["ID"]) in send_deals], after_conds)
        storage.create_update_batch({"set": [str(c) for c in after_conds], "filter": [], "deals_found": None},
                                    rows, campaign_id=cid)

    sample = None
    first = next((r for r in col.rows if r["status"] == "queued"), None)
    if first:
        sample = {"deal_id": first["deal_id"], "email": first["email"],
                  "subject": template.render(camp.subject, first["context"], escape=False),
                  "html": template.render(camp.body_html, first["context"], escape=True)}
    return Preview(cid, col.stats, [str(c) for c in conditions], csv_path, sample)


def format_preview(storage: Storage, cid: int) -> str:
    c = storage.campaign(cid)
    s = json.loads(c["stats_json"])
    f = json.loads(c["filter_json"])
    lines = [f"Кампания #{cid} «{c['name']}» (ключ истории: {c['key']})", "", "Фильтр:"]
    lines += [f"  • {x}" for x in f["conditions"]] or ["  • —"]
    lines += [f"  • (как есть) {k} = {v}" for k, v in (f.get("raw") or {}).items()]
    lines += [f"Получатели: {CONTACT_MODES[c['contact_mode']]}",
              f"Отправка: {'через Битрикс24 (исходящее письмо в Timeline)' if c['send_via'] == 'bitrix' else 'SMTP + запись в Timeline'}",
              "",
              f"Найдено по фильтру:     {s['deals_found']} сделок" + (
                  f" (загружено {s['deals_loaded']})" if s["deals_found"] != s["deals_loaded"] else ""),
              f"С контактами:           {s['deals_with_contacts']}",
              f"Без контакта:           {s['deals_without_contacts']}",
              f"С валидным email:       {s['deals_with_valid_email']}",
              f"Без валидного email:    {s['deals_without_email']}",
              f"Уже получали письмо:    {s['already_sent']}"]
    if s.get("duplicates"):
        lines.append(f"Повтор email в выборке: {s['duplicates']} (отправим один раз)")
    lines.append(f"Будет отправлено:       {s['to_send']} писем по {s['deals_to_send']} сделкам")
    skipped: dict[str, int] = {}
    for r in storage.recipients(cid, "skipped"):
        reason = (r["skip_reason"] or "").split(" (")[0].split(" #")[0].split(": ")[0]
        skipped[reason] = skipped.get(reason, 0) + 1
    if skipped:
        lines += ["", "Причины исключения:"] + [f"  – {k}: {v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])]
    batch = storage.campaign_update_batch(cid)
    if batch is not None:
        info = json.loads(batch["info_json"])
        counts = storage.change_counts(batch["id"])
        lines += ["", f"После отправки изменить сделки ({', '.join(info['set'])}):",
                  f"  будет изменено: {counts.get('planned', 0)}, без изменений/нельзя: {counts.get('skipped', 0)}"]
        examples = storage.deal_changes(batch["id"], "planned")[:3]
        lines += [f"  например, сделка #{r['deal_id']}: {r['label']}" for r in examples]
        reasons = {(r["skip_reason"] or "").split(" (")[0] for r in storage.deal_changes(batch["id"], "skipped")}
        lines += [f"  пропуск: {x}" for x in sorted(reasons)]
    return "\n".join(lines)


def export_csv(storage: Storage, cid: int, reports_dir: str) -> Path:
    path = Path(reports_dir) / f"campaign-{cid}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["deal_id", "deal_title", "contact_id", "contact_name", "is_primary", "email", "status",
            "skip_reason", "activity_id", "timeline_verified", "error", "sent_at", "read_status", "read_at"]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(cols)
        for r in storage.recipients(cid):
            w.writerow([r[c] for c in cols])
    return path


READ_STATUS_TITLES = {"read": "прочитано", "unread": "не прочитано", "untracked": "нет данных"}


def read_status_of(activity: dict) -> tuple[str, str | None]:
    """Статус прочтения письма-дела «E-mail» по данным Битрикс24: (статус, время открытия).

    Битрикс24 вставляет пиксель отслеживания в письма, отправленные из интерфейса
    CRM (у таких дел есть ``SETTINGS.EMAIL_META``), и при открытии ставит
    ``SETTINGS.READ_CONFIRMED`` (unix-время). В письма, созданные через REST
    (crm.activity.add — так отправляет этот инструмент), и в «сжатые» дела пиксель
    не попадает: у них отсутствие отметки ничего не значит — это «нет данных»,
    а не «не прочитано».
    """
    settings = activity.get("SETTINGS") or {}
    if not isinstance(settings, dict):
        settings = {}
    ts = settings.get("READ_CONFIRMED")
    if ts:
        return "read", dt.datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")
    if "EMAIL_META" in settings:
        return "unread", None
    return "untracked", None


def refresh_read_status(client: BitrixClient, storage: Storage, cid: int) -> dict[str, int] | None:
    """Подтянуть из Битрикс24 статус прочтения отправленных писем (только чтение,
    crm.activity.get). Возвращает счётчики статусов или None, если письма ушли через
    SMTP — их Битрикс24 не отслеживает."""
    if storage.campaign(cid)["send_via"] != "bitrix":
        return None
    rows = [r for r in storage.recipients(cid, "sent") if r["activity_id"] and r["read_status"] != "read"]
    if rows:
        results, _errors = client.batch({str(r["id"]): ("crm.activity.get", {"id": r["activity_id"]}) for r in rows})
        for r in rows:
            activity = results.get(str(r["id"]))
            if not activity:
                continue  # дело не прочиталось — статус остаётся прежним (или неизвестным)
            status, read_at = read_status_of(activity)
            storage.update_recipient(r["id"], read_status=status, read_at=read_at)
    counts: dict[str, int] = {}
    for r in storage.recipients(cid, "sent"):
        key = r["read_status"] or "untracked"
        counts[key] = counts.get(key, 0) + 1
    return counts


class NotConfirmed(RuntimeError):
    pass


def send(
    storage: Storage,
    cid: int,
    sender,
    *,
    confirmed: bool,
    rate_per_minute: float = 30.0,
    limit: int | None = None,
    retry_failed: bool = False,
    reports_dir: str = "reports",
    progress: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    update_client: BitrixClient | None = None,
) -> dict:
    """update_client — клиент с allow_deal_updates=True; нужен, только если в кампании
    задан [after_send] (изменение сделок после отправки, показанное в предпросмотре)."""
    c = storage.campaign(cid)
    batch = storage.campaign_update_batch(cid)
    if batch is not None and update_client is None:
        raise RuntimeError("в кампании задано изменение сделок после отправки, но клиент для изменений не передан")
    if not confirmed:
        raise NotConfirmed("рассылка не подтверждена")
    if c["status"] in ("done", "cancelled") and not retry_failed:
        raise RuntimeError(f"кампания #{cid} уже завершена ({c['status']})")
    created = dt.datetime.fromisoformat(c["created_at"])
    if dt.datetime.now() - created > dt.timedelta(hours=24):
        progress("⚠ Предпросмотр старше 24 часов: данные в CRM могли измениться, лучше сделать новый preview.")

    # Письма, отправка которых оборвалась (сбой посреди отправки): статус неизвестен,
    # повторно не шлём, чтобы не задублировать.
    for r in storage.recipients(cid, "sending"):
        storage.update_recipient(r["id"], status="unknown", error="отправка прервана; проверьте Timeline сделки вручную")
    if retry_failed:
        for r in storage.recipients(cid, "failed"):
            storage.update_recipient(r["id"], status="queued", error=None)
    if c["status"] == "previewed":
        storage.set_status(cid, "confirmed")
    storage.set_status(cid, "sending")

    queue = storage.recipients(cid, "queued")
    if limit is not None:
        queue = queue[:limit]
    interval = 60.0 / rate_per_minute if rate_per_minute > 0 else 0.0
    progress(f"В очереди: {len(queue)} писем, скорость до {rate_per_minute:g}/мин")
    try:
        for i, r in enumerate(queue, 1):
            prev = storage.already_sent(c["key"], r["email"], exclude_recipient=r["id"])
            if prev is not None:
                storage.update_recipient(r["id"], status="skipped",
                                         skip_reason=f"уже получал это письмо ({prev['sent_at']})")
                continue
            ctx = json.loads(r["context_json"])
            deal = ctx.get("deal") or {}
            msg = Message(
                deal_id=r["deal_id"], contact_id=r["contact_id"], contact_name=r["contact_name"] or "",
                email=r["email"],
                subject=template.render(c["subject"], ctx, escape=False),
                html=template.render(c["body_html"], ctx, escape=True),
                responsible_id=int(deal["ASSIGNED_BY_ID"]) if deal.get("ASSIGNED_BY_ID") else None,
            )
            storage.update_recipient(r["id"], status="sending", attempts=r["attempts"] + 1)
            try:
                res = sender.send(msg)
            except CrmWriteForbidden:
                storage.update_recipient(r["id"], status="failed", error="запрещено защитой CRM")
                raise
            except Exception as e:  # noqa: BLE001 — ошибка одного адреса не останавливает очередь
                log.exception("ошибка отправки сделке %s", r["deal_id"])
                storage.update_recipient(r["id"], status="failed", error=str(e)[:500])
                progress(f"  [{i}/{len(queue)}] ✗ сделка #{r['deal_id']} {r['email']}: {e}")
            else:
                storage.update_recipient(r["id"], status="sent", activity_id=res.timeline_id,
                                         timeline_verified=int(res.verified), sent_at=now(),
                                         error=None if res.verified else f"Timeline: {res.note}")
                mark = "✓" if res.verified else "✓ (Timeline не подтверждён)"
                progress(f"  [{i}/{len(queue)}] {mark} сделка #{r['deal_id']} → {r['email']}")
                if batch is not None:
                    for ch in storage.deal_changes(batch["id"], "planned", deal_id=r["deal_id"]):
                        ok, err = updates.apply_one(update_client, storage, ch)
                        progress(f"      {'✓' if ok else '✗'} {ch['label'] if ok else 'сделка не изменена: ' + err}")
            if interval and i < len(queue):
                sleep(interval)
    finally:
        if hasattr(sender, "close"):
            sender.close()
        counts = storage.counts(cid)
        if not counts.get("queued"):
            storage.set_status(cid, "done")
        if batch is not None:
            storage.set_update_status(batch["id"], "applied")
        export_csv(storage, cid, reports_dir)
    return storage.counts(cid)


def format_report(storage: Storage, cid: int) -> str:
    c = storage.campaign(cid)
    s = json.loads(c["stats_json"])
    counts = storage.counts(cid)
    rows = storage.recipients(cid)
    verified = sum(1 for r in rows if r["status"] == "sent" and r["timeline_verified"])
    lines = [
        f"Отчёт по кампании #{cid} «{c['name']}» — статус: {c['status']}",
        f"Найдено сделок по фильтру: {s.get('deals_found')}",
        f"Запланировано к отправке:  {s.get('to_send')}",
        f"Отправлено:                {counts.get('sent', 0)}",
        f"  из них видно в Timeline: {verified}",
    ]
    sent = [r for r in rows if r["status"] == "sent"]
    by_read: dict[str, list] = {"read": [], "unread": [], "untracked": []}
    for r in sent:
        by_read[r["read_status"] if r["read_status"] in by_read else "untracked"].append(r)
    if c["send_via"] == "bitrix":
        tracked = len(by_read["read"]) + len(by_read["unread"])
        pct = f" ({round(100 * len(by_read['read']) / tracked)}% от отслеживаемых)" if tracked else ""
        lines += [f"  прочитано:               {len(by_read['read'])}{pct}",
                  f"  не прочитано:            {len(by_read['unread'])}",
                  f"  нет данных о прочтении:  {len(by_read['untracked'])}"]
        if by_read["untracked"]:
            lines.append("    (Битрикс24 не отслеживает открытие писем, созданных через API,"
                         " — это не значит, что их не прочитали)")
    else:
        lines.append("  прочитано:               нет данных (письма через SMTP Битрикс24 не отслеживает)")
    lines += [
        f"Ошибки отправки:           {counts.get('failed', 0)}",
        f"Статус неизвестен:         {counts.get('unknown', 0)}",
        f"Ещё в очереди:             {counts.get('queued', 0)}",
        f"Пропущено:                 {counts.get('skipped', 0)}",
    ]
    batch = storage.campaign_update_batch(cid)
    if batch is not None:
        bc = storage.change_counts(batch["id"])
        lines.append(f"Сделки изменены после отправки: {bc.get('applied', 0)}, ошибки: {bc.get('failed', 0)}"
                     f" (журнал изменений #{batch['id']}, откат: update-undo {batch['id']})")
    if c["send_via"] == "bitrix":
        for title, part in (("Прочитали:", by_read["read"]), ("Не прочитали (открытие отслеживается):", by_read["unread"])):
            if not part:
                continue
            lines += ["", title]
            lines += [f"  сделка #{r['deal_id']} {r['contact_name'] or ''} {r['email']}"
                      + (f" — {r['read_at'].replace('T', ' ')}" if r["read_at"] else "") for r in part[:50]]
            if len(part) > 50:
                lines.append(f"  … и ещё {len(part) - 50} (см. CSV)")
    bad = [r for r in rows if r["status"] in ("failed", "unknown") or (r["status"] == "sent" and not r["timeline_verified"])]
    if bad:
        lines += ["", "Требуют внимания:"]
        lines += [f"  сделка #{r['deal_id']} контакт #{r['contact_id']} {r['email']}: {r['status']} — {r['error']}"
                  for r in bad[:50]]
        if len(bad) > 50:
            lines.append(f"  … и ещё {len(bad) - 50} (см. CSV)")
    return "\n".join(lines)
