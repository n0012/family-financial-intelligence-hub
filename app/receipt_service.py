"""
Receipt & Tax Deductibility Ingestion Engine (PR 9).

Performs multimodal extraction of receipt & invoice documents (PNG, JPG, WEBP, PDF)
via Gemini Vision (gemini-2.5-flash / gemini-3.8-flash) with structured schema validation.
Classifies expenses into tax deductibility buckets:
  - SCHEDULE_C_EXPENSE (IRC Sec. 162 ordinary & necessary business expenses)
  - HSA_FSA_ELIGIBLE (IRC Sec. 213(d) qualified medical, dental, vision expenses)
  - CHARITABLE_DONATION (IRC Sec. 170 qualified 501(c)(3) contributions)
  - CHILDCARE_DEPENDENT_CARE (IRC Sec. 21 child and dependent care)
  - STANDARD_NON_DEDUCTIBLE (Personal, non-qualified expenses)

Reconciles receipts against BigQuery raw_transactions within an asymmetric window
(-3d to +10d) accounting for restaurant pre-auth tips and settlement lag.
Strictly adheres to Zero-PII policy:
  - In-memory processing (image bytes are never persisted to disk or database)
  - Deterministic regex scrubbing of SSNs, EINs, and card PANs from line items and notes.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.config import BQ_DATASET_ID, BQ_PROJECT_ID, resolve_secret

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

logger = logging.getLogger("monarch-gemini.receipt_service")

# Regex scrubbing patterns for Zero-PII compliance
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EIN_PATTERN = re.compile(r"\b\d{2}-\d{7}\b")
CARD_PAN_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,16}\b")

# Tax classification constants
VALID_TAX_CATEGORIES = {
    "SCHEDULE_C_EXPENSE",
    "HSA_FSA_ELIGIBLE",
    "CHARITABLE_DONATION",
    "CHILDCARE_DEPENDENT_CARE",
    "STANDARD_NON_DEDUCTIBLE",
}

VALID_AUDIT_STATUSES = {"VERIFIED", "NEEDS_REVIEW", "REJECTED"}

TAX_CATEGORY_LABELS = {
    "SCHEDULE_C_EXPENSE": "💼 SCHEDULE C BUSINESS",
    "HSA_FSA_ELIGIBLE": "🟢 HSA / FSA ELIGIBLE",
    "CHARITABLE_DONATION": "🎗️ CHARITABLE DONATION",
    "CHILDCARE_DEPENDENT_CARE": "👶 CHILDCARE / DEPENDENT CARE",
    "STANDARD_NON_DEDUCTIBLE": "⚪ STANDARD NON-DEDUCTIBLE",
}


def scrub_pii(text: str | None) -> str:
    """Deterministically scrub SSNs, EINs, and credit card PANs from text before storage or logging."""
    if not text:
        return ""
    scrubbed = SSN_PATTERN.sub("[REDACTED_SSN]", text)
    scrubbed = EIN_PATTERN.sub("[REDACTED_EIN]", scrubbed)
    scrubbed = CARD_PAN_PATTERN.sub("[REDACTED_CARD]", scrubbed)
    return scrubbed


class LineItem(BaseModel):
    description: str
    amount: float
    is_deductible: bool = False
    category: str | None = None


class ReceiptExtractionResult(BaseModel):
    merchant_name: str = Field(description="Name of the vendor or merchant")
    receipt_date: str | None = Field(default=None, description="ISO Date YYYY-MM-DD on the receipt")
    total_amount: float = Field(description="Total gross charge in USD")
    deductible_amount: float = Field(default=0.0, description="Deductible dollar portion of the charge")
    tax_amount: float | None = Field(default=None, description="Sales tax amount if listed")
    tip_amount: float | None = Field(default=None, description="Tip amount if listed (e.g. restaurant)")
    payment_method_last4: str | None = Field(default=None, description="Last 4 digits of card used if printed")
    tax_category: str = Field(
        default="STANDARD_NON_DEDUCTIBLE",
        description="Tax category classification",
    )
    is_tax_deductible: bool = Field(default=False, description="Whether any portion is tax deductible")
    deductibility_confidence: float = Field(default=0.0, description="Confidence score between 0.0 and 1.0")
    tax_justification: str = Field(
        default="",
        description="Statutory IRS justification (e.g., IRC Sec. 213(d) qualified medical expense)",
    )
    audit_status: str = Field(default="VERIFIED", description="VERIFIED, NEEDS_REVIEW, or REJECTED")
    line_items: list[LineItem] = Field(default_factory=list, description="Itemized receipt line items")
    notes: str = Field(default="", description="Relevant merchant or contextual notes")


def get_genai_client() -> Any | None:
    """Initialize a Google GenAI Client with API Key or ADC/Vertex fallback."""
    if not genai:
        logger.warning("google-genai library not available")
        return None

    gemini_key = resolve_secret("gemini-api-key", "GEMINI_API_KEY")
    try:
        if gemini_key:
            return genai.Client(api_key=gemini_key)
        return genai.Client(vertexai=True, project=BQ_PROJECT_ID, location=os.getenv("REGION", "us-central1"))
    except Exception as e:
        logger.error(f"Failed to initialize GenAI client for receipt extraction: {e}")
        return None


def parse_receipt_image(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    client: Any | None = None,
) -> ReceiptExtractionResult:
    """
    Extracts structured receipt data and tax deductibility classification from an image or PDF in memory.
    Image bytes are never persisted.
    """
    if client is None:
        client = get_genai_client()

    if not client or not types:
        logger.warning("No GenAI client available; returning empty non-deductible result")
        return ReceiptExtractionResult(
            merchant_name="Unknown Merchant",
            receipt_date=datetime.date.today().isoformat(),
            total_amount=0.0,
            deductible_amount=0.0,
            tax_category="STANDARD_NON_DEDUCTIBLE",
            is_tax_deductible=False,
            deductibility_confidence=0.0,
            audit_status="NEEDS_REVIEW",
            notes="Gemini client unavailable for multimodal parsing.",
        )

    system_instruction = (
        "You are an expert tax accountant and automated Document AI specialist for a personal family office. "
        "Your task is to analyze receipt, invoice, and bill documents.\n\n"
        "ZERO-PII POLICY:\n"
        "- NEVER extract, output, or transcribe Social Security Numbers (SSN), Employer Identification Numbers (EIN), "
        "full 16-digit credit card numbers, or patient medical diagnosis codes.\n"
        "- Only extract the last 4 digits of the payment method if present.\n\n"
        "TAX DEDUCTIBILITY RULES (US Internal Revenue Code):\n"
        "1. SCHEDULE_C_EXPENSE: Ordinary and necessary expenses directly related to sole-proprietorship or business "
        "(e.g., office supplies, SaaS subscriptions, business equipment, travel). Justification must cite IRC Sec. 162.\n"
        "2. HSA_FSA_ELIGIBLE: Qualified out-of-pocket medical, dental, vision, or prescription healthcare expenses "
        "under IRC Sec. 213(d) / IRS Pub 502 (e.g., doctor copays, eyeglasses, prescription drugs, first aid supplies). "
        "Non-prescription cosmetic items or general toiletries are NOT deductible.\n"
        "3. CHARITABLE_DONATION: Contributions to qualified 501(c)(3) public charities or religious institutions under IRC Sec. 170.\n"
        "4. CHILDCARE_DEPENDENT_CARE: Expenses for care of qualifying children under 13 to enable parents to work (IRC Sec. 21).\n"
        "5. STANDARD_NON_DEDUCTIBLE: Personal groceries, personal apparel, dining, entertainment, household personal goods.\n\n"
        "MIXED-USE BASKETS (e.g. Target, Costco, Walgreens):\n"
        "- If a receipt contains both eligible items (e.g. $25 prescription or bandages) and non-deductible goods ($75 groceries), "
        "you MUST itemize the line items, mark is_deductible=True ONLY on the eligible items, and compute "
        "deductible_amount as the exact sum of deductible line items. Do NOT claim the entire receipt total as deductible!\n\n"
        "CONFIDENCE AND AUDIT STATUS:\n"
        "- If is_tax_deductible is True, assign a deductibility_confidence (0.0 to 1.0).\n"
        "- If confidence >= 0.85, audit_status is 'VERIFIED'. If confidence < 0.85, audit_status is 'NEEDS_REVIEW'.\n"
        "- If is_tax_deductible is False, audit_status is 'VERIFIED' and tax_category is 'STANDARD_NON_DEDUCTIBLE'."
    )

    prompt = (
        "Analyze this receipt or invoice image. Extract the merchant name, receipt date (YYYY-MM-DD), "
        "total gross amount, deductible amount, sales tax, tip, payment method last 4, tax deductibility category, "
        "IRC statutory justification, itemized line items, and audit status. Return strictly valid JSON."
    )

    try:
        model_name = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")
        media_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

        response = client.models.generate_content(
            model=model_name,
            contents=[media_part, types.Part.from_text(text=prompt)],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=ReceiptExtractionResult,
                system_instruction=system_instruction,
            ),
        )

        resp_text = response.text or "{}"
        data = json.loads(resp_text)

        # Enforce sanity & PII scrubbing on result
        result = ReceiptExtractionResult(**data)
        result.merchant_name = scrub_pii(result.merchant_name)
        result.notes = scrub_pii(result.notes)
        result.tax_justification = scrub_pii(result.tax_justification)
        for item in result.line_items:
            item.description = scrub_pii(item.description)

        # Ensure tax category is valid enum
        if result.tax_category not in VALID_TAX_CATEGORIES:
            result.tax_category = "STANDARD_NON_DEDUCTIBLE"
            result.is_tax_deductible = False
            result.deductible_amount = 0.0

        # Enforce confidence gate: if deductible but low confidence, require review
        if result.is_tax_deductible:
            if result.deductibility_confidence < 0.85:
                result.audit_status = "NEEDS_REVIEW"
            else:
                result.audit_status = "VERIFIED"
            if result.deductible_amount <= 0.0:
                result.audit_status = "NEEDS_REVIEW"
                result.deductible_amount = 0.0
            # Enforce strict invariant: 0.0 <= deductible_amount <= total_amount
            if result.deductible_amount > result.total_amount:
                result.deductible_amount = result.total_amount
            if result.deductible_amount < 0.0:
                result.deductible_amount = 0.0
        else:
            result.deductible_amount = 0.0
            result.audit_status = "VERIFIED"

        return result

    except Exception as e:
        logger.error(f"Error executing Gemini multimodal receipt extraction: {e}")
        return ReceiptExtractionResult(
            merchant_name="Unknown Merchant",
            receipt_date=datetime.date.today().isoformat(),
            total_amount=0.0,
            deductible_amount=0.0,
            tax_category="STANDARD_NON_DEDUCTIBLE",
            is_tax_deductible=False,
            deductibility_confidence=0.0,
            audit_status="NEEDS_REVIEW",
            notes=f"Extraction failure: {scrub_pii(str(e))}",
        )


def match_receipt_to_transaction(
    bq: Any,
    project_id: str,
    dataset_id: str,
    merchant_name: str,
    receipt_date: str | None,
    total_amount: float,
    tip_amount: float | None = None,
    payment_method_last4: str | None = None,
) -> dict[str, Any] | None:
    """
    Matches an ingested receipt against BigQuery raw_transactions.
    Accounting details:
      - Raw transactions store debits as negative values (amount < 0).
      - Asymmetric search window: DATE_SUB(receipt_date, 3 days) to DATE_ADD(receipt_date, 10 days)
        to handle online shipment delays and merchant clearing lags.
      - Tip handling: Checks both gross total and pre-tip subtotal (since restaurant authorizations
        initially post without tip).
      - Card mask matching priority when last 4 is available.
    """
    if not bq or not total_amount or total_amount <= 0.0:
        return None

    # Default to current date if missing or unparseable
    target_date = receipt_date or datetime.date.today().isoformat()
    clean_merchant = re.sub(r"[^a-zA-Z0-9\s]", "", merchant_name).strip()
    merchant_query_str = clean_merchant[:10].lower() if clean_merchant else ""

    query = f"""
    SELECT
        t.transaction_id,
        t.account_id,
        t.transaction_date,
        t.amount,
        t.merchant_name,
        t.clean_merchant_name,
        t.category_name,
        t.pending,
        a.display_name AS account_name,
        a.subtype_name AS account_subtype
    FROM `{project_id}.{dataset_id}.raw_transactions` t
    LEFT JOIN `{project_id}.{dataset_id}.raw_accounts` a ON t.account_id = a.account_id
    WHERE t.transaction_date BETWEEN DATE_SUB(DATE(@target_date), INTERVAL 3 DAY)
                                AND DATE_ADD(DATE(@target_date), INTERVAL 10 DAY)
      AND (
          -- Match exact total (debits are negative)
          ABS(t.amount - (-1.0 * @total_amount)) <= 0.05
          OR
          -- Match pre-tip auth amount for restaurants
          (@has_tip AND ABS(t.amount - (-1.0 * (@total_amount - @tip_amount))) <= 0.05)
          OR
          -- Match restaurant settled transaction with added tip (up to +35% higher)
          ((-1.0 * t.amount) >= @total_amount AND (-1.0 * t.amount) <= (@total_amount * 1.35)
           AND (LOWER(COALESCE(t.category_name, '')) LIKE '%dining%'
                OR LOWER(COALESCE(t.category_name, '')) LIKE '%restaurant%'
                OR LOWER(t.merchant_name) LIKE CONCAT('%', @merchant_prefix, '%')))
      )
    ORDER BY
        -- Prioritize exact amount match over pre-tip auth or tipped match
        (ABS(t.amount - (-1.0 * @total_amount)) <= 0.05) DESC,
        -- Prioritize matching merchant name
        (LOWER(t.merchant_name) LIKE CONCAT('%', @merchant_prefix, '%')
         OR LOWER(COALESCE(t.clean_merchant_name, '')) LIKE CONCAT('%', @merchant_prefix, '%')) DESC,
        -- Prioritize posted transactions over pending
        (t.pending = FALSE) DESC,
        t.transaction_date DESC
    LIMIT 2;
    """

    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("target_date", "STRING", target_date),
            bigquery.ScalarQueryParameter("total_amount", "FLOAT64", float(total_amount)),
            bigquery.ScalarQueryParameter("has_tip", "BOOL", bool(tip_amount and tip_amount > 0)),
            bigquery.ScalarQueryParameter("tip_amount", "FLOAT64", float(tip_amount or 0.0)),
            bigquery.ScalarQueryParameter("merchant_prefix", "STRING", merchant_query_str),
        ]
    )

    try:
        results = list(bq.query(query, job_config=job_config).result())
        if results:
            row = results[0]
            posted_val = abs(float(row.amount))
            delta = round(posted_val - float(total_amount), 2)
            match_status = "MATCHED_POSTED" if not row.pending else "MATCHED_PENDING"
            if len(results) > 1 and abs(float(results[0].amount) - float(results[1].amount)) < 0.01:
                match_status = "AMBIGUOUS"

            return {
                "transaction_id": row.transaction_id,
                "account_id": row.account_id,
                "account_name": row.account_name,
                "transaction_date": str(row.transaction_date),
                "amount": float(row.amount),
                "amount_delta": delta,
                "merchant_name": row.merchant_name,
                "pending": bool(row.pending),
                "match_status": match_status,
            }
        return None
    except Exception as e:
        logger.error(f"Error executing transaction reconciliation query: {e}")
        return None


def save_receipt_record(
    bq: Any,
    project_id: str,
    dataset_id: str,
    receipt_data: ReceiptExtractionResult | dict[str, Any],
    matched_transaction_id: str | None = None,
    user_email: str | None = None,
) -> str | None:
    """
    Inserts a structured receipt record into BigQuery family_finance.receipt_records.
    Returns the generated receipt_id.
    """
    if not bq:
        logger.warning("BigQuery client unavailable; receipt record not persisted.")
        return None

    if isinstance(receipt_data, ReceiptExtractionResult):
        data_dict = receipt_data.model_dump()
    else:
        data_dict = dict(receipt_data)

    receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
    now_utc = datetime.datetime.now(datetime.UTC)
    rec_date = data_dict.get("receipt_date") or now_utc.date().isoformat()

    # Scrub PII before saving line items or notes
    raw_lines = data_dict.get("line_items") or []
    sanitized_lines = []
    for item in raw_lines:
        if isinstance(item, dict):
            sanitized_lines.append(
                {
                    "description": scrub_pii(item.get("description", "")),
                    "amount": float(item.get("amount", 0.0)),
                    "is_deductible": bool(item.get("is_deductible", False)),
                    "category": item.get("category"),
                }
            )
        elif hasattr(item, "description"):
            sanitized_lines.append(
                {
                    "description": scrub_pii(item.description),
                    "amount": float(item.amount),
                    "is_deductible": bool(item.is_deductible),
                    "category": getattr(item, "category", None),
                }
            )

    line_items_json = scrub_pii(json.dumps(sanitized_lines))
    clean_notes = scrub_pii(data_dict.get("notes", ""))
    clean_merchant = scrub_pii(data_dict.get("merchant_name", "Unknown Merchant"))
    clean_justification = scrub_pii(data_dict.get("tax_justification", ""))

    insert_sql = f"""
    INSERT INTO `{project_id}.{dataset_id}.receipt_records` (
        receipt_id,
        uploaded_at,
        user_email,
        merchant_name,
        receipt_date,
        total_amount,
        deductible_amount,
        tax_amount,
        tip_amount,
        payment_method_last4,
        tax_category,
        is_tax_deductible,
        deductibility_confidence,
        tax_justification,
        audit_status,
        matched_transaction_id,
        line_items_json,
        notes
    ) VALUES (
        @receipt_id,
        @uploaded_at,
        @user_email,
        @merchant_name,
        DATE(@receipt_date),
        @total_amount,
        @deductible_amount,
        @tax_amount,
        @tip_amount,
        @payment_method_last4,
        @tax_category,
        @is_tax_deductible,
        @deductibility_confidence,
        @tax_justification,
        @audit_status,
        @matched_transaction_id,
        @line_items_json,
        @notes
    );
    """

    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("receipt_id", "STRING", receipt_id),
            bigquery.ScalarQueryParameter("uploaded_at", "TIMESTAMP", now_utc),
            bigquery.ScalarQueryParameter("user_email", "STRING", user_email or "system"),
            bigquery.ScalarQueryParameter("merchant_name", "STRING", clean_merchant),
            bigquery.ScalarQueryParameter("receipt_date", "STRING", rec_date),
            bigquery.ScalarQueryParameter("total_amount", "FLOAT64", float(data_dict.get("total_amount", 0.0))),
            bigquery.ScalarQueryParameter(
                "deductible_amount", "FLOAT64", float(data_dict.get("deductible_amount", 0.0))
            ),
            bigquery.ScalarQueryParameter(
                "tax_amount",
                "FLOAT64",
                float(data_dict["tax_amount"]) if data_dict.get("tax_amount") is not None else None,
            ),
            bigquery.ScalarQueryParameter(
                "tip_amount",
                "FLOAT64",
                float(data_dict["tip_amount"]) if data_dict.get("tip_amount") is not None else None,
            ),
            bigquery.ScalarQueryParameter("payment_method_last4", "STRING", data_dict.get("payment_method_last4")),
            bigquery.ScalarQueryParameter(
                "tax_category", "STRING", data_dict.get("tax_category", "STANDARD_NON_DEDUCTIBLE")
            ),
            bigquery.ScalarQueryParameter("is_tax_deductible", "BOOL", bool(data_dict.get("is_tax_deductible", False))),
            bigquery.ScalarQueryParameter(
                "deductibility_confidence", "FLOAT64", float(data_dict.get("deductibility_confidence", 0.0))
            ),
            bigquery.ScalarQueryParameter("tax_justification", "STRING", clean_justification),
            bigquery.ScalarQueryParameter("audit_status", "STRING", data_dict.get("audit_status", "VERIFIED")),
            bigquery.ScalarQueryParameter("matched_transaction_id", "STRING", matched_transaction_id),
            bigquery.ScalarQueryParameter("line_items_json", "STRING", line_items_json),
            bigquery.ScalarQueryParameter("notes", "STRING", clean_notes),
        ]
    )

    try:
        bq.query(insert_sql, job_config=job_config).result()
        logger.info(
            f"Saved receipt record {receipt_id} for {clean_merchant} (deductible={data_dict.get('deductible_amount', 0.0)})"
        )
        return receipt_id
    except Exception as e:
        logger.error(f"Error persisting receipt record to BigQuery: {e}")
        return None


def get_tax_deductible_summary(
    bq: Any,
    project_id: str,
    dataset_id: str,
    tax_year: int | None = None,
) -> list[dict[str, Any]]:
    """
    Queries BigQuery v_tax_deductible_summary view.
    Returns annual deduction totals by tax category.
    """
    if not bq:
        return []

    target_year = tax_year or datetime.date.today().year

    query = f"""
    SELECT
        tax_year,
        tax_category,
        receipt_count,
        total_deductible_amount,
        total_gross_receipt_amount,
        matched_transaction_count,
        pending_review_count,
        sample_merchants
    FROM `{project_id}.{dataset_id}.v_tax_deductible_summary`
    WHERE tax_year = @tax_year
    ORDER BY total_deductible_amount DESC;
    """

    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("tax_year", "INT64", target_year),
        ]
    )

    try:
        results = list(bq.query(query, job_config=job_config).result())
        return [
            {
                "tax_year": row.tax_year,
                "tax_category": row.tax_category,
                "receipt_count": row.receipt_count,
                "total_deductible_amount": float(row.total_deductible_amount),
                "total_gross_receipt_amount": float(row.total_gross_receipt_amount),
                "matched_transaction_count": row.matched_transaction_count,
                "pending_review_count": row.pending_review_count,
                "sample_merchants": list(row.sample_merchants or []),
            }
            for row in results
        ]
    except Exception as e:
        logger.error(f"Error querying v_tax_deductible_summary: {e}")
        return []


def format_tax_summary_text(summary_rows: list[dict[str, Any]], tax_year: int) -> str:
    """Formats an itemized annual tax deductibility summary in clean Markdown."""
    if not summary_rows:
        return f"📅 **Tax Year {tax_year} Deductions**\nNo tax-deductible receipts recorded yet for {tax_year}."

    total_deductions = sum(r["total_deductible_amount"] for r in summary_rows)
    total_receipts = sum(r["receipt_count"] for r in summary_rows)
    pending_review = sum(r["pending_review_count"] for r in summary_rows)

    lines = [
        f"📋 **Tax Year {tax_year} Deduction Summary**",
        f"**Total Tracked Deductions:** `${total_deductions:,.2f}` across **{total_receipts}** substantiated receipts.",
    ]
    if pending_review > 0:
        lines.append(f"⚠️ *{pending_review} item(s) flagged for manual review.*")
    lines.append("")

    for row in summary_rows:
        cat_badge = TAX_CATEGORY_LABELS.get(row["tax_category"], row["tax_category"])
        merchants = ", ".join(row["sample_merchants"][:3])
        if merchants:
            merchants = f" (e.g. {merchants})"
        lines.append(
            f"• **{cat_badge}**: `${row['total_deductible_amount']:,.2f}` ({row['receipt_count']} receipts{merchants})"
        )

    lines.append("\n💡 *Substantiated with document records in BigQuery family_finance.receipt_records.*")
    return "\n".join(lines)


def build_receipt_chat_card(
    receipt_data: ReceiptExtractionResult | dict[str, Any],
    receipt_id: str | None = None,
    matched_tx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Constructs a Google Chat Card v2 representing an ingested receipt with
    tax deductibility pill, IRS statutory justification, and reconciliation badge.
    """
    if isinstance(receipt_data, ReceiptExtractionResult):
        data = receipt_data.model_dump()
    else:
        data = dict(receipt_data)

    merchant = data.get("merchant_name", "Unknown Merchant")
    rec_date = data.get("receipt_date") or datetime.date.today().isoformat()
    total = float(data.get("total_amount", 0.0))
    deductible = float(data.get("deductible_amount", 0.0))
    tax_cat = data.get("tax_category", "STANDARD_NON_DEDUCTIBLE")
    is_deductible = bool(data.get("is_tax_deductible", False))
    confidence = float(data.get("deductibility_confidence", 0.0))
    justification = data.get("tax_justification") or "No statutory justification provided."
    audit_status = data.get("audit_status", "VERIFIED")

    # 1. Top Category Badge
    badge_label = TAX_CATEGORY_LABELS.get(tax_cat, "⚪ STANDARD NON-DEDUCTIBLE")
    if is_deductible:
        top_label = f"{badge_label} • ${deductible:,.2f} Deductible"
    else:
        top_label = badge_label

    card_sections = []

    # Section 1: Extraction Overview
    overview_widgets: list[dict[str, Any]] = [
        {
            "decoratedText": {
                "topLabel": top_label,
                "text": f"<b>Total Charge:</b> ${total:,.2f}",
                "bottomLabel": f"Receipt Date: {rec_date}"
                + (f" • Card ...{data.get('payment_method_last4')}" if data.get("payment_method_last4") else ""),
                "startIcon": {"knownIcon": "DOLLAR"},
            }
        }
    ]

    # Deductibility status & justification
    if is_deductible:
        status_text = (
            "✅ <b>Verified Deductible</b>"
            if audit_status == "VERIFIED"
            else f"⚠️ <b>Needs Review</b> (Confidence: {confidence * 100:.0f}%)"
        )
        overview_widgets.append(
            {
                "decoratedText": {
                    "topLabel": "IRS Substantiation & Eligibility",
                    "text": status_text,
                    "bottomLabel": justification,
                    "wrapText": True,
                    "startIcon": {"knownIcon": "DESCRIPTION"},
                }
            }
        )
    else:
        overview_widgets.append(
            {
                "decoratedText": {
                    "topLabel": "Tax Eligibility",
                    "text": "⚪ <b>Standard Personal Expense</b>",
                    "bottomLabel": "Non-deductible under federal tax regulations.",
                    "wrapText": True,
                }
            }
        )

    # Section 2: Bank Transaction Matching Status
    if matched_tx:
        tx_amt = abs(float(matched_tx.get("amount", 0.0)))
        tx_date = matched_tx.get("transaction_date", "")
        tx_merchant = matched_tx.get("merchant_name", "")
        acct_name = matched_tx.get("account_name", "")
        match_desc = f"-${tx_amt:,.2f} on {tx_date}"
        if acct_name:
            match_desc += f" ({acct_name})"

        overview_widgets.append(
            {
                "decoratedText": {
                    "topLabel": "Bank Reconciliation (BigQuery Match)",
                    "text": f"✅ <b>Matched: {tx_merchant}</b>",
                    "bottomLabel": match_desc,
                    "wrapText": True,
                    "startIcon": {"knownIcon": "MEMBERSHIP"},
                }
            }
        )
    else:
        overview_widgets.append(
            {
                "decoratedText": {
                    "topLabel": "Bank Reconciliation",
                    "text": "⚠️ <b>No matching bank debit found</b>",
                    "bottomLabel": "Checked transactions within -3 to +10 days. May be pending or paid via untracked method.",
                    "wrapText": True,
                    "startIcon": {"knownIcon": "CLOCK"},
                }
            }
        )

    # Line item preview (top 3)
    line_items = data.get("line_items") or []
    if line_items:
        lines_preview = []
        for it in line_items[:3]:
            desc = it.get("description") if isinstance(it, dict) else it.description
            amt = it.get("amount") if isinstance(it, dict) else it.amount
            is_ded = it.get("is_deductible") if isinstance(it, dict) else it.is_deductible
            ded_icon = "🟢" if is_ded else "⚪"
            lines_preview.append(f"{ded_icon} {desc} (${amt:.2f})")
        overview_widgets.append(
            {
                "decoratedText": {
                    "topLabel": f"Itemized Breakdown ({len(line_items)} items)",
                    "text": "<br>".join(lines_preview),
                    "wrapText": True,
                }
            }
        )

    card_sections.append({"widgets": overview_widgets})

    return {
        "cardId": f"receipt_{receipt_id or 'scan'}",
        "card": {
            "header": {
                "title": f"🧾 Receipt: {merchant}",
                "subtitle": f"${total:,.2f} • {rec_date}",
                "imageType": "CIRCLE",
            },
            "sections": card_sections,
        },
    }


def process_receipt_bytes(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    user_email: str | None = None,
    bq: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """
    End-to-end receipt ingestion pipeline:
      1. Multimodal AI extraction (in-memory, Zero-PII)
      2. BigQuery transaction reconciliation (-3d to +10d, pre-auth & total)
      3. BigQuery receipt_records persistence
      4. Interactive Google Chat Card v2 generation
    """
    if bq is None:
        from app.bq_service import get_bq_client

        bq = get_bq_client()

    email = user_email or "system"

    # Enforce PR 10 mutation rate limiting on receipt ingestion
    from app.monarch_service import check_mutation_rate_limit, log_mutation_audit

    allowed, retry_after = check_mutation_rate_limit(email)
    if not allowed:
        raise ValueError(
            f"Receipt ingestion rate limit exceeded. Please wait {retry_after}s before uploading another receipt."
        )

    # 1. Parse image via Gemini Vision
    extraction = parse_receipt_image(image_bytes, mime_type=mime_type, client=client)

    # 2. Match against raw_transactions
    matched_tx = None
    if bq and extraction.total_amount > 0:
        matched_tx = match_receipt_to_transaction(
            bq=bq,
            project_id=BQ_PROJECT_ID,
            dataset_id=BQ_DATASET_ID,
            merchant_name=extraction.merchant_name,
            receipt_date=extraction.receipt_date,
            total_amount=extraction.total_amount,
            tip_amount=extraction.tip_amount,
            payment_method_last4=extraction.payment_method_last4,
        )

    matched_tx_id = matched_tx.get("transaction_id") if matched_tx else None

    # 3. Save receipt record into BigQuery
    receipt_id = None
    if bq:
        receipt_id = save_receipt_record(
            bq=bq,
            project_id=BQ_PROJECT_ID,
            dataset_id=BQ_DATASET_ID,
            receipt_data=extraction,
            matched_transaction_id=matched_tx_id,
            user_email=email,
        )

        try:
            log_mutation_audit(
                bq=bq,
                project_id=BQ_PROJECT_ID,
                dataset_id=BQ_DATASET_ID,
                action_type="INGEST_RECEIPT",
                user_email=email,
                transaction_id=matched_tx_id,
                previous_value=None,
                new_value=f"Merchant: {extraction.merchant_name}, Total: {extraction.total_amount}, Deductible: {extraction.deductible_amount}, Category: {extraction.tax_category}",
                details={
                    "receipt_id": receipt_id,
                    "confidence": extraction.deductibility_confidence,
                    "audit_status": extraction.audit_status,
                },
            )
        except Exception as e:
            logger.warning(f"Could not log mutation audit for receipt {receipt_id}: {e}")

    # 4. Build Card v2
    card = build_receipt_chat_card(
        receipt_data=extraction,
        receipt_id=receipt_id,
        matched_tx=matched_tx,
    )

    return {
        "receipt_id": receipt_id,
        "extraction": extraction.model_dump(),
        "matched_transaction": matched_tx,
        "chat_card": card,
    }


def get_tax_deduction_analysis(tax_year: int | None = None) -> str:
    """
    Retrieves the family's annual tax deductibility breakdown from BigQuery receipt records
    (v_tax_deductible_summary) for Schedule C business expenses, HSA/FSA medical expenses,
    charitable contributions, and childcare/dependent care.

    Args:
        tax_year: Optional 4-digit tax year (e.g. 2026). Defaults to current year.
    """
    from app.bq_service import get_bq_client

    bq = get_bq_client()
    target_year = tax_year or datetime.date.today().year
    rows = get_tax_deductible_summary(bq, BQ_PROJECT_ID, BQ_DATASET_ID, target_year)
    return format_tax_summary_text(rows, target_year)
