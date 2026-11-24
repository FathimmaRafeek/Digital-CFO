import csv
import io
import asyncio
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import os

from database import init_db, get_db, AsyncSessionLocal
from models import (
    BankReferral,
    CFOAlert,
    Company,
    CompanyConsent,
    ConsentEvent,
    PayableObligation,
    ReceivableInvoice,
    Transaction,
)
from schemas import (
    CFOQueryRequest,
    ConsentUpdate,
    PayableCreate,
    ReceivableCreate,
    ReferralCreate,
    TransactionCreate,
)
from financial_engine import compute_cash_forecast, compute_financial_metrics, run_autonomous_alert_scan
from cfo_agents import cfo_agent_graph
from invoice_extraction import (
    MAX_FILE_BYTES,
    InvoiceFileError,
    InvoiceGeminiError,
    InvoiceUnreadableError,
    extract_invoice_document,
)

scheduler = AsyncIOScheduler()

async def scheduled_cfo_watchdog():
    async with AsyncSessionLocal() as db:
        companies = (await db.execute(select(Company))).scalars().all()
        for comp in companies:
            await run_autonomous_alert_scan(db, comp.id)


async def seed_demo_data():
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(Company).where(Company.id == "comp_001"))).scalar_one_or_none()
        today = date.today()
        if not existing:
            previous_month = today.replace(day=1) - timedelta(days=5)
            db.add(Company(
                id="comp_001",
                name="NexaTech Solutions Pvt Ltd",
                current_cash_balance=Decimal("1850000.00"),
                currency="INR",
            ))
            db.add_all([
                Transaction(company_id="comp_001", tx_type="INFLOW", category="Client Retainer", vendor_or_client="Apex Global Corp", amount=Decimal("950000.00"), tx_date=today.replace(day=2)),
                Transaction(company_id="comp_001", tx_type="OUTFLOW", category="Payroll", vendor_or_client="Monthly Engineering Payroll", amount=Decimal("420000.00"), tx_date=today.replace(day=5)),
                Transaction(company_id="comp_001", tx_type="OUTFLOW", category="Cloud Infrastructure", vendor_or_client="AWS Enterprise", amount=Decimal("85000.00"), tx_date=today.replace(day=8)),
                Transaction(company_id="comp_001", tx_type="OUTFLOW", category="Marketing & Ads", vendor_or_client="Unapproved Agency Surge", amount=Decimal("310000.00"), tx_date=today.replace(day=12), description="High-risk unbudgeted campaign spike"),
                Transaction(company_id="comp_001", tx_type="INFLOW", category="Client Retainer", vendor_or_client="Apex Global Corp", amount=Decimal("900000.00"), tx_date=previous_month),
                Transaction(company_id="comp_001", tx_type="OUTFLOW", category="Payroll", vendor_or_client="Monthly Engineering Payroll", amount=Decimal("410000.00"), tx_date=previous_month),
                Transaction(company_id="comp_001", tx_type="OUTFLOW", category="Cloud Infrastructure", vendor_or_client="AWS Enterprise", amount=Decimal("78000.00"), tx_date=previous_month),
            ])
            db.add_all([
                PayableObligation(company_id="comp_001", title="Cyber & Liability Insurance Renewal", category="Insurance", amount=Decimal("145000.00"), due_date=today + timedelta(days=3), priority="CRITICAL"),
                PayableObligation(company_id="comp_001", title="Monthly GST & TDS Filing Payment", category="Tax/Compliance", amount=Decimal("112000.00"), due_date=today + timedelta(days=5), priority="HIGH"),
            ])
            await db.commit()

        invoice_count = await db.scalar(
            select(ReceivableInvoice.id).where(ReceivableInvoice.company_id == "comp_001").limit(1)
        )
        if invoice_count is None:
            db.add_all([
                ReceivableInvoice(
                    company_id="comp_001",
                    invoice_number="INV-1048",
                    customer="Apex Global Corp",
                    amount=Decimal("480000.00"),
                    issue_date=today - timedelta(days=18),
                    due_date=today + timedelta(days=5),
                    expected_collection_date=today + timedelta(days=5),
                ),
                ReceivableInvoice(
                    company_id="comp_001",
                    invoice_number="INV-1049",
                    customer="Northstar Retail",
                    amount=Decimal("325000.00"),
                    issue_date=today - timedelta(days=12),
                    due_date=today + timedelta(days=12),
                ),
                ReceivableInvoice(
                    company_id="comp_001",
                    invoice_number="INV-1037",
                    customer="Meridian Labs",
                    amount=Decimal("165000.00"),
                    issue_date=today - timedelta(days=42),
                    due_date=today - timedelta(days=12),
                ),
            ])
            await db.commit()

        consent = await db.get(CompanyConsent, "comp_001")
        if consent is None:
            db.add(CompanyConsent(company_id="comp_001"))
            await db.commit()

        await run_autonomous_alert_scan(db, "comp_001")


async def get_or_create_company(
    db: AsyncSession,
    company_id: str,
    name: str = "My Company",
    currency: str = "INR",
    opening_cash: Decimal = Decimal("0"),
) -> Company:
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if company is None:
        company = Company(
            id=company_id,
            name=name,
            currency=currency,
            current_cash_balance=opening_cash,
        )
        db.add(company)
    return company


def parse_transactions_csv(content: bytes) -> list[dict]:
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames
        if not headers:
            raise HTTPException(status_code=400, detail="The CSV file is empty or has no header row.")

        aliases = {
            "tx_date": {"date", "tx_date"},
            "tx_type": {"type", "tx_type"},
            "category": {"category"},
            "vendor_or_client": {"vendor", "vendor_or_client"},
            "amount": {"amount"},
        }
        fields = {}
        for header in headers:
            normalized = (header or "").strip().lower()
            for field, names in aliases.items():
                if normalized in names:
                    if field in fields:
                        raise HTTPException(status_code=400, detail=f"The CSV has more than one column for {field}.")
                    fields[field] = header

        missing = [field for field in aliases if field not in fields]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required CSV columns: {', '.join(missing)}.",
            )

        transactions = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise HTTPException(status_code=400, detail=f"Row {row_number} has more values than the header.")
            if not any((value or "").strip() for value in row.values()):
                continue

            tx_type = (row[fields["tx_type"]] or "").strip().upper()
            category = (row[fields["category"]] or "").strip()
            vendor = (row[fields["vendor_or_client"]] or "").strip()
            if tx_type not in {"INFLOW", "OUTFLOW"}:
                raise HTTPException(status_code=400, detail=f"Row {row_number}: type must be INFLOW or OUTFLOW.")
            if not category or len(category) > 80:
                raise HTTPException(status_code=400, detail=f"Row {row_number}: category must be 1–80 characters.")
            if not vendor or len(vendor) > 150:
                raise HTTPException(status_code=400, detail=f"Row {row_number}: vendor must be 1–150 characters.")

            try:
                tx_date = date.fromisoformat((row[fields["tx_date"]] or "").strip())
                amount = Decimal((row[fields["amount"]] or "").strip())
            except (ValueError, InvalidOperation):
                raise HTTPException(status_code=400, detail=f"Row {row_number}: enter a valid YYYY-MM-DD date and amount.")
            if not amount.is_finite() or amount <= 0:
                raise HTTPException(status_code=400, detail=f"Row {row_number}: amount must be greater than zero.")

            transactions.append({
                "tx_type": tx_type,
                "category": category,
                "vendor_or_client": vendor,
                "amount": amount,
                "tx_date": tx_date,
            })
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Upload a UTF-8 encoded CSV file.")
    except csv.Error:
        raise HTTPException(status_code=400, detail="The CSV file could not be parsed.")

    if not transactions:
        raise HTTPException(status_code=400, detail="The CSV contains no transaction rows.")
    return transactions

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.path.exists("static"):
        os.makedirs("static", exist_ok=True)
    await init_db()
    await seed_demo_data()
    await scheduled_cfo_watchdog()
    scheduler.add_job(scheduled_cfo_watchdog, "interval", minutes=15)
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="LucidPlus SME Digital CFO", version="1.0.0", lifespan=lifespan)
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_dashboard():
    return FileResponse("static/index.html")

@app.get("/api/v1/dashboard/{company_id}")
async def get_dashboard(company_id: str, db: AsyncSession = Depends(get_db)):
    metrics = await compute_financial_metrics(db, company_id)
    alerts_res = await db.execute(select(CFOAlert).where(CFOAlert.company_id == company_id, CFOAlert.is_resolved == False).order_by(CFOAlert.created_at.desc()))
    alerts = [{"id": a.id, "type": a.alert_type, "severity": a.severity, "title": a.title, "message": a.message} for a in alerts_res.scalars().all()]
    return {**metrics, "alerts": alerts}

@app.get("/api/v1/forecast/{company_id}")
async def get_cash_forecast(
    company_id: str,
    days: int = Query(default=30, ge=7, le=90),
    db: AsyncSession = Depends(get_db),
):
    return await compute_cash_forecast(db, company_id, days)

@app.get("/api/v1/receivables/{company_id}")
async def list_receivables(company_id: str, db: AsyncSession = Depends(get_db)):
    invoices = (await db.execute(
        select(ReceivableInvoice)
        .where(ReceivableInvoice.company_id == company_id)
        .order_by(ReceivableInvoice.due_date.asc())
    )).scalars().all()
    today = date.today()
    rows = []
    for invoice in invoices:
        overdue_days = max(0, (today - invoice.due_date).days) if invoice.status == "OPEN" else 0
        aging_bucket = (
            "Current" if overdue_days == 0 else
            "1–30 days" if overdue_days <= 30 else
            "31–60 days" if overdue_days <= 60 else "60+ days"
        )
        rows.append({
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "customer": invoice.customer,
            "amount": float(invoice.amount),
            "issue_date": invoice.issue_date.isoformat(),
            "due_date": invoice.due_date.isoformat(),
            "expected_collection_date": (
                invoice.expected_collection_date.isoformat()
                if invoice.expected_collection_date else None
            ),
            "status": invoice.status,
            "overdue_days": overdue_days,
            "aging_bucket": aging_bucket,
        })
    return {"receivables": rows, "open_total": sum(row["amount"] for row in rows if row["status"] == "OPEN")}

@app.post("/api/v1/invoices/extract")
async def extract_invoice(file: UploadFile = File(...)):
    filename = file.filename or ""
    try:
        content = await file.read(MAX_FILE_BYTES + 1)
        return await asyncio.to_thread(extract_invoice_document, content, filename)
    except InvoiceFileError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except InvoiceUnreadableError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except InvoiceGeminiError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    finally:
        await file.close()

@app.post("/api/v1/receivables")
async def create_receivable(invoice: ReceivableCreate, db: AsyncSession = Depends(get_db)):
    if invoice.due_date < invoice.issue_date:
        raise HTTPException(status_code=400, detail="Invoice due date cannot be before its issue date.")
    company = await get_or_create_company(db, invoice.company_id)
    duplicate = await db.scalar(
        select(ReceivableInvoice.id).where(
            ReceivableInvoice.company_id == invoice.company_id,
            ReceivableInvoice.invoice_number == invoice.invoice_number,
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="An invoice with this number already exists.")
    new_invoice = ReceivableInvoice(**invoice.model_dump())
    db.add(new_invoice)
    await db.commit()
    return {"status": "created", "invoice_id": new_invoice.id, "company": company.id}

@app.post("/api/v1/receivables/{invoice_id}/settle")
async def settle_receivable(invoice_id: int, db: AsyncSession = Depends(get_db)):
    invoice = await db.get(ReceivableInvoice, invoice_id)
    if invoice is None or invoice.status != "OPEN":
        raise HTTPException(status_code=404, detail="Open invoice not found.")
    company = await db.get(Company, invoice.company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found for this invoice.")
    invoice.status = "PAID"
    company.current_cash_balance += invoice.amount
    db.add(Transaction(
        company_id=invoice.company_id,
        tx_type="INFLOW",
        category="Accounts receivable",
        vendor_or_client=invoice.customer,
        amount=invoice.amount,
        tx_date=date.today(),
        description=f"Settled invoice {invoice.invoice_number}",
    ))
    await db.commit()
    await run_autonomous_alert_scan(db, invoice.company_id)
    return {"status": "settled", "new_cash_balance": float(company.current_cash_balance)}

@app.get("/api/v1/consent/{company_id}")
async def get_company_consent(company_id: str, db: AsyncSession = Depends(get_db)):
    consent = await db.get(CompanyConsent, company_id)
    return {
        "share_portfolio_signals": bool(consent and consent.share_portfolio_signals),
        "allow_referral_sharing": bool(consent and consent.allow_referral_sharing),
    }

@app.put("/api/v1/consent/{company_id}")
async def update_company_consent(
    company_id: str,
    update: ConsentUpdate,
    db: AsyncSession = Depends(get_db),
):
    if await db.get(Company, company_id) is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    consent = await db.get(CompanyConsent, company_id)
    if consent is None:
        consent = CompanyConsent(company_id=company_id)
        db.add(consent)
        old_values = {"portfolio_signals": False, "referral_sharing": False}
    else:
        old_values = {
            "portfolio_signals": consent.share_portfolio_signals,
            "referral_sharing": consent.allow_referral_sharing,
        }
    new_values = {
        "portfolio_signals": update.share_portfolio_signals,
        "referral_sharing": update.allow_referral_sharing,
    }
    consent.share_portfolio_signals = update.share_portfolio_signals
    consent.allow_referral_sharing = update.allow_referral_sharing
    for purpose, enabled in new_values.items():
        if old_values[purpose] != enabled:
            db.add(ConsentEvent(company_id=company_id, purpose=purpose, enabled=enabled))
    await db.commit()
    return new_values

@app.post("/api/v1/referrals/{company_id}")
async def create_bank_referral(
    company_id: str,
    referral: ReferralCreate,
    db: AsyncSession = Depends(get_db),
):
    consent = await db.get(CompanyConsent, company_id)
    if consent is None or not consent.allow_referral_sharing:
        raise HTTPException(status_code=403, detail="Enable referral-sharing consent before requesting a bank referral.")
    record = BankReferral(company_id=company_id, **referral.model_dump())
    db.add(record)
    await db.commit()
    return {"status": "customer_approved", "referral_id": record.id}

@app.get("/api/v1/referrals/{company_id}")
async def list_bank_referrals(company_id: str, db: AsyncSession = Depends(get_db)):
    referrals = (await db.execute(
        select(BankReferral)
        .where(BankReferral.company_id == company_id)
        .order_by(BankReferral.created_at.desc())
    )).scalars().all()
    return {"referrals": [
        {
            "id": item.id,
            "estimated_need": float(item.estimated_need),
            "reason": item.reason,
            "status": item.status,
            "created_at": item.created_at.isoformat(),
        }
        for item in referrals
    ]}

@app.get("/api/v1/rm/portfolio")
async def get_rm_portfolio(db: AsyncSession = Depends(get_db)):
    consented_ids = (await db.execute(
        select(CompanyConsent.company_id).where(CompanyConsent.share_portfolio_signals == True)
    )).scalars().all()
    summaries = []
    for company_id in consented_ids:
        metrics = await compute_financial_metrics(db, company_id)
        forecast = await compute_cash_forecast(db, company_id)
        summaries.append({
            "cash_runway_months": metrics["runway_months"],
            "open_receivables": (await db.scalar(
                select(ReceivableInvoice.id).where(
                    ReceivableInvoice.company_id == company_id,
                    ReceivableInvoice.status == "OPEN",
                )
            )) is not None,
            "forecast_shortfall": forecast["shortfall"],
        })
    return {
        "consented_businesses": len(summaries),
        "businesses_with_shortfall": sum(item["forecast_shortfall"] > 0 for item in summaries),
        "businesses_below_four_month_runway": sum(
            item["cash_runway_months"] is not None and item["cash_runway_months"] < 4
            for item in summaries
        ),
        "businesses_with_open_receivables": sum(item["open_receivables"] for item in summaries),
        "portfolio_signals": "Aggregated signals only; individual customer records are not shown.",
    }

@app.post("/api/v1/transactions")
async def ingest_transaction(tx: TransactionCreate, db: AsyncSession = Depends(get_db)):
    company = await get_or_create_company(db, tx.company_id)
    new_tx = Transaction(**tx.model_dump())
    db.add(new_tx)
    if tx.tx_type.upper() == "INFLOW":
        company.current_cash_balance += tx.amount
    else:
        company.current_cash_balance -= tx.amount
    await db.commit()
    await run_autonomous_alert_scan(db, tx.company_id)
    return {"status": "ingested", "new_cash_balance": float(company.current_cash_balance)}


@app.post("/api/v1/transactions/upload")
async def upload_transactions(
    request: Request,
    company_name: str = Query(min_length=1, max_length=150),
    currency: str = Query(default="INR", min_length=1, max_length=10),
    opening_cash: Decimal = Query(default=Decimal("0"), ge=0),
    db: AsyncSession = Depends(get_db),
):
    if not company_name.strip():
        raise HTTPException(status_code=400, detail="Company name cannot be blank.")
    transactions = parse_transactions_csv(await request.body())
    company = await get_or_create_company(
        db,
        "comp_001",
        name=company_name.strip(),
        currency=currency.strip().upper(),
        opening_cash=opening_cash,
    )

    db.add_all(
        Transaction(company_id=company.id, **transaction)
        for transaction in transactions
    )
    company.current_cash_balance += sum(
        (transaction["amount"] if transaction["tx_type"] == "INFLOW" else -transaction["amount"])
        for transaction in transactions
    )
    await db.commit()
    await run_autonomous_alert_scan(db, company.id)
    return {"status": "uploaded", "transactions_imported": len(transactions)}


@app.post("/api/v1/payables")
async def create_payable(payable: PayableCreate, db: AsyncSession = Depends(get_db)):
    await get_or_create_company(db, payable.company_id)
    new_payable = PayableObligation(**payable.model_dump())
    db.add(new_payable)
    await db.commit()
    await run_autonomous_alert_scan(db, payable.company_id)
    return {"status": "scheduled", "payable_id": new_payable.id}

@app.post("/api/v1/payables/{payable_id}/pay")
async def settle_payable(payable_id: int, db: AsyncSession = Depends(get_db)):
    payable = (await db.execute(select(PayableObligation).where(PayableObligation.id == payable_id))).scalar_one_or_none()
    if not payable or payable.is_paid:
        raise HTTPException(status_code=404, detail="Payable not found or already paid")
    company = (await db.execute(select(Company).where(Company.id == payable.company_id))).scalar_one()
    payable.is_paid = True
    company.current_cash_balance -= payable.amount
    db.add(Transaction(company_id=payable.company_id, tx_type="OUTFLOW", category=payable.category, vendor_or_client=payable.title, amount=payable.amount, tx_date=date.today(), description="Settled obligation"))
    await db.commit()
    await run_autonomous_alert_scan(db, payable.company_id)
    return {"status": "settled", "remaining_cash": float(company.current_cash_balance)}

@app.post("/api/v1/cfo/ask")
async def ask_digital_cfo(req: CFOQueryRequest, db: AsyncSession = Depends(get_db)):
    metrics = await compute_financial_metrics(db, req.company_id)
    if not metrics["has_data"]:
        raise HTTPException(status_code=400, detail="Upload financial transactions before requesting a CFO briefing.")
    result = cfo_agent_graph.invoke({"question": req.question, "metrics": metrics, "ledger_analysis": "", "risk_audit": "", "final_cfo_briefing": ""})
    return {"briefing": result["final_cfo_briefing"], "ledger_insight": result["ledger_analysis"], "risk_audit": result["risk_audit"]}
