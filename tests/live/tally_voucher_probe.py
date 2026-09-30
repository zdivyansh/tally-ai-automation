"""
Live probe of Tally Sales-voucher import behaviour.

Creates test vouchers with a separate number series (TEST/CLAUDE/xxxx),
prints Tally's raw import response, verifies what was stored, then deletes
each test voucher. The real invoice number series is not touched.

Usage (party and item must exist in the open company):
  export TALLY_TEST_PARTY="Some Customer" TALLY_TEST_ITEM="Some Item"
  uv run python tests/live/tally_voucher_probe.py

Superseded by tests/live/test_tally_live.py; kept for reference.
"""

import os
import re
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import httpx

TALLY_URL = f"{os.getenv('TALLY_HOST', 'http://localhost')}:{os.getenv('TALLY_PORT', '9000')}"

# Tally here runs in Educational mode: only the 1st, 2nd and 31st of a month are accepted.
DATE = os.getenv("TALLY_TEST_DATE", "20260102")
PARTY = os.getenv("TALLY_TEST_PARTY", "")
ITEM = os.getenv("TALLY_TEST_ITEM", "")
UNIT = "Ctn"
QTY = 1
RATE = 2086.40


def _strip_bad_entities(text: str) -> str:
    def fix(m):
        s = m.group(1)
        code = int(s[1:], 16) if s.lower().startswith("x") else int(s)
        return "" if code < 32 and code not in (9, 10, 13) else m.group(0)

    return re.sub(r"&#(x?[0-9a-fA-F]+);", fix, text)


def post(xml: str) -> str:
    resp = httpx.post(TALLY_URL, content=xml.encode("utf-8"), timeout=120)
    resp.raise_for_status()
    return _strip_bad_entities(resp.text)


def import_summary(text: str) -> dict:
    root = ET.fromstring(text)
    return {
        k.lower(): root.findtext(f".//{k}")
        for k in ("CREATED", "ALTERED", "DELETED", "ERRORS", "EXCEPTIONS", "LINEERROR", "LASTVCHID")
    }


def envelope(voucher_xml: str) -> str:
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Import</TALLYREQUEST>"
        "<TYPE>Data</TYPE><ID>Vouchers</ID></HEADER><BODY><DESC><STATICVARIABLES/></DESC>"
        '<DATA><TALLYMESSAGE xmlns:UDF="TallyUDF">'
        f"{voucher_xml}"
        "</TALLYMESSAGE></DATA></BODY></ENVELOPE>"
    )


def repo_template_voucher(number: str, item: str) -> str:
    """Mirrors TALLY_SALES_XML_TEMPLATE in sales_voucher_agent.py (no units, no batch, no GST)."""
    amt = f"{QTY * RATE:.2f}"
    return f"""
<VOUCHER VCHTYPE="Sales" ACTION="Create">
  <DATE>{DATE}</DATE>
  <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
  <PARTYLEDGERNAME>{escape(PARTY)}</PARTYLEDGERNAME>
  <VOUCHERNUMBER>{number}</VOUCHERNUMBER>
  <NARRATION>AUTOMATED TEST - safe to delete</NARRATION>
  <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
  <ISINVOICE>Yes</ISINVOICE>
  <LEDGERENTRIES.LIST>
    <LEDGERNAME>{escape(PARTY)}</LEDGERNAME>
    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
    <AMOUNT>-{amt}</AMOUNT>
  </LEDGERENTRIES.LIST>
  <ALLINVENTORYENTRIES.LIST>
    <STOCKITEMNAME>{escape(item)}</STOCKITEMNAME>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <RATE>{RATE}</RATE>
    <AMOUNT>{amt}</AMOUNT>
    <ACTUALQTY>{QTY}</ACTUALQTY>
    <BILLEDQTY>{QTY}</BILLEDQTY>
    <ACCOUNTINGALLOCATIONS.LIST>
      <LEDGERNAME>Sales</LEDGERNAME>
      <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
      <AMOUNT>{amt}</AMOUNT>
    </ACCOUNTINGALLOCATIONS.LIST>
  </ALLINVENTORYENTRIES.LIST>
</VOUCHER>"""


def corrected_voucher(number: str, gst_pct: float = 5.0) -> str:
    """Units on qty/rate, batch allocation, CGST/SGST and round-off, like real ARH invoices."""
    amt = round(QTY * RATE, 2)
    half = round(amt * gst_pct / 200, 2)
    gross = amt + 2 * half
    total = round(gross)
    roff = round(total - gross, 2)
    return f"""
<VOUCHER VCHTYPE="Sales" ACTION="Create" OBJVIEW="Invoice Voucher View">
  <DATE>{DATE}</DATE>
  <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
  <PARTYLEDGERNAME>{escape(PARTY)}</PARTYLEDGERNAME>
  <PARTYNAME>{escape(PARTY)}</PARTYNAME>
  <VOUCHERNUMBER>{number}</VOUCHERNUMBER>
  <NARRATION>AUTOMATED TEST - safe to delete</NARRATION>
  <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
  <ISINVOICE>Yes</ISINVOICE>
  <LEDGERENTRIES.LIST>
    <LEDGERNAME>{escape(PARTY)}</LEDGERNAME>
    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
    <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
    <AMOUNT>-{total:.2f}</AMOUNT>
  </LEDGERENTRIES.LIST>
  <LEDGERENTRIES.LIST>
    <LEDGERNAME>CGST</LEDGERNAME>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <AMOUNT>{half:.2f}</AMOUNT>
  </LEDGERENTRIES.LIST>
  <LEDGERENTRIES.LIST>
    <LEDGERNAME>SGST</LEDGERNAME>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <AMOUNT>{half:.2f}</AMOUNT>
  </LEDGERENTRIES.LIST>
  <LEDGERENTRIES.LIST>
    <LEDGERNAME>Round Off (+/-)</LEDGERNAME>
    <ISDEEMEDPOSITIVE>{"No" if roff >= 0 else "Yes"}</ISDEEMEDPOSITIVE>
    <AMOUNT>{roff:.2f}</AMOUNT>
  </LEDGERENTRIES.LIST>
  <ALLINVENTORYENTRIES.LIST>
    <STOCKITEMNAME>{escape(ITEM)}</STOCKITEMNAME>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <RATE>{RATE:.2f}/{UNIT}</RATE>
    <AMOUNT>{amt:.2f}</AMOUNT>
    <ACTUALQTY> {QTY} {UNIT}</ACTUALQTY>
    <BILLEDQTY> {QTY} {UNIT}</BILLEDQTY>
    <BATCHALLOCATIONS.LIST>
      <GODOWNNAME>Main Location</GODOWNNAME>
      <BATCHNAME>Primary Batch</BATCHNAME>
      <AMOUNT>{amt:.2f}</AMOUNT>
      <ACTUALQTY> {QTY} {UNIT}</ACTUALQTY>
      <BILLEDQTY> {QTY} {UNIT}</BILLEDQTY>
    </BATCHALLOCATIONS.LIST>
    <ACCOUNTINGALLOCATIONS.LIST>
      <LEDGERNAME>Sales</LEDGERNAME>
      <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
      <AMOUNT>{amt:.2f}</AMOUNT>
    </ACCOUNTINGALLOCATIONS.LIST>
  </ALLINVENTORYENTRIES.LIST>
</VOUCHER>"""


def fetch_voucher(number: str):
    xml = f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>c</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT><SVFROMDATE TYPE="Date">20000101</SVFROMDATE><SVTODATE TYPE="Date">20991231</SVTODATE></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="c"><TYPE>Voucher</TYPE><FETCH>Date,VoucherNumber,MasterID,PartyLedgerName,AllInventoryEntries.*,LedgerEntries.*</FETCH><FILTER>F</FILTER></COLLECTION><SYSTEM TYPE="Formulae" NAME="F">$VoucherNumber = "{number}"</SYSTEM></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    root = ET.fromstring(post(xml))
    return [v for v in root.iter("VOUCHER") if v.findtext("VOUCHERNUMBER") == number]


def describe(v) -> None:
    print(
        f"    stored: date={v.findtext('DATE')} party={v.findtext('PARTYLEDGERNAME')} masterid={(v.findtext('MASTERID') or '').strip()}"
    )
    for i in v.iter("ALLINVENTORYENTRIES.LIST"):
        print(
            f"      item={i.findtext('STOCKITEMNAME')!r} qty={i.findtext('ACTUALQTY')!r} "
            f"billed={i.findtext('BILLEDQTY')!r} rate={i.findtext('RATE')!r} amount={i.findtext('AMOUNT')!r} "
            f"batches={len(i.findall('BATCHALLOCATIONS.LIST'))}"
        )
    for l in v.findall("LEDGERENTRIES.LIST"):
        print(f"      ledger={l.findtext('LEDGERNAME')!r} amount={l.findtext('AMOUNT')!r}")


def delete_voucher(number: str) -> dict:
    xml = envelope(
        f'<VOUCHER DATE="{DATE}" TAGNAME="Voucher Number" TAGVALUE="{number}" '
        f'ACTION="Delete" VCHTYPE="Sales"><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME></VOUCHER>'
    )
    return import_summary(post(xml))


def run_case(title: str, number: str, voucher_xml: str) -> None:
    print(f"\n=== {title} ({number}) ===")
    text = post(envelope(voucher_xml))
    print("  import response:", import_summary(text))
    found = fetch_voucher(number)
    print(f"  vouchers found with this number: {len(found)}")
    for v in found:
        describe(v)
    if found:
        print("  cleanup:", delete_voucher(number))
        print(f"  still present after delete: {len(fetch_voucher(number))}")


def main() -> int:
    if not PARTY or not ITEM:
        print("set TALLY_TEST_PARTY and TALLY_TEST_ITEM")
        return 2
    run_case(
        "1. Repo template, valid item", "TEST/CLAUDE/0001", repo_template_voucher("TEST/CLAUDE/0001", ITEM)
    )
    run_case(
        "2. Repo template, item not in Tally",
        "TEST/CLAUDE/0002",
        repo_template_voucher("TEST/CLAUDE/0002", ITEM.rstrip(".")),
    )
    run_case(
        "3. Corrected XML (units, batch, GST, round-off)",
        "TEST/CLAUDE/0003",
        corrected_voucher("TEST/CLAUDE/0003"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
