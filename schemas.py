from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field

class TransactionCreate(BaseModel):
    company_id: str = "comp_001"
    tx_type: str
    category: str
    vendor_or_client: str
    amount: Decimal
    tx_date: date
    description: Optional[str] = None

class PayableCreate(BaseModel):
    company_id: str = "comp_001"
    title: str
    category: str
    amount: Decimal
    due_date: date
    priority: str = "HIGH"

class CFOQueryRequest(BaseModel):
    company_id: str = "comp_001"
    question: Optional[str] = "Provide a complete executive CFO briefing on our current cash position, expense trends, and upcoming risks."

class ReceivableCreate(BaseModel):
    company_id: str = "comp_001"
    invoice_number: str = Field(min_length=1, max_length=80)
    customer: str = Field(min_length=1, max_length=150)
    amount: Decimal = Field(gt=0, max_digits=15, decimal_places=2)
    issue_date: date
    due_date: date
    expected_collection_date: Optional[date] = None

class ConsentUpdate(BaseModel):
    share_portfolio_signals: bool
    allow_referral_sharing: bool

class ReferralCreate(BaseModel):
    estimated_need: Decimal = Field(gt=0, max_digits=15, decimal_places=2)
    reason: str = Field(min_length=1, max_length=250)
