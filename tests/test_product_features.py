import unittest
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from fastapi import HTTPException
from fastapi import UploadFile
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from financial_engine import compute_cash_forecast
from invoice_extraction import (
    InvoiceFileError,
    _normalize_gemini_fields,
    extract_invoice_document,
    parse_invoice_fields,
)
from main import (
    create_bank_referral,
    create_receivable,
    extract_invoice,
    get_company_consent,
    get_rm_portfolio,
    settle_receivable,
    update_company_consent,
)
from models import Company, ConsentEvent, PayableObligation, ReceivableInvoice, Transaction
from schemas import ConsentUpdate, ReceivableCreate, ReferralCreate


class ProductFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_cash_forecast_includes_invoice_payable_and_historical_outflow(self):
        today = date.today()
        async with self.session_factory() as db:
            db.add(Company(id="comp_001", name="Demo", current_cash_balance=Decimal("1000")))
            db.add(Transaction(
                company_id="comp_001",
                tx_type="OUTFLOW",
                category="Payroll",
                vendor_or_client="Staff",
                amount=Decimal("90"),
                tx_date=today - timedelta(days=1),
            ))
            db.add(ReceivableInvoice(
                company_id="comp_001",
                invoice_number="INV-1",
                customer="Customer",
                amount=Decimal("500"),
                issue_date=today - timedelta(days=2),
                due_date=today + timedelta(days=2),
            ))
            db.add(PayableObligation(
                company_id="comp_001",
                title="Tax",
                category="Tax",
                amount=Decimal("300"),
                due_date=today + timedelta(days=3),
            ))
            await db.commit()

            forecast = await compute_cash_forecast(db, "comp_001")

        self.assertEqual(len(forecast["points"]), 31)
        self.assertEqual(forecast["points"][1]["balance"], 999)
        self.assertEqual(forecast["points"][2]["balance"], 1498)
        self.assertEqual(forecast["points"][3]["balance"], 1197)
        self.assertEqual(forecast["expected_inflows"], 500)
        self.assertEqual(forecast["scheduled_payables"], 300)

    async def test_rm_signals_and_referrals_follow_separate_consents(self):
        async with self.session_factory() as db:
            db.add(Company(id="comp_001", name="Demo", current_cash_balance=Decimal("1000")))
            await db.commit()

            self.assertEqual(
                await get_company_consent("comp_001", db),
                {"share_portfolio_signals": False, "allow_referral_sharing": False},
            )
            with self.assertRaises(HTTPException) as error:
                await create_bank_referral(
                    "comp_001",
                    ReferralCreate(estimated_need=Decimal("100"), reason="Cash planning"),
                    db,
                )
            self.assertEqual(error.exception.status_code, 403)

            await update_company_consent(
                "comp_001",
                ConsentUpdate(share_portfolio_signals=True, allow_referral_sharing=True),
                db,
            )
            self.assertEqual((await get_rm_portfolio(db))["consented_businesses"], 1)
            await create_bank_referral(
                "comp_001",
                ReferralCreate(estimated_need=Decimal("100"), reason="Cash planning"),
                db,
            )
            events = (await db.execute(select(ConsentEvent))).scalars().all()
            self.assertEqual(len(events), 2)

            await update_company_consent(
                "comp_001",
                ConsentUpdate(share_portfolio_signals=False, allow_referral_sharing=False),
                db,
            )
            self.assertEqual((await get_rm_portfolio(db))["consented_businesses"], 0)

    async def test_receivable_settlement_updates_cash_and_rejects_duplicate_invoice(self):
        today = date.today()
        invoice_data = ReceivableCreate(
            company_id="comp_001",
            invoice_number="INV-42",
            customer="Customer",
            amount=Decimal("250"),
            issue_date=today - timedelta(days=2),
            due_date=today + timedelta(days=10),
        )
        async with self.session_factory() as db:
            db.add(Company(id="comp_001", name="Demo", current_cash_balance=Decimal("1000")))
            await db.commit()
            result = await create_receivable(invoice_data, db)
            self.assertEqual(result["status"], "created")

            with self.assertRaises(HTTPException) as error:
                await create_receivable(invoice_data, db)
            self.assertEqual(error.exception.status_code, 409)

            settled = await settle_receivable(result["invoice_id"], db)
            company = await db.get(Company, "comp_001")
            invoice = await db.get(ReceivableInvoice, result["invoice_id"])

        self.assertEqual(settled["new_cash_balance"], 1250)
        self.assertEqual(float(company.current_cash_balance), 1250)
        self.assertEqual(invoice.status, "PAID")

    def test_invoice_text_parser_extracts_labeled_fields_and_rejects_bad_files(self):
        fields = parse_invoice_fields(
            "Supplier: Lucid Tech Services\n"
            "Bill To: Northwind Stores\n"
            "Invoice No: LT-2026-041\n"
            "Invoice Date: 12/11/2026\n"
            "Due Date: 27/11/2026\n"
            "Grand Total: INR 1,23,456.78"
        )

        self.assertEqual(fields, {
            "invoice_number": "LT-2026-041",
            "supplier": "Lucid Tech Services",
            "customer": "Northwind Stores",
            "amount": "123456.78",
            "issue_date": "2026-11-12",
            "due_date": "2026-11-27",
        })
        with self.assertRaises(InvoiceFileError):
            extract_invoice_document(b"not an invoice", "invoice.txt")
        with self.assertRaises(InvoiceFileError):
            extract_invoice_document(b"%PDF-malformed", "invoice.pdf")
        with self.assertRaises(InvoiceFileError):
            extract_invoice_document(b"x" * (12 * 1024 * 1024 + 1), "invoice.png")

    def test_gemini_invoice_fields_are_normalized(self):
        fields = _normalize_gemini_fields({
            "invoice_number": " INV-42 ",
            "supplier": "Supplier Ltd",
            "customer": "not found",
            "amount": "INR 1,23,456.78",
            "issue_date": "2026-11-12",
            "due_date": "not found",
        })

        self.assertEqual(fields, {
            "invoice_number": "INV-42",
            "supplier": "Supplier Ltd",
            "customer": None,
            "amount": "123456.78",
            "issue_date": "2026-11-12",
            "due_date": None,
        })

    async def test_invoice_upload_extracts_fields_from_local_image(self):
        image = Image.new("RGB", (1400, 800), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=42)
        lines = [
            "Supplier: Lucid Tech Services",
            "Bill To: Northwind Stores",
            "Invoice No: LT-2026-041",
            "Invoice Date: 12/11/2026",
            "Due Date: 27/11/2026",
            "Grand Total: INR 1,23,456.78",
        ]
        for index, line in enumerate(lines):
            draw.text((55, 45 + index * 100), line, fill="black", font=font)
        file_bytes = BytesIO()
        image.save(file_bytes, format="PNG")

        gemini_fields = {
            "invoice_number": "LT-2026-041",
            "supplier": "Lucid Tech Services",
            "customer": "Northwind Stores",
            "amount": "123456.78",
            "issue_date": "2026-11-12",
            "due_date": "2026-11-27",
        }
        with patch("invoice_extraction._extract_fields_with_gemini", return_value=gemini_fields) as gemini:
            result = await extract_invoice(UploadFile(
                filename="invoice.png",
                file=BytesIO(file_bytes.getvalue()),
            ))
            gemini.assert_called_once()

        self.assertEqual(result["fields"]["invoice_number"], "LT-2026-041")
        self.assertEqual(result["fields"]["customer"], "Northwind Stores")
        self.assertEqual(result["fields"]["amount"], "123456.78")
        self.assertEqual(result["extraction_provider"], "Gemini")
        self.assertGreater(result["ocr_confidence"], 0.5)

        pdf_bytes = BytesIO()
        image.save(pdf_bytes, format="PDF", resolution=150)
        with patch("invoice_extraction._extract_fields_with_gemini", return_value=gemini_fields):
            pdf_result = extract_invoice_document(pdf_bytes.getvalue(), "invoice.pdf")
        self.assertEqual(pdf_result["fields"]["issue_date"], "2026-11-12")
        self.assertEqual(pdf_result["fields"]["due_date"], "2026-11-27")


if __name__ == "__main__":
    unittest.main()
