from datetime import date, timedelta
from typing import Dict, Any
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models import Company, Transaction, PayableObligation, CFOAlert, ReceivableInvoice

async def compute_financial_metrics(db: AsyncSession, company_id: str) -> Dict[str, Any]:
    company_res = await db.execute(select(Company).where(Company.id == company_id))
    company = company_res.scalar_one_or_none()
    if not company:
        return {
            "has_data": False,
            "company_name": "No company data uploaded",
            "currency": "INR",
            "current_cash": 0.0,
            "current_month_expense": 0.0,
            "previous_month_expense": 0.0,
            "mom_expense_change_pct": 0.0,
            "monthly_burn_rate": 0.0,
            "runway_months": None,
            "upcoming_payables_total": 0.0,
            "upcoming_payables": [],
            "category_breakdown": {},
            "monthly_trend": [],
            "anomalies": [],
            "dashboard_insights": [],
        }

    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_of_prev_month.replace(day=1)

    tx_res = await db.execute(select(Transaction).where(Transaction.company_id == company_id))
    transactions = tx_res.scalars().all()

    curr_month_outflow = sum(float(t.amount) for t in transactions if t.tx_type == "OUTFLOW" and t.tx_date >= first_of_this_month)
    prev_month_outflow = sum(float(t.amount) for t in transactions if t.tx_type == "OUTFLOW" and first_of_prev_month <= t.tx_date <= last_of_prev_month)
    curr_month_inflow = sum(float(t.amount) for t in transactions if t.tx_type == "INFLOW" and t.tx_date >= first_of_this_month)

    mom_change_pct = round(((curr_month_outflow - prev_month_outflow) / prev_month_outflow) * 100, 2) if prev_month_outflow > 0 else 0.0

    net_burn = max(curr_month_outflow - curr_month_inflow, 0.0)
    current_cash = float(company.current_cash_balance)
    effective_burn = net_burn if net_burn > 0 else curr_month_outflow
    runway_months = round(current_cash / effective_burn, 1) if effective_burn > 0 else None

    category_totals: Dict[str, float] = {}
    for t in transactions:
        if t.tx_type == "OUTFLOW" and t.tx_date >= first_of_this_month:
            category_totals[t.category] = category_totals.get(t.category, 0.0) + float(t.amount)

    monthly_trend = []
    for i in range(5, -1, -1):
        target_date = (first_of_this_month - timedelta(days=i * 28)).replace(day=1)
        next_month = (target_date + timedelta(days=32)).replace(day=1)
        m_in = sum(float(t.amount) for t in transactions if t.tx_type == "INFLOW" and target_date <= t.tx_date < next_month)
        m_out = sum(float(t.amount) for t in transactions if t.tx_type == "OUTFLOW" and target_date <= t.tx_date < next_month)
        monthly_trend.append({"month": target_date.strftime("%b %Y"), "inflow": m_in, "outflow": m_out})

    outflow_amounts = [float(t.amount) for t in transactions if t.tx_type == "OUTFLOW"]
    anomalies = []
    if len(outflow_amounts) >= 4:
        mean_val = float(np.mean(outflow_amounts))
        std_val = float(np.std(outflow_amounts))
        threshold = mean_val + (1.5 * std_val)
        for t in transactions:
            if t.tx_type == "OUTFLOW" and t.tx_date >= first_of_this_month and float(t.amount) > threshold:
                anomalies.append({
                    "id": t.id, "vendor": t.vendor_or_client, "category": t.category,
                    "amount": float(t.amount), "baseline_avg": round(mean_val, 2),
                    "reason": f"Exceeds normal expense baseline by {round(((float(t.amount)-mean_val)/mean_val)*100, 1)}%"
                })

    payables_res = await db.execute(
        select(PayableObligation).where(PayableObligation.company_id == company_id, PayableObligation.is_paid == False).order_by(PayableObligation.due_date.asc())
    )
    payables = payables_res.scalars().all()
    upcoming_payables = []
    for p in payables:
        days_left = (p.due_date - today).days
        upcoming_payables.append({
            "id": p.id, "title": p.title, "category": p.category, "amount": float(p.amount),
            "due_date": p.due_date.isoformat(), "days_remaining": days_left,
            "status": "OVERDUE" if days_left < 0 else ("DUE_SOON" if days_left <= 7 else "SCHEDULED")
        })

    dashboard_insights = [
        f"Current cash balance is {company.currency} {current_cash:,.2f}."
    ]
    if curr_month_outflow > 0:
        if curr_month_inflow >= curr_month_outflow:
            dashboard_insights.append(
                f"This month's inflows exceed expenses by {company.currency} {curr_month_inflow - curr_month_outflow:,.2f}."
            )
        else:
            dashboard_insights.append(
                f"This month's net cash burn is {company.currency} {curr_month_outflow - curr_month_inflow:,.2f}."
            )
    else:
        dashboard_insights.append("No expenses have been recorded for the current month.")

    if prev_month_outflow > 0:
        if mom_change_pct == 0:
            dashboard_insights.append("Expenses are unchanged compared with last month.")
        else:
            direction = "up" if mom_change_pct > 0 else "down"
            dashboard_insights.append(
                f"Expenses are {abs(mom_change_pct):.1f}% {direction} compared with last month."
            )
    elif curr_month_outflow > 0:
        dashboard_insights.append("There is not enough previous-month data to compare expenses.")

    if runway_months is None:
        dashboard_insights.append("Cash runway is unavailable until monthly expenses are recorded.")
    else:
        dashboard_insights.append(f"Estimated cash runway is {runway_months} months at the current burn rate.")

    if upcoming_payables:
        dashboard_insights.append(
            f"{len(upcoming_payables)} unpaid payable(s) total {company.currency} "
            f"{sum(p['amount'] for p in upcoming_payables):,.2f}."
        )
    if anomalies:
        dashboard_insights.append(f"{len(anomalies)} unusually large expense(s) were detected.")

    return {
        "has_data": bool(transactions or payables), "company_name": company.name, "currency": company.currency, "current_cash": current_cash,
        "current_month_expense": curr_month_outflow, "previous_month_expense": prev_month_outflow,
        "mom_expense_change_pct": mom_change_pct, "monthly_burn_rate": curr_month_outflow,
        "runway_months": runway_months, "upcoming_payables_total": sum(p["amount"] for p in upcoming_payables),
        "upcoming_payables": upcoming_payables, "category_breakdown": category_totals,
        "monthly_trend": monthly_trend, "anomalies": anomalies,
        "dashboard_insights": dashboard_insights,
    }

async def compute_cash_forecast(db: AsyncSession, company_id: str, days: int = 30) -> Dict[str, Any]:
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if company is None:
        return {
            "has_data": False,
            "currency": "INR",
            "current_cash": 0.0,
            "days": days,
            "daily_operating_outflow": 0.0,
            "expected_inflows": 0.0,
            "scheduled_payables": 0.0,
            "minimum_balance": 0.0,
            "shortfall": 0.0,
            "points": [],
            "assumptions": [],
        }

    today = date.today()
    end_date = today + timedelta(days=days)
    history_start = today - timedelta(days=90)
    transactions = (await db.execute(
        select(Transaction).where(
            Transaction.company_id == company_id,
            Transaction.tx_type == "OUTFLOW",
            Transaction.tx_date >= history_start,
            Transaction.tx_date < today,
        )
    )).scalars().all()
    daily_operating_outflow = sum(float(tx.amount) for tx in transactions) / 90
    confidence = "LOW" if len(transactions) < 10 else "MODERATE"
    confidence_reason = (
        f"Based on {len(transactions)} recorded outflow(s) in the prior 90 days; "
        "no live bank feed is connected."
    )

    invoices = (await db.execute(
        select(ReceivableInvoice).where(
            ReceivableInvoice.company_id == company_id,
            ReceivableInvoice.status == "OPEN",
        )
    )).scalars().all()
    payables = (await db.execute(
        select(PayableObligation).where(
            PayableObligation.company_id == company_id,
            PayableObligation.is_paid == False,
            PayableObligation.due_date <= end_date,
        )
    )).scalars().all()

    inflows_by_date: Dict[date, float] = {}
    for invoice in invoices:
        collection_date = invoice.expected_collection_date or invoice.due_date
        collection_date = max(today, collection_date)
        if collection_date <= end_date:
            inflows_by_date[collection_date] = inflows_by_date.get(collection_date, 0.0) + float(invoice.amount)

    payables_by_date: Dict[date, float] = {}
    for payable in payables:
        due_date = max(today, payable.due_date)
        payables_by_date[due_date] = payables_by_date.get(due_date, 0.0) + float(payable.amount)

    cash = float(company.current_cash_balance)
    points = [{"date": today.isoformat(), "balance": round(cash, 2)}]
    minimum_balance = cash
    for offset in range(1, days + 1):
        forecast_date = today + timedelta(days=offset)
        cash += inflows_by_date.get(forecast_date, 0.0)
        cash -= payables_by_date.get(forecast_date, 0.0)
        cash -= daily_operating_outflow
        minimum_balance = min(minimum_balance, cash)
        points.append({"date": forecast_date.isoformat(), "balance": round(cash, 2)})

    return {
        "has_data": True,
        "currency": company.currency,
        "current_cash": float(company.current_cash_balance),
        "days": days,
        "daily_operating_outflow": round(daily_operating_outflow, 2),
        "expected_inflows": round(sum(inflows_by_date.values()), 2),
        "scheduled_payables": round(sum(payables_by_date.values()), 2),
        "minimum_balance": round(minimum_balance, 2),
        "shortfall": round(max(0.0, -minimum_balance), 2),
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "points": points,
        "assumptions": [
            "Open invoices are treated as expected inflows on their expected collection date, or due date if no date is set.",
            "Unpaid obligations are deducted on their due date; overdue items are shown on day one.",
            f"Daily operating outflow uses recorded expenses from the 90 days before {today.isoformat()}, spread evenly across the forecast.",
            "This is an illustrative planning estimate, not a guarantee or credit decision.",
        ],
    }

async def run_autonomous_alert_scan(db: AsyncSession, company_id: str):
    metrics = await compute_financial_metrics(db, company_id)
    desired_alerts = {}

    for p in metrics["upcoming_payables"]:
        if p["days_remaining"] <= 7:
            alert_title = f"Due Alert: {p['title']} ({p['category']}) [#{p['id']}]"
            due_text = "OVERDUE" if p["days_remaining"] < 0 else (
                "due today" if p["days_remaining"] == 0 else f"due in {p['days_remaining']} days"
            )
            desired_alerts[("DUE_DATE", alert_title)] = {
                "severity": "CRITICAL" if p["days_remaining"] <= 2 else "WARNING",
                "message": f"{p['title']} for {metrics['currency']} {p['amount']:,.2f} is {due_text}.",
            }

    for anom in metrics["anomalies"]:
        alert_title = f"High-Risk Expense Spike: {anom['vendor']} [transaction #{anom['id']}]"
        desired_alerts[("EXPENSE_SPIKE", alert_title)] = {
            "severity": "CRITICAL",
            "message": f"Outflow of {metrics['currency']} {anom['amount']:,.2f} in {anom['category']} ({anom['reason']}).",
        }

    if metrics["runway_months"] is not None and metrics["runway_months"] < 4.0:
        alert_title = "Critical Cash Runway Warning"
        desired_alerts[("RUNWAY_RISK", alert_title)] = {
            "severity": "CRITICAL",
            "message": f"Current cash reserves cover only {metrics['runway_months']} months at current burn rate.",
        }

    active_alerts = await db.execute(
        select(CFOAlert).where(
            CFOAlert.company_id == company_id,
            CFOAlert.is_resolved == False,
            CFOAlert.alert_type.in_(("DUE_DATE", "EXPENSE_SPIKE", "RUNWAY_RISK")),
        )
    )
    existing_by_key = {}
    for alert in active_alerts.scalars().all():
        key = (alert.alert_type, alert.title)
        if key not in desired_alerts or key in existing_by_key:
            alert.is_resolved = True
        else:
            existing_by_key[key] = alert

    for key, details in desired_alerts.items():
        alert = existing_by_key.get(key)
        if alert is None:
            db.add(CFOAlert(
                company_id=company_id,
                alert_type=key[0],
                title=key[1],
                severity=details["severity"],
                message=details["message"],
            ))
        else:
            alert.severity = details["severity"]
            alert.message = details["message"]

    await db.commit()
