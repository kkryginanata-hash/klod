"""Командная строка.

    python -m bitrix_mailer fields                           # воронки, стадии, поля сделки
    python -m bitrix_mailer preview campaigns/cold.toml      # предпросмотр + подтверждение
    python -m bitrix_mailer send 12                          # отправка кампании #12 (спросит подтверждение)
    python -m bitrix_mailer report 12                        # итоговый отчёт

    # изменение сделок (стадия, ответственный, поля) — тоже с подтверждением
    python -m bitrix_mailer update -w "Воронка=Холодная" -w "Стадия=Свободные" --set "Стадия=В работе"
    python -m bitrix_mailer update-apply 3                   # применить план #3 (спросит подтверждение)
    python -m bitrix_mailer update-undo 3                    # вернуть старые значения
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import campaign as camp_mod
from .client import BitrixClient
from .config import load_campaign, load_settings
from .filters import FilterResolver, conditions_from_mapping, parse_expression
from .senders import BitrixEmailSender, SmtpSender
from .storage import Storage
from .template import render
from . import updates


def _ask(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("да", "yes", "y", "д")
    except EOFError:
        return False


def make_sender(settings, client):
    if settings.send_via == "bitrix":
        if not settings.from_address:
            raise SystemExit("задайте [send].from — адрес ящика, подключённого к CRM Битрикс24")
        return BitrixEmailSender(client, settings.from_address, settings.responsible_id)
    s = settings.smtp
    return SmtpSender(client, host=s["host"], port=int(s.get("port", 465)), username=s.get("username", ""),
                      password=s.get("password", ""), from_address=settings.from_address or s.get("username", ""),
                      from_name=settings.from_name, security=s.get("security", "ssl"), reply_to=settings.reply_to,
                      log_to_contact=bool(s.get("log_to_contact", True)))


def cmd_fields(args, settings, client, storage) -> int:
    r = FilterResolver(client)
    print("Воронки и стадии:")
    for cid, name in r.categories().items():
        print(f"  [{cid}] {name}")
        for sid, sname in r.stages(cid):
            print(f"      {sid:<24} {sname}")
    print("\nПоля сделки (можно использовать код или название):")
    for code, meta in r.fields().items():
        title = meta.get("formLabel") or meta.get("listLabel") or meta.get("title") or ""
        items = ", ".join(i.get("VALUE", "") for i in meta.get("items") or [])
        print(f"  {code:<28} {meta.get('type', ''):<12} {title}" + (f"  [{items}]" if items else ""))
    return 0


def cmd_preview(args, settings, client, storage) -> int:
    camp = load_campaign(args.campaign)
    extra = [parse_expression(w) for w in args.where or []]
    pv = camp_mod.preview(client, storage, settings, camp, extra, args.contact_mode)
    print()
    print(camp_mod.format_preview(storage, pv.campaign_id))
    print(f"\nПолный список получателей: {pv.csv_path}")
    if pv.sample:
        print(f"\nПример (сделка #{pv.sample['deal_id']}, {pv.sample['email']}):\n  Тема: {pv.sample['subject']}")
        sample_path = Path(settings.reports_dir) / f"campaign-{pv.campaign_id}-sample.html"
        sample_path.write_text(pv.sample["html"], "utf-8")
        print(f"  HTML: {sample_path}")
    to_send = pv.stats["to_send"]
    if not to_send:
        print("\nОтправлять некому.")
        return 0
    if args.no_prompt:
        print(f"\nДля отправки: python -m bitrix_mailer send {pv.campaign_id}")
        return 0
    if _ask(f"\nОтправить {to_send} писем? Введите «да»: "):
        return _run_send(settings, client, storage, pv.campaign_id, args)
    print(f"Отправка не запущена. Запустить позже: python -m bitrix_mailer send {pv.campaign_id}")
    return 0


def cmd_send(args, settings, client, storage) -> int:
    print(camp_mod.format_preview(storage, args.campaign_id))
    queued = len(storage.recipients(args.campaign_id, "queued"))
    failed = len(storage.recipients(args.campaign_id, "failed")) if args.retry_failed else 0
    if args.dry_run:
        return _dry_run(settings, storage, args.campaign_id)
    if not args.yes and not _ask(f"\nОтправить {queued + failed} писем? Введите «да»: "):
        print("Отменено: нужно подтверждение («да» или флаг --yes).")
        return 1
    return _run_send(settings, client, storage, args.campaign_id, args)


def _update_client(settings) -> BitrixClient:
    """Клиент, которому разрешено менять поля существующих сделок. Создаётся только
    после подтверждения пользователя (update-apply/update-undo, send с [after_send])."""
    return BitrixClient(settings.webhook_url, settings.requests_per_second, allow_deal_updates=True)


def _run_send(settings, client, storage, cid, args) -> int:
    sender = make_sender(settings, client)
    upd = _update_client(settings) if storage.campaign_update_batch(cid) is not None else None
    camp_mod.send(storage, cid, sender, confirmed=True, rate_per_minute=settings.rate_per_minute,
                  limit=getattr(args, "limit", None), retry_failed=getattr(args, "retry_failed", False),
                  reports_dir=settings.reports_dir, update_client=upd)
    print()
    print(camp_mod.format_report(storage, cid))
    print(f"\nCSV: {camp_mod.export_csv(storage, cid, settings.reports_dir)}")
    return 0


def _dry_run(settings, storage, cid) -> int:
    c = storage.campaign(cid)
    out = Path(settings.reports_dir) / f"campaign-{cid}-dry-run"
    out.mkdir(parents=True, exist_ok=True)
    rows = storage.recipients(cid, "queued")
    for r in rows:
        ctx = json.loads(r["context_json"])
        subject = render(c["subject"], ctx, escape=False)
        body = render(c["body_html"], ctx, escape=True)
        (out / f"deal-{r['deal_id']}-contact-{r['contact_id']}.html").write_text(
            f"<!-- To: {r['email']} | Subject: {subject} -->\n{body}", "utf-8")
    print(f"\nDry-run: {len(rows)} писем сохранено в {out}; ничего не отправлено и в CRM не записано.")
    return 0


def cmd_report(args, settings, client, storage) -> int:
    camp_mod.refresh_read_status(client, storage, args.campaign_id)
    print(camp_mod.format_report(storage, args.campaign_id))
    print(f"\nCSV: {camp_mod.export_csv(storage, args.campaign_id, settings.reports_dir)}")
    return 0


def cmd_list(args, settings, client, storage) -> int:
    for c in storage.campaigns():
        counts = storage.counts(c["id"])
        print(f"#{c['id']:<4} {c['created_at']}  {c['status']:<10} {c['name']}  "
              f"отправлено {counts.get('sent', 0)}, в очереди {counts.get('queued', 0)}")
    return 0


def cmd_update(args, settings, client, storage) -> int:
    conds = [parse_expression(w) for w in args.where or []]
    raw = {}
    if args.campaign:
        camp = load_campaign(args.campaign)
        conds = conditions_from_mapping(camp.filter) + conds
        raw = camp.raw_filter
    resolver = FilterResolver(client)
    resolved = resolver.resolve(conds, raw)
    if args.deals:
        ids = [x.strip() for x in args.deals.split(",") if x.strip()]
        if not all(x.isdigit() for x in ids):
            raise SystemExit("--deals: номера сделок через запятую, например 12,15,40")
        resolved.bitrix["@ID"] = ids
    if not resolved.bitrix:
        raise SystemExit("укажите, какие сделки менять: -w условия, --campaign файл или --deals номера")
    changes = updates.parse_changes(args.set or [])
    planner = updates.ChangePlanner(resolver)
    total, deals = updates.load_deals(client, resolved.bitrix, planner.codes(changes))
    rows = planner.plan(deals, changes)
    info = {"filter": [str(c) for c in conds] + ([f"ID: {args.deals}"] if args.deals else []),
            "set": [str(c) for c in changes], "deals_found": total if total is not None else len(deals),
            "bitrix_filter": resolved.bitrix}
    bid = storage.create_update_batch(info, rows)
    print(updates.format_plan(storage, bid))
    n = storage.change_counts(bid).get("planned", 0)
    if not n:
        print("\nМенять нечего.")
        return 0
    if args.no_prompt:
        print(f"\nДля применения: python -m bitrix_mailer update-apply {bid}")
        return 0
    if _ask(f"\nИзменить {n} сделок? Введите «да»: "):
        return _apply(settings, storage, bid)
    print(f"Изменения не внесены. Применить позже: python -m bitrix_mailer update-apply {bid}")
    return 0


def _apply(settings, storage, bid) -> int:
    updates.apply_batch(_update_client(settings), storage, bid, confirmed=True)
    print()
    print(updates.format_result(storage, bid))
    print(f"\nОткатить: python -m bitrix_mailer update-undo {bid}")
    return 0


def cmd_update_apply(args, settings, client, storage) -> int:
    b = storage.update_batch(args.batch_id)
    if b["campaign_id"]:
        raise SystemExit("это изменение привязано к рассылке и применяется при её отправке (send)")
    print(updates.format_plan(storage, args.batch_id))
    n = storage.change_counts(args.batch_id).get("planned", 0)
    if not n:
        print("\nМенять нечего.")
        return 0
    if not args.yes and not _ask(f"\nИзменить {n} сделок? Введите «да»: "):
        print("Отменено: нужно подтверждение («да» или флаг --yes).")
        return 1
    return _apply(settings, storage, args.batch_id)


def cmd_update_undo(args, settings, client, storage) -> int:
    n = storage.change_counts(args.batch_id).get("applied", 0)
    print(updates.format_result(storage, args.batch_id))
    if not n:
        print("\nОткатывать нечего.")
        return 0
    if not args.yes and not _ask(f"\nВернуть старые значения в {n} сделках? Введите «да»: "):
        print("Отменено: нужно подтверждение («да» или флаг --yes).")
        return 1
    updates.undo_batch(_update_client(settings), storage, args.batch_id, confirmed=True)
    print()
    print(updates.format_result(storage, args.batch_id))
    return 0


def cmd_updates(args, settings, client, storage) -> int:
    for b in storage.update_batches():
        info = json.loads(b["info_json"])
        c = storage.change_counts(b["id"])
        src = f"после кампании #{b['campaign_id']}" if b["campaign_id"] else "; ".join(info.get("filter", []))
        print(f"#{b['id']:<4} {b['created_at']}  {b['status']:<8} {', '.join(info['set'])}  [{src}]  "
              f"изменено {c.get('applied', 0)}, ждут {c.get('planned', 0)}, откачено {c.get('undone', 0)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bitrix_mailer", description="Рассылка по сделкам Битрикс24 с фильтрами")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fields", help="показать воронки, стадии и поля сделки")

    pp = sub.add_parser("preview", help="собрать получателей и показать статистику")
    pp.add_argument("campaign", help="файл кампании (.toml)")
    pp.add_argument("-w", "--where", action="append",
                    help="доп. условие, напр. -w 'Сумма>100000' -w 'Дата создания=последние 30 дней'")
    pp.add_argument("--contact-mode", choices=["primary", "all", "valid_only"])
    pp.add_argument("--no-prompt", action="store_true", help="не спрашивать подтверждение, только предпросмотр")

    ps = sub.add_parser("send", help="отправить подтверждённую кампанию")
    ps.add_argument("campaign_id", type=int)
    ps.add_argument("--yes", action="store_true", help="подтверждение отправки (после просмотра статистики)")
    ps.add_argument("--dry-run", action="store_true", help="сохранить письма в файлы, ничего не отправлять")
    ps.add_argument("--limit", type=int, help="отправить не больше N писем за запуск")
    ps.add_argument("--retry-failed", action="store_true", help="повторить письма с ошибкой")

    pr = sub.add_parser("report", help="отчёт по кампании")
    pr.add_argument("campaign_id", type=int)
    sub.add_parser("list", help="список кампаний")

    pu = sub.add_parser("update", help="изменить поля сделок по фильтру (с подтверждением)")
    pu.add_argument("-w", "--where", action="append", help="условие отбора сделок, как в preview")
    pu.add_argument("--campaign", help="взять фильтр из файла кампании")
    pu.add_argument("--deals", help="номера сделок через запятую")
    pu.add_argument("-s", "--set", action="append", required=True,
                    help="что изменить, напр. --set 'Стадия=В работе' --set 'Ответственный=Пётр Петров'")
    pu.add_argument("--no-prompt", action="store_true", help="только показать план, не применять")
    pa = sub.add_parser("update-apply", help="применить план изменений")
    pa.add_argument("batch_id", type=int)
    pa.add_argument("--yes", action="store_true")
    pn = sub.add_parser("update-undo", help="откатить изменения (вернуть старые значения)")
    pn.add_argument("batch_id", type=int)
    pn.add_argument("--yes", action="store_true")
    sub.add_parser("updates", help="журнал изменений сделок")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    settings = load_settings(args.config)
    client = BitrixClient(settings.webhook_url, settings.requests_per_second)
    storage = Storage(settings.db_path)
    handler = {"fields": cmd_fields, "preview": cmd_preview, "send": cmd_send,
               "report": cmd_report, "list": cmd_list, "update": cmd_update,
               "update-apply": cmd_update_apply, "update-undo": cmd_update_undo, "updates": cmd_updates}[args.cmd]
    return handler(args, settings, client, storage)
