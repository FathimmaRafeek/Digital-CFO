import unittest
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from financial_engine import compute_financial_metrics, run_autonomous_alert_scan
from main import parse_transactions_csv, upload_transactions
from models import CFOAlert, Company, PayableObligation


class CSVRequest:
    def __init__(self, content: bytes):
        self.content = content

    async def body(self) -> bytes:
        return self.content


class TransactionUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_dashboard_starts_empty_and_csv_upload_populates_insights(self):
        previous_month = date.today().replace(day=1) - timedelta(days=1)
        csv_content = (
            "date,type,category,vendor,amount\n"
            f"{previous_month.isoformat()},OUTFLOW,Payroll,Staff,400\n"
            f"{date.today().isoformat()},INFLOW,Sales,Customer,300\n"
            f"{date.today().isoformat()},OUTFLOW,Payroll,Staff,500\n"
        ).encode()

        async with self.session_factory() as db:
            empty_metrics = await compute_financial_metrics(db, "comp_001")
            self.assertFalse(empty_metrics["has_data"])
            self.assertIsNone(empty_metrics["runway_months"])

            result = await upload_transactions(
                CSVRequest(csv_content),
                company_name="Test Company",
                currency="USD",
                opening_cash=Decimal("2000"),
                db=db,
            )
            metrics = await compute_financial_metrics(db, "comp_001")

        self.assertEqual(result["transactions_imported"], 3)
        self.assertTrue(metrics["has_data"])
        self.assertEqual(metrics["company_name"], "Test Company")
        self.assertEqual(metrics["current_cash"], 1400)
        self.assertEqual(metrics["current_month_expense"], 500)
        self.assertEqual(metrics["runway_months"], 7.0)
        self.assertTrue(any("25.0% up" in insight for insight in metrics["dashboard_insights"]))

    def test_csv_rejects_invalid_rows(self):
        with self.assertRaises(HTTPException) as error:
            parse_transactions_csv(b"date,type,category,vendor,amount\n2026-11-18,OTHER,Payroll,Staff,10\n")

        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("INFLOW or OUTFLOW", error.exception.detail)

    async def test_duplicate_payables_have_individual_current_alerts(self):
        async with self.session_factory() as db:
            db.add(Company(id="comp_001", name="Test Company", current_cash_balance=Decimal("1000")))
            db.add_all([
                PayableObligation(
                    company_id="comp_001",
                    title="Insurance",
                    category="Insurance",
                    amount=Decimal("100"),
                    due_date=date.today(),
                ),
                PayableObligation(
                    company_id="comp_001",
                    title="Insurance",
                    category="Insurance",
                    amount=Decimal("200"),
                    due_date=date.today() + timedelta(days=3),
                ),
            ])
            await db.commit()
            await run_autonomous_alert_scan(db, "comp_001")

            alerts = (await db.execute(
                select(CFOAlert).where(
                    CFOAlert.company_id == "comp_001",
                    CFOAlert.alert_type == "DUE_DATE",
                    CFOAlert.is_resolved == False,
                )
            )).scalars().all()
            self.assertEqual(len(alerts), 2)
            self.assertEqual(
                {alert.message.rsplit(" is ", 1)[-1] for alert in alerts},
                {"due today.", "due in 3 days."},
            )

            first_payable = (await db.execute(
                select(PayableObligation).where(PayableObligation.id == 1)
            )).scalar_one()
            first_payable.is_paid = True
            await db.commit()
            await run_autonomous_alert_scan(db, "comp_001")
            active_alerts = (await db.execute(
                select(CFOAlert).where(
                    CFOAlert.company_id == "comp_001",
                    CFOAlert.alert_type == "DUE_DATE",
                    CFOAlert.is_resolved == False,
                )
            )).scalars().all()
            self.assertEqual(len(active_alerts), 1)
            self.assertIn("due in 3 days", active_alerts[0].message)
            resolved_alerts = (await db.execute(
                select(CFOAlert).where(
                    CFOAlert.company_id == "comp_001",
                    CFOAlert.alert_type == "DUE_DATE",
                    CFOAlert.is_resolved == True,
                )
            )).scalars().all()
            self.assertEqual(len(resolved_alerts), 1)


if __name__ == "__main__":
    unittest.main()
