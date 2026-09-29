import unittest

from bitrix_mailer import campaign as camp_mod
from bitrix_mailer import updates
from bitrix_mailer.client import BitrixClient, CrmWriteForbidden, check_method_allowed
from bitrix_mailer.filters import FilterError, FilterResolver
from bitrix_mailer.senders import BitrixEmailSender
from bitrix_mailer.storage import Storage

from tests.fake_bitrix import cold_portal
from tests.test_mailer import campaign, make, settings

COLD_FILTER = {"=CATEGORY_ID": "4", "=STAGE_ID": "C4:NEW"}


def updater(fb):
    return BitrixClient("https://example.bitrix24.ru/rest/1/x/", requests_per_second=0, transport=fb,
                        sleep=lambda s: None, allow_deal_updates=True)


def plan(fb, st, changes, flt=COLD_FILTER):
    client = make(fb)
    planner = updates.ChangePlanner(FilterResolver(client))
    conds = updates.parse_changes(changes)
    total, deals = updates.load_deals(client, flt, planner.codes(conds))
    return st.create_update_batch({"filter": [], "set": changes, "deals_found": total}, planner.plan(deals, conds))


class DealUpdateGuardTest(unittest.TestCase):
    def test_deal_update_off_by_default(self):
        with self.assertRaises(CrmWriteForbidden):
            check_method_allowed("crm.deal.update", {"id": 5, "fields": {"STAGE_ID": "C4:WORK"}})
        fb = cold_portal()
        with self.assertRaises(CrmWriteForbidden):
            make(fb).call("crm.deal.update", {"id": 5, "fields": {"STAGE_ID": "C4:WORK"}})
        self.assertNotIn("crm.deal.update", fb.calls)

    def test_allowed_only_for_existing_deal_fields(self):
        check_method_allowed("crm.deal.update", {"id": 5, "fields": {"STAGE_ID": "C4:WORK"}}, True)
        for bad in ({"id": 0, "fields": {"STAGE_ID": "X"}}, {"id": 5, "fields": {}},
                    {"id": 5, "fields": {"CATEGORY_ID": "0"}}, {"id": 5, "fields": {"CONTACT_ID": "1"}},
                    {"id": 5, "fields": {"COMPANY_ID": "1"}}):
            with self.assertRaises(CrmWriteForbidden, msg=bad):
                check_method_allowed("crm.deal.update", bad, True)

    def test_creation_and_other_entities_still_forbidden(self):
        for m in ("crm.deal.add", "crm.lead.add", "crm.contact.add", "crm.company.add", "crm.deal.delete",
                  "crm.contact.update", "crm.company.update", "crm.lead.update"):
            with self.assertRaises(CrmWriteForbidden, msg=m):
                check_method_allowed(m, {"id": 1, "fields": {"TITLE": "x"}}, True)
        with self.assertRaises(CrmWriteForbidden):
            check_method_allowed("batch", {"cmd": {"a": "crm.deal.update?id=1&fields[STAGE_ID]=X"}}, True)


class DealUpdateFlowTest(unittest.TestCase):
    def test_plan_apply_undo(self):
        fb, st = cold_portal(), Storage(":memory:")
        fb.deals[0]["STAGE_ID"] = "C4:NEW"
        bid = plan(fb, st, ["Стадия=В работе", "Ответственный=Пётр Петров"])
        text = updates.format_plan(st, bid)
        self.assertIn("Найдено сделок:          80", text)
        self.assertIn("Стадия: Свободные → В работе", text)
        # сделки с ответственным Петров уже в нужном состоянии по этому полю, но стадия меняется
        self.assertEqual(st.change_counts(bid), {"planned": 80})
        self.assertFalse([c for c in fb.calls if c == "crm.deal.update"])  # до подтверждения ничего

        with self.assertRaises(PermissionError):
            updates.apply_batch(updater(fb), st, bid, confirmed=False, progress=lambda s: None)
        updates.apply_batch(updater(fb), st, bid, confirmed=True, progress=lambda s: None)
        cold = [d for d in fb.deals if int(d["ID"]) <= 80]
        self.assertTrue(all(d["STAGE_ID"] == "C4:WORK" and d["ASSIGNED_BY_ID"] == "8" for d in cold))
        self.assertEqual(fb.deals[100]["STAGE_ID"], "C4:WORK")  # чужая сделка не тронута (была в работе)
        self.assertEqual(st.change_counts(bid), {"applied": 80})

        fb.deals[0]["STAGE_ID"] = "C4:NEW"  # кто-то вручную поменял сделку #1 после нас
        updates.undo_batch(updater(fb), st, bid, confirmed=True, progress=lambda s: None)
        self.assertEqual(fb.deals[1]["STAGE_ID"], "C4:NEW")
        self.assertEqual(fb.deals[2]["ASSIGNED_BY_ID"], "7")
        self.assertEqual(st.change_counts(bid), {"undone": 79, "applied": 1})

    def test_skips_and_errors(self):
        fb, st = cold_portal(), Storage(":memory:")
        bid = plan(fb, st, ["Стадия=Свободные"])
        self.assertEqual(st.change_counts(bid), {"skipped": 80})  # уже в этой стадии
        bid = plan(fb, st, ["Стадия=Новая"])  # стадия из другой воронки
        self.assertIn("нет стадии", updates.format_plan(st, bid))
        with self.assertRaises(FilterError):
            plan(fb, st, ["Воронка=Общая"])
        with self.assertRaises(FilterError):
            updates.parse_changes(["Сумма>5"])


class AfterSendTest(unittest.TestCase):
    def test_stage_changed_only_for_sent_deals(self):
        fb, st = cold_portal(), Storage(":memory:")
        client = make(fb)
        camp = campaign(after_send={"Стадия": "В работе"})
        pv = camp_mod.preview(client, st, settings(), camp, progress=lambda s: None)
        text = camp_mod.format_preview(st, pv.campaign_id)
        self.assertIn("После отправки изменить сделки (Стадия = В работе)", text)
        self.assertIn("будет изменено: 73", text)
        self.assertNotIn("crm.deal.update", fb.calls)

        sender = BitrixEmailSender(client, "sales@example.com")
        with self.assertRaises(RuntimeError):  # без клиента изменений не отправляем молча
            camp_mod.send(st, pv.campaign_id, sender, confirmed=True, rate_per_minute=0, progress=lambda s: None)
        camp_mod.send(st, pv.campaign_id, sender, confirmed=True, rate_per_minute=0,
                      reports_dir="/tmp/bitrix-mailer-tests", progress=lambda s: None, update_client=updater(fb))
        moved = {int(d["ID"]) for d in fb.deals if d["STAGE_ID"] == "C4:WORK" and int(d["ID"]) <= 80}
        self.assertEqual(len(moved), 73)
        self.assertTrue({1, 2, 3, 4, 5, 6, 7}.isdisjoint(moved))  # без контакта/email — не тронуты
        self.assertIn("Сделки изменены после отправки: 73", camp_mod.format_report(st, pv.campaign_id))


if __name__ == "__main__":
    unittest.main()
