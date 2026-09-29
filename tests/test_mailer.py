import datetime as dt
import unittest

from bitrix_mailer import campaign as camp_mod
from bitrix_mailer.client import BitrixClient, CrmWriteForbidden, check_method_allowed
from bitrix_mailer.config import Campaign, Settings
from bitrix_mailer.filters import FilterError, FilterResolver, conditions_from_mapping, parse_expression
from bitrix_mailer.recipients import Options, collect
from bitrix_mailer.senders import BitrixEmailSender, DryRunSender
from bitrix_mailer.storage import Storage
from bitrix_mailer import template, validation

from tests.fake_bitrix import FakeBitrix, cold_portal

COLD = {"Воронка": "Холодная", "Стадия": "Свободные"}


def make(fb: FakeBitrix):
    return BitrixClient("https://example.bitrix24.ru/rest/1/x/", requests_per_second=0, transport=fb,
                        sleep=lambda s: None)


def settings():
    return Settings(webhook_url="x", reports_dir="/tmp/bitrix-mailer-tests", from_address="sales@example.com")


def campaign(**kw):
    base = dict(name="Тест", key="cold-1", subject="{{contact.NAME}}, предложение",
                body_html="<p>Здравствуйте, {{contact.NAME|default:\"коллега\"}}! Сделка {{deal.TITLE}}</p>",
                filter=dict(COLD))
    base.update(kw)
    return Campaign(**base)


class GuardTest(unittest.TestCase):
    def test_crm_changes_forbidden(self):
        for m in ("crm.lead.add", "crm.deal.add", "crm.contact.add", "crm.company.add", "crm.deal.update",
                  "crm.contact.update", "crm.deal.delete", "crm.item.update", "crm.activity.update"):
            with self.assertRaises(CrmWriteForbidden, msg=m):
                check_method_allowed(m, {"fields": {"STAGE_ID": "WON"}})

    def test_writes_hidden_in_batch_forbidden(self):
        with self.assertRaises(CrmWriteForbidden):
            check_method_allowed("batch", {"cmd": {"a": "crm.deal.update?id=1&fields[STAGE_ID]=WON"}})

    def test_activity_only_outgoing_email_to_existing_contact(self):
        ok = {"TYPE_ID": 4, "DIRECTION": 2, "OWNER_TYPE_ID": 2, "OWNER_ID": 5,
              "COMMUNICATIONS": [{"VALUE": "a@b.ru", "ENTITY_ID": 9, "ENTITY_TYPE_ID": 3}]}
        check_method_allowed("crm.activity.add", {"fields": ok})
        for bad in ({"TYPE_ID": 2}, {"DIRECTION": 1}, {"OWNER_ID": 0},
                    {"COMMUNICATIONS": [{"VALUE": "a@b.ru", "ENTITY_TYPE_ID": 3}]}):
            with self.assertRaises(CrmWriteForbidden):
                check_method_allowed("crm.activity.add", {"fields": {**ok, **bad}})

    def test_client_refuses_before_http(self):
        fb = FakeBitrix()
        with self.assertRaises(CrmWriteForbidden):
            make(fb).call("crm.lead.add", {"fields": {"TITLE": "x"}})
        self.assertEqual(fb.calls, [])


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.r = FilterResolver(make(cold_portal()), today=dt.date(2026, 9, 29))

    def test_names_resolved_to_ids(self):
        conds = conditions_from_mapping({"Стадия": "Свободные", "Воронка": "Холодная",
                                         "Ответственный": "Иван Иванов", "Сумма": "> 100 000",
                                         "Дата создания": "последние 30 дней", "Регион": "Москва"})
        f = self.r.resolve(conds).bitrix
        self.assertEqual(f, {"=CATEGORY_ID": "4", "=STAGE_ID": "C4:NEW", "=ASSIGNED_BY_ID": "7",
                             ">OPPORTUNITY": "100000", ">=DATE_CREATE": "2026-08-30T00:00:00",
                             "=UF_CRM_REGION": "11"})

    def test_cli_expressions(self):
        self.assertEqual(parse_expression("Сумма>=5000").op, ">=")
        c = parse_expression("Стадия = Свободные, В работе")
        self.assertEqual(c.value, ["Свободные", "В работе"])
        f = self.r.resolve([parse_expression("Воронка=Холодная"), c]).bitrix
        self.assertEqual(f["STAGE_ID"], ["C4:NEW", "C4:WORK"])

    def test_unknown_values_are_errors(self):
        for cond in ({"Воронка": "Горячая"}, {"Стадия": "Нет такой"}, {"Ответственный": "Кто-то"},
                     {"Непонятное поле": "1"}):
            with self.assertRaises(FilterError):
                self.r.resolve(conditions_from_mapping(cond))


class CollectTest(unittest.TestCase):
    def test_example_funnel_80_to_68(self):
        fb = cold_portal()
        client, st = make(fb), Storage(":memory:")
        # 5 контактов уже получали это письмо в прошлой кампании
        old = st.create_campaign(key="cold-1", name="old", subject="s", body_html="b", filter_info={},
                                 contact_mode="primary", send_via="bitrix")
        st.add_recipients(old, [{"deal_id": i, "contact_id": 1000 + i, "email": f"client{i}@example.com",
                                 "status": "sent"} for i in range(10, 15)])
        pv = camp_mod.preview(client, st, settings(), campaign(), progress=lambda s: None)
        s = pv.stats
        self.assertEqual(s["deals_found"], 80)
        self.assertEqual(s["deals_loaded"], 80)  # все страницы, не только первые 50
        self.assertEqual(s["deals_with_contacts"], 76)
        self.assertEqual(s["deals_with_valid_email"], 73)
        self.assertEqual(s["already_sent"], 5)
        self.assertEqual(s["to_send"], 68)
        text = camp_mod.format_preview(st, pv.campaign_id)
        self.assertIn("Будет отправлено:       68", text)
        self.assertEqual(pv.sample["subject"], "Имя8, предложение")
        # до подтверждения ничего не отправлено и в CRM не записано
        self.assertFalse([c for c in fb.calls if c.endswith(".add")])

    def test_pagination_1000_deals(self):
        fb = FakeBitrix()
        for i in range(1, 1001):
            fb.deals.append({"ID": str(i), "CATEGORY_ID": "4", "STAGE_ID": "C4:NEW"})
        deals = list(make(fb).list_all("crm.deal.list", {"filter": {"STAGE_ID": "C4:NEW"}, "select": ["ID"]}))
        self.assertEqual(len(deals), 1000)
        self.assertEqual(len({d["ID"] for d in deals}), 1000)

    def _multi_portal(self):
        fb = FakeBitrix()
        fb.deals.append({"ID": "1", "TITLE": "d", "CATEGORY_ID": "4", "STAGE_ID": "C4:NEW"})
        fb.contacts = {
            1: {"ID": "1", "NAME": "Осн", "EMAIL": [{"VALUE": "main@x.ru", "VALUE_TYPE": "WORK"}]},
            2: {"ID": "2", "NAME": "Втор", "EMAIL": [{"VALUE": "second@x.ru", "VALUE_TYPE": "WORK"}]},
            3: {"ID": "3", "NAME": "Без", "EMAIL": []},
        }
        fb.links[1] = [{"CONTACT_ID": 2, "SORT": 10, "IS_PRIMARY": "N"},
                       {"CONTACT_ID": 1, "SORT": 20, "IS_PRIMARY": "Y"},
                       {"CONTACT_ID": 3, "SORT": 30, "IS_PRIMARY": "N"}]
        return fb

    def test_contact_modes(self):
        for mode, emails in (("primary", ["main@x.ru"]), ("all", ["main@x.ru", "second@x.ru"]),
                             ("valid_only", ["main@x.ru", "second@x.ru"])):
            col = collect(make(self._multi_portal()), Storage(":memory:"), {"STAGE_ID": "C4:NEW"},
                          Options(contact_mode=mode, campaign_key="k"))
            got = [r["email"] for r in col.rows if r["status"] == "queued"]
            self.assertEqual(got, emails, mode)

    def test_same_email_in_two_deals_sent_once(self):
        fb = cold_portal()
        fb.links[9] = fb.links[8]  # один и тот же контакт в сделках 8 и 9
        col = collect(make(fb), Storage(":memory:"), {"STAGE_ID": "C4:NEW"}, Options(campaign_key="k"))
        self.assertEqual(col.stats["duplicates"], 1)


class SendTest(unittest.TestCase):
    def test_requires_confirmation(self):
        fb = cold_portal()
        st = Storage(":memory:")
        pv = camp_mod.preview(make(fb), st, settings(), campaign(), progress=lambda s: None)
        with self.assertRaises(camp_mod.NotConfirmed):
            camp_mod.send(st, pv.campaign_id, DryRunSender(), confirmed=False, progress=lambda s: None)

    def test_send_via_bitrix_binds_to_deal_and_contact(self):
        fb = cold_portal()
        client, st = make(fb), Storage(":memory:")
        pv = camp_mod.preview(client, st, settings(), campaign(), progress=lambda s: None)
        sender = BitrixEmailSender(client, "sales@example.com")
        counts = camp_mod.send(st, pv.campaign_id, sender, confirmed=True, rate_per_minute=0,
                               reports_dir="/tmp/bitrix-mailer-tests", progress=lambda s: None)
        self.assertEqual(counts["sent"], 73)
        self.assertEqual(len(fb.activities), 73)
        a = fb.activities[1000]
        self.assertEqual((a["OWNER_TYPE_ID"], a["TYPE_ID"], a["DIRECTION"]), ("2", "4", "2"))
        deal_id = int(a["OWNER_ID"])
        self.assertEqual(a["COMMUNICATIONS"][0]["ENTITY_ID"], 1000 + deal_id)
        self.assertIn(f"Сделка {deal_id}", a["DESCRIPTION"])
        self.assertTrue(all(r["timeline_verified"] for r in st.recipients(pv.campaign_id, "sent")))
        # ничего, кроме писем, в CRM не создано
        writes = {c for c in fb.calls if not c.startswith(("crm.deal.list", "crm.contact.list"))
                  and (".add" in c or ".update" in c or ".delete" in c)}
        self.assertEqual(writes, {"crm.activity.add"})
        self.assertIn("Отправлено:                73", camp_mod.format_report(st, pv.campaign_id))

        # повторный запуск той же рассылки: все уже получили письмо
        pv2 = camp_mod.preview(client, st, settings(), campaign(), progress=lambda s: None)
        self.assertEqual(pv2.stats["already_sent"], 73)
        self.assertEqual(pv2.stats["to_send"], 0)

    def test_interrupted_send_not_repeated(self):
        st = Storage(":memory:")
        pv = camp_mod.preview(make(cold_portal()), st, settings(), campaign(), progress=lambda s: None)
        first = st.recipients(pv.campaign_id, "queued")[0]
        st.update_recipient(first["id"], status="sending")
        sender = DryRunSender()
        camp_mod.send(st, pv.campaign_id, sender, confirmed=True, rate_per_minute=0,
                      reports_dir="/tmp/bitrix-mailer-tests", progress=lambda s: None)
        self.assertEqual(len(sender.sent), 72)
        self.assertEqual(st.counts(pv.campaign_id).get("unknown"), 1)


class HelpersTest(unittest.TestCase):
    def test_email_validation(self):
        self.assertIsNone(validation.check("Ivan.Petrov+crm@Example.co.uk"))
        self.assertIsNone(validation.check("info@пример.рф"))
        for bad in ("", "broken@", "a@b", "a b@c.ru", "a@@c.ru", "a@c..ru"):
            self.assertIsNotNone(validation.check(bad), bad)

    def test_template_escapes_html(self):
        out = template.render("<b>{{contact.NAME}}</b>", {"contact": {"NAME": "<script>"}}, escape=True)
        self.assertEqual(out, "<b>&lt;script&gt;</b>")
        self.assertEqual(template.render('{{contact.NAME|default:"коллега"}}', {"contact": {}}, False), "коллега")
        with self.assertRaises(template.TemplateError):
            template.placeholders("{{ name }}")


if __name__ == "__main__":
    unittest.main()
