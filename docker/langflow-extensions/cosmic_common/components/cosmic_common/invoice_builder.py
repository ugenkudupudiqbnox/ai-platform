"""Invoice builder component (constitution §8).

Generic, reusable builder that assembles a contract-conformant ``InvoiceData``
from customer + line-item inputs, computing totals with 2-decimal-precision
amounts (the project-wide rule). Scaffold only — pure Decimal math at build
phase; never raises.
"""

from lfx.custom import Component
from lfx.io import MessageTextInput, MultilineInput, Output
from lfx.schema import Message


class InvoiceBuilderComponent(Component):
    name = "InvoiceBuilderComponent"
    display_name = "Invoice Builder"
    description = (
        "Assemble an InvoiceData from a customer + line items and return it. "
        "Call this when an AR run has produced matched/adjusted rows that must "
        "be posted to Zoho as an invoice."
    )
    icon = "ReceiptText"

    inputs = [
        MessageTextInput(
            name="customer_ref",
            display_name="Customer Ref",
            info="Stable id of the customer being invoiced (no name/email inlined).",
            required=True,
            tool_mode=True,
        ),
        MultilineInput(
            name="line_items",
            display_name="Line Items (JSON)",
            info='JSON array of line items, e.g. [{"description":"...","quantity":"1","unit_price":"100.00","tax_code":"standard"}].',
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="currency",
            display_name="Currency",
            value="SAR",
            info="ISO-4217 currency for the invoice totals.",
        ),
        MessageTextInput(
            name="issue_date",
            display_name="Issue Date",
            info="Issue date (ISO-8601 date, e.g. 2026-07-06).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="due_date",
            display_name="Due Date",
            info="Due date (ISO-8601 date).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="tax_rate",
            display_name="Tax Rate",
            value="0.15",
            info="Tax rate as a decimal fraction applied to taxable line items (e.g. 0.15 for 15% VAT).",
        ),
        MultilineInput(
            name="discounts",
            display_name="Discounts (JSON)",
            info='Optional JSON array of discounts, e.g. [{"amount":"25.00","reason":"early payment"}].',
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="invoice_output",
            display_name="Invoice Data",
            method="build",
        ),
    ]

    def build(self) -> Message:
        customer_ref = self.customer_ref or "<customer_ref>"
        # Placeholder — Decimal total math + InvoiceData assembly is build
        # phase. Emits the InvoiceData contract. Never raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"customer_ref":"{customer_ref}","line_items":[],"sub_total":"0.00",'
                '"tax_total":"0.00","total":"0.00"}}}'
            )
        )