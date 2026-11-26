"""
Bank-related API routes.

Prefix: ``/api/banks``
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api.schemas import BankResponse
from backend.db.database import get_db
from backend.db.models import Bank

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/banks", tags=["Banks"])


@router.get("/", response_model=List[BankResponse])
def list_banks(db: Session = Depends(get_db)) -> list[Bank]:
    """Return all active banks."""
    banks = db.query(Bank).filter(Bank.is_active == True).order_by(Bank.name).all()  # noqa: E712
    return banks


@router.get("/{bank_id}", response_model=BankResponse)
def get_bank(bank_id: int, db: Session = Depends(get_db)) -> Bank:
    """Return a single bank by ID."""
    bank = db.query(Bank).filter(Bank.id == bank_id).first()
    if bank is None:
        raise HTTPException(status_code=404, detail=f"Bank with id {bank_id} not found")
    return bank
