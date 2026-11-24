from datetime import datetime
from sqlalchemy import Column, String, Numeric, Date, DateTime, Boolean, Integer, ForeignKey, Text
from database import Base

class Company(Base):
    __tablename__ = "companies"
    id = Column(String(50), primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    current_cash_balance = Column(Numeric(15, 2), default=0.00)
    currency = Column(String(10), default="INR")
    created_at = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String(50), ForeignKey("companies.id"), index=True, nullable=False)
    tx_type = Column(String(20), nullable=False)
    category = Column(String(80), nullable=False)
    vendor_or_client = Column(String(150), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    tx_date = Column(Date, nullable=False, index=True)
    description = Column(Text, nullable=True)

class PayableObligation(Base):
    __tablename__ = "payables"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String(50), ForeignKey("companies.id"), index=True, nullable=False)
    title = Column(String(150), nullable=False)
    category = Column(String(80), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    due_date = Column(Date, nullable=False, index=True)
    is_paid = Column(Boolean, default=False)
    priority = Column(String(20), default="HIGH")

class CFOAlert(Base):
    __tablename__ = "cfo_alerts"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String(50), ForeignKey("companies.id"), index=True, nullable=False)
    alert_type = Column(String(50), nullable=False)
    severity = Column(String(20), nullable=False)
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    is_resolved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class ReceivableInvoice(Base):
    __tablename__ = "receivable_invoices"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String(50), ForeignKey("companies.id"), index=True, nullable=False)
    invoice_number = Column(String(80), nullable=False)
    customer = Column(String(150), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    issue_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False, index=True)
    expected_collection_date = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, default="OPEN")
    created_at = Column(DateTime, default=datetime.utcnow)

class CompanyConsent(Base):
    __tablename__ = "company_consents"
    company_id = Column(String(50), ForeignKey("companies.id"), primary_key=True)
    share_portfolio_signals = Column(Boolean, nullable=False, default=False)
    allow_referral_sharing = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ConsentEvent(Base):
    __tablename__ = "consent_events"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String(50), ForeignKey("companies.id"), index=True, nullable=False)
    purpose = Column(String(80), nullable=False)
    enabled = Column(Boolean, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class BankReferral(Base):
    __tablename__ = "bank_referrals"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String(50), ForeignKey("companies.id"), index=True, nullable=False)
    estimated_need = Column(Numeric(15, 2), nullable=False)
    reason = Column(String(250), nullable=False)
    status = Column(String(20), nullable=False, default="CUSTOMER_APPROVED")
    created_at = Column(DateTime, default=datetime.utcnow)
