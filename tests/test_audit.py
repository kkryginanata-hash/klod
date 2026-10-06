import unittest

from bitrix_mailer import audit
from bitrix_mailer.client import CrmWriteForbidden

from tests.fake_bitrix import FakeBitrix
from tests.test_mailer import make


def portal():
    fb = FakeBitrix()
    mk = lambda i, comp, contact=None: fb.deals.append(
        {"ID": str(i), "TITLE": f"Сделка {i}", "CATEGORY_ID": "4", "STAGE_ID": "C4:NEW", "ASSIGNED_BY_ID": "7",
         "COMPANY_ID": str(comp), "CONTACT_ID": str(contact) if contact else None})
    # 1: у компании пусто, у контакта сделки только мусор → нет ничего
    fb.companies[1] = {"ID": "1", "TITLE": "Пустая", "EMAIL": [], "PHONE": [{"VALUE": "-"}], "INDUSTRY": "IT"}
    fb.contacts[11] = {"ID": "11", "NAME": "Иван", "EMAIL": [{"VALUE": "broken@"}], "PHONE": []}
    mk(1, 1, 11)
    fb.links[1] = [{"CONTACT_ID": 11, "IS_PRIMARY": "Y"}]
    # 2: телефон только у контакта компании (не сделки), две сделки на компанию
    fb.companies[2] = {"ID": "2", "TITLE": "С телефоном", "EMAIL": [], "PHONE": []}
    fb.contacts[22] = {"ID": "22", "NAME": "Пётр", "EMAIL": [], "PHONE": [{"VALUE": "+7 (910) 000-00-00"}]}
    fb.company_links[2] = [{"CONTACT_ID": 22}]
    mk(2, 2), mk(3, 2)
    # 3: почта и телефон в карточке компании, контактов нет
    fb.companies[3] = {"ID": "3", "TITLE": "Полная", "EMAIL": [{"VALUE": "info@full.ru"}],
                       "PHONE": [{"VALUE": "84951234567"}]}
    mk(4, 3)
    # 4: компании нет, контактов нет
    mk(5, 0)
    # 5: компания без контактов и данных
    fb.companies[5] = {"ID": "5", "TITLE": "Голая", "EMAIL": [], "PHONE": []}
    mk(6, 5)
    return fb


class AuditTest(unittest.TestCase):
    def test_statuses(self):
        fb = portal()
        res = audit.run(make(fb), {"=CATEGORY_ID": "4"})
        by = {r["company"]: r for r in res.rows}
        self.assertEqual(by["Пустая"]["status"], audit.STATUS_NONE)
        self.assertEqual(by["С телефоном"]["status"], audit.STATUS_NO_EMAIL)
        self.assertEqual(by["С телефоном"]["deal_ids"], "2, 3")
        self.assertEqual(by["Полная"]["status"], audit.STATUS_OK)
        self.assertEqual(by["Голая"]["status"], audit.STATUS_NONE)
        s = res.stats
        self.assertEqual((s["deals"], s["companies"], s["deals_without_company"]), (6, 4, 1))
        self.assertEqual((s[audit.STATUS_NONE], s["companies_without_contacts"], s["no_company_none"]), (2, 1, 1))
        self.assertIn("Пустая", audit.format_result(res))
        # только чтение
        self.assertTrue(all(m.endswith((".list", ".get", ".fields")) or m == "batch" for m in fb.calls), fb.calls)

    def test_company_writes_still_forbidden(self):
        with self.assertRaises(CrmWriteForbidden):
            make(portal()).call("crm.company.update", {"id": 1, "fields": {"TITLE": "x"}})


if __name__ == "__main__":
    unittest.main()
