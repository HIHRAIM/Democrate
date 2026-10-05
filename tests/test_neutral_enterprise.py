"""Small isolated economy check; never opens the production dem.db."""
import calendar
import os
import tempfile
import time
import unittest
from datetime import date, timedelta


class NeutralEnterpriseTest(unittest.TestCase):
    def test_backfill_provision_transfer_and_opt_out(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            db = None
            try:
                os.chdir(directory)
                import db
                from economy.consumption import consume_for_server, provision_report
                from economy.tasks import prev_month

                db.init()
                db.cur.execute("INSERT INTO unions (code, created_at) VALUES ('TEST', 0)")
                db.conn.commit()
                db.setup_chat("discord", "123", "TEST", "operator", "Example")
                original = db.ensure_neutral_enterprise("discord", "123")
                self.assertEqual(original["code"], db.ensure_neutral_enterprise("discord", "123")["code"])
                yesterday = date(2026, 1, 1)
                stamp = calendar.timegm(time.strptime(yesterday.isoformat(), "%Y-%m-%d")) + 3600
                for _ in range(4):
                    db.record_neutral_activity("discord", "123", "discord", "member", stamp)
                today = (yesterday + timedelta(days=1)).isoformat()
                now = stamp + 86400
                meal = consume_for_server("discord", "123", today, now)
                self.assertEqual(meal["population"], 1)
                self.assertTrue(all(abs(c["satisfaction"] / c["need"] - 0.4) < 1e-9
                                    for c in meal["categories"].values()))
                self.assertEqual(db.cur.execute("SELECT COUNT(*) FROM server_production").fetchone()[0], 0)
                report_now = int(time.time())
                month = prev_month(time.strftime("%Y-%m", time.gmtime(report_now)))
                db.save_server_task("discord", "123", month, "{}")
                task = db.get_server_task("discord", "123", month)
                db.set_server_task_completed(task["id"], True)
                report = provision_report("discord", "123", report_now)
                self.assertAlmostEqual(report["score"], 0.4)
                db.set_enterprise_leader(original["code"], "discord", "new-owner", "Owner")
                self.assertEqual(db.get_enterprise(original["code"])["neutral"], 0)
                self.assertEqual(db.get_enterprise(original["code"])["founder_id"], "new-owner")
                replacement = db.ensure_neutral_enterprise("discord", "123")
                self.assertNotEqual(original["code"], replacement["code"])
                self.assertTrue(db.is_enterprise_worker_anywhere("discord", "new-owner"))
                db.delete_enterprise(replacement["code"])
                self.assertIsNone(db.ensure_neutral_enterprise("discord", "123"))
                db.setup_chat("discord", "123", "TEST", "operator", "Example")
                self.assertIsNotNone(db.ensure_neutral_enterprise("discord", "123"))
                db.remove_chat("123")
                self.assertEqual(db.cur.execute(
                    "SELECT COUNT(*) FROM enterprises WHERE platform='discord'"
                    " AND server_id='123' AND neutral=1").fetchone()[0], 0)
                self.assertEqual(db.cur.execute(
                    "SELECT COUNT(*) FROM neutral_activity WHERE server_id='123'").fetchone()[0], 0)
            finally:
                if db is not None:
                    db.conn.close()
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
