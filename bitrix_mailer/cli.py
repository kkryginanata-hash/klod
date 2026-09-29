"""Командная строка.

    python -m bitrix_mailer fields                           # воронки, стадии, поля сделки
    python -m bitrix_mailer preview campaigns/cold.toml      # предпросмотр + подтверждение
    python -m bitrix_mailer send 12                          # отправка кампании #12 (спросит подтверждение)
    python -m bitrix_mailer report 12                        # итоговый отчёт
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
from .filters import FilterResolver, parse_expression
from .senders import BitrixEmailSender, SmtpSender
from .storage import Storage
from .template import render


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


def _run_send(settings, client, storage, cid, args) -> int:
    sender = make_sender(settings, client)
    camp_mod.send(storage, cid, sender, confirmed=True, rate_per_minute=settings.rate_per_minute,
                  limit=getattr(args, "limit", None), max_per_day=settings.max_per_day,
                  retry_failed=getattr(args, "retry_failed", False),
                  reports_dir=settings.reports_dir)
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
    print(camp_mod.format_report(storage, args.campaign_id))
    print(f"\nCSV: {camp_mod.export_csv(storage, args.campaign_id, settings.reports_dir)}")
    return 0


def cmd_list(args, settings, client, storage) -> int:
    for c in storage.campaigns():
        counts = storage.counts(c["id"])
        print(f"#{c['id']:<4} {c['created_at']}  {c['status']:<10} {c['name']}  "
              f"отправлено {counts.get('sent', 0)}, в очереди {counts.get('queued', 0)}")
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

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    settings = load_settings(args.config)
    client = BitrixClient(settings.webhook_url, settings.requests_per_second)
    storage = Storage(settings.db_path)
    handler = {"fields": cmd_fields, "preview": cmd_preview, "send": cmd_send,
               "report": cmd_report, "list": cmd_list}[args.cmd]
    return handler(args, settings, client, storage)
