"""
QBBridge: an MCP frontend for QuickBooks Desktop.

Exposes QuickBooks Desktop (via QBXML/QBFC) as a set of MCP tools: read
(accounts, vendors, transactions, vendor history), write (add/modify/delete
transactions and vendors), and safety (every write is undo-logged; revert
any run). Deliberately does not include any business-specific classification
logic (which vendor maps to which account, confidence thresholds, etc.) --
that's policy, and belongs in the calling agent/skill, not in this server.
Different businesses need different policy; this server should work the
same for all of them.
"""

import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .qbxml import (
    QuickBooksSession, QuickBooksError, TxnLine,
    build_add_request, build_query_request, build_journal_entry_add_request,
    wrap_envelope, parse_txn_blocks, extract,
)
from .undo import (
    RunLog, revert_run, reverse_for_add, reverse_for_vendor_add,
    reverse_for_vendor_deactivate, reverse_for_line_mod, reverse_for_journal_recreate,
    reverse_for_recreate,
)

mcp = FastMCP("qbbridge")

RUNS_DIR = Path(os.environ.get("QBBRIDGE_RUNS_DIR", "./qbbridge_runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

_session = QuickBooksSession()


def _new_run_log():
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    path = RUNS_DIR / (run_id + ".undolog.jsonl")
    return RunLog(str(path), run_id), run_id, str(path)


def _check_expected_file(expected_company_file):
    """If the caller specified which company file it expects to be open,
    verify that before doing anything -- this is the single check that
    would have prevented every "wrong client's file" mistake this project
    made by hand before this safeguard existed."""
    if not expected_company_file:
        return
    actual = _session.current_company_file()
    if os.path.normcase(os.path.abspath(actual)) != os.path.normcase(os.path.abspath(expected_company_file)):
        raise ValueError(
            "Refusing to proceed: expected company file \"" + expected_company_file +
            "\" but QuickBooks currently has \"" + actual + "\" open."
        )


# --- read tools --------------------------------------------------------

@mcp.tool()
def qb_company_info() -> dict:
    """Returns the company file path QuickBooks Desktop currently has open.
    Call this first, and before any write, to confirm you're pointed at the
    right client's file."""
    return {"company_file": _session.current_company_file()}


@mcp.tool()
def qb_list_accounts() -> list[dict]:
    """Lists all accounts in the chart of accounts (name, type, active,
    balance, total_balance). Use this to confirm an account name exists
    exactly before trying to post to it -- QuickBooks rejects an
    AccountRef to a nonexistent or misspelled account.

    balance vs total_balance: for a parent account with subaccounts,
    "balance" is that account's OWN activity only (excludes subaccounts);
    "total_balance" is the rolled-up figure including every subaccount.
    When checking whether a restatement changed a parent account's real
    total, use total_balance -- "balance" will look like it dropped even
    when nothing left the account tree, since it only reflects direct
    postings and moving activity into a new subaccount is exactly what a
    category split does.
    """
    resp = _session.send(wrap_envelope(['<AccountQueryRq requestID="1" />'], on_error="stopOnError"))
    out = []
    for block in parse_txn_blocks(resp, "AccountRet"):
        out.append({
            "name": extract(r"<FullName>([^<]+)</FullName>", block),
            "type": extract(r"<AccountType>([^<]+)</AccountType>", block),
            "active": extract(r"<IsActive>([^<]+)</IsActive>", block) == "true",
            "balance": extract(r"<Balance>([^<]+)</Balance>", block),
            "total_balance": extract(r"<TotalBalance>([^<]+)</TotalBalance>", block),
        })
    return out


@mcp.tool()
def qb_list_vendors(active_only: bool = True) -> list[dict]:
    """Lists vendors (name, ListID). Use this to confirm a payee exists
    exactly before posting a transaction against it."""
    filt = "<ActiveStatus>ActiveOnly</ActiveStatus>" if active_only else ""
    resp = _session.send(wrap_envelope(['<VendorQueryRq requestID="1">' + filt + '</VendorQueryRq>'], on_error="stopOnError"))
    out = []
    for block in parse_txn_blocks(resp, "VendorRet"):
        out.append({
            "name": extract(r"<Name>([^<]+)</Name>", block),
            "list_id": extract(r"<ListID>([^<]+)</ListID>", block),
            "active": extract(r"<IsActive>([^<]+)</IsActive>", block) == "true",
        })
    return out


@mcp.tool()
def qb_vendor_history(vendor_name: str, txn_types: Optional[list[str]] = None) -> dict:
    """
    Returns which account(s) this vendor's past transactions were booked
    to, with counts -- e.g. {"Food Purchases": 175}. Use this before
    guessing an account for a new transaction from a known vendor: prefer
    whatever this vendor has actually been booked to before, not a
    plausible-sounding guess.
    """
    types = txn_types or ["Check", "CreditCardCharge", "CreditCardCredit"]
    counts = {}
    for i, t in enumerate(types, start=1):
        q = build_query_request(t, i, entity=vendor_name, include_line_items=True)
        resp = _session.send(wrap_envelope([q]))
        ret_tag = t + "Ret"
        for block in parse_txn_blocks(resp, ret_tag):
            account = extract(r"<(?:ExpenseLineRet|DepositLineRet)>.*?<FullName>([^<]+)</FullName>", block)
            if account:
                counts[account] = counts.get(account, 0) + 1
    return {"vendor": vendor_name, "account_counts": counts}


@mcp.tool()
def qb_query_transactions(
    txn_type: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    account: Optional[str] = None,
    entity: Optional[str] = None,
) -> list[dict]:
    """
    Queries transactions of one type (Check, Deposit, CreditCardCharge,
    CreditCardCredit, Transfer, JournalEntry). date_from/date_to as
    YYYY-MM-DD. Returns normalized records: date, amount, payee, account
    (first line), memo, txn_id, edit_sequence.
    """
    q = build_query_request(txn_type, 1, date_from=date_from, date_to=date_to, account=account, entity=entity)
    resp = _session.send(wrap_envelope([q]))
    ret_tag = txn_type + "Ret"
    out = []
    for block in parse_txn_blocks(resp, ret_tag):
        out.append({
            "txn_id": extract(r"<TxnID>([^<]+)</TxnID>", block),
            "edit_sequence": extract(r"<EditSequence>([^<]+)</EditSequence>", block),
            "date": extract(r"<TxnDate>([^<]+)</TxnDate>", block),
            "amount": extract(r"<Amount>([^<]+)</Amount>", block),
            "payee": extract(r"<PayeeEntityRef>\s*<ListID>[^<]*</ListID>\s*<FullName>([^<]+)</FullName>", block),
            "account": extract(r"<(?:ExpenseLineRet|DepositLineRet)>.*?<FullName>([^<]+)</FullName>", block),
            "memo": extract(r"<Memo>([^<]*)</Memo>", block),
        })
    return out


@mcp.tool()
def qb_check_duplicate(
    date: str,
    amount: float,
    account: Optional[str] = None,
    date_tolerance_days: int = 5,
    txn_types: Optional[list[str]] = None,
) -> list[dict]:
    """
    Checks whether a transaction with this date+amount already exists
    somewhere in the company file, within a tolerance window (banks don't
    always post the two sides of a transfer/payment on the exact same
    date). Use this before posting anything, to avoid double-entry --
    especially for payments/transfers, where the same real-world event can
    get recorded from either side of a linked account.
    """
    from datetime import timedelta

    types = txn_types or ["Check", "Deposit", "CreditCardCharge", "CreditCardCredit", "Transfer"]
    target_date = datetime.strptime(date, "%Y-%m-%d")
    date_from = (target_date - timedelta(days=date_tolerance_days)).strftime("%Y-%m-%d")
    date_to = (target_date + timedelta(days=date_tolerance_days)).strftime("%Y-%m-%d")

    matches = []
    for i, t in enumerate(types, start=1):
        q = build_query_request(t, i, date_from=date_from, date_to=date_to, account=account)
        resp = _session.send(wrap_envelope([q]))
        ret_tag = t + "Ret"
        for block in parse_txn_blocks(resp, ret_tag):
            amt = extract(r"<Amount>([^<]+)</Amount>", block)
            d = extract(r"<TxnDate>([^<]+)</TxnDate>", block)
            if amt and abs(float(amt)) == round(abs(amount), 2):
                matches.append({"txn_type": t, "date": d, "amount": amt,
                                 "txn_id": extract(r"<TxnID>([^<]+)</TxnID>", block)})
    return matches


# --- write tools ---------------------------------------------------------

@mcp.tool()
def qb_add_transaction(
    txn_type: str,
    account: str,
    txn_date: str,
    lines: list[dict],
    payee: Optional[str] = None,
    refnum: Optional[str] = None,
    header_memo: Optional[str] = None,
    expected_company_file: Optional[str] = None,
) -> dict:
    """
    Adds a transaction (Check, Deposit, CreditCardCharge, or
    CreditCardCredit). lines: [{"account": str, "amount": float, "memo":
    str|None}, ...]. Automatically logs an undo entry -- every add can be
    reverted with qb_revert_run using the returned run_id.

    Pass expected_company_file (from a prior qb_company_info call) to have
    this refuse to run if a different file is open than you expect.
    """
    _check_expected_file(expected_company_file)
    txn_lines = [TxnLine(account=l["account"], amount=l["amount"], memo=l.get("memo")) for l in lines]
    fragment = build_add_request(txn_type, 1, account, txn_date, txn_lines,
                                  payee=payee, refnum=refnum, header_memo=header_memo)
    qbxml = wrap_envelope([fragment], on_error="stopOnError")

    run_log, run_id, log_path = _new_run_log()
    try:
        _, body, _ = _session.send_single(qbxml, txn_type + "AddRs")
        txn_id = extract(r"<TxnID>([^<]+)</TxnID>", body)
        rev_qbxml, kind = reverse_for_add(txn_type, txn_id)
        run_log.record(
            txn_type + " " + (payee or "") + " " + ("%.2f" % sum(l.amount for l in txn_lines)) + " on " + txn_date,
            rev_qbxml, kind,
        )
    except QuickBooksError as e:
        raise
    finally:
        run_log.close()

    return {"txn_id": txn_id, "run_id": run_id, "run_log_path": log_path}


def _query_journal_entry_snapshot(txn_id):
    """
    Fetches one JournalEntry's full current state: header (txn_date,
    refnum, header_memo, is_adjustment) plus every line in original order
    (kind, line_id, account, amount, memo, entity). Used both to validate a
    split before writing and to capture the "before" snapshot a delete
    needs to be undoable.
    """
    q = build_query_request("JournalEntry", 1, txn_id=txn_id, include_line_items=True)
    resp = _session.send(wrap_envelope([q]))
    blocks = parse_txn_blocks(resp, "JournalEntryRet")
    if not blocks:
        raise ValueError("JournalEntry " + txn_id + " not found")
    block = blocks[0]

    header = re.split(r"<Journal(?:Debit|Credit)Line>", block, maxsplit=1)[0]
    is_adj_raw = extract(r"<IsAdjustment>([^<]+)</IsAdjustment>", header)

    lines = []
    for m in re.finditer(r"<Journal(Debit|Credit)Line>(.*?)</Journal\1Line>", block, re.S):
        kind, sub = m.group(1).lower(), m.group(2)
        lines.append({
            "kind": kind,
            "line_id": extract(r"<TxnLineID>([^<]+)</TxnLineID>", sub),
            "account": extract(r"<FullName>([^<]+)</FullName>", sub),
            "amount": float(extract(r"<Amount>([^<]+)</Amount>", sub)),
            "memo": extract(r"<Memo>([^<]+)</Memo>", sub),
            "entity": extract(r"<EntityRef>.*?<FullName>([^<]+)</FullName>", sub),
        })

    return {
        "txn_date": extract(r"<TxnDate>([^<]+)</TxnDate>", header),
        "refnum": extract(r"<RefNumber>([^<]+)</RefNumber>", header),
        "header_memo": extract(r"<Memo>([^<]+)</Memo>", header),
        "is_adjustment": (is_adj_raw == "true") if is_adj_raw is not None else None,
        "lines": lines,
    }


@mcp.tool()
def qb_split_journal_entry_line(
    txn_id: str,
    target_line_id: str,
    splits: list[dict],
    expected_company_file: Optional[str] = None,
) -> dict:
    """
    Replaces one lump-sum JournalDebitLine/JournalCreditLine with several
    lines across different accounts, without changing the entry's date,
    other lines, or total. Built specifically for restating a lump-sum
    sales line into per-category lines from an external source (e.g. a POS
    category report) -- not a general-purpose JE editor.

    splits: [{"account": str, "amount": float, "memo": str|None}, ...] --
    must sum to exactly the target line's current amount (checked here;
    QuickBooks would otherwise happily accept a mismatched total). Splits
    inherit the target line's EntityRef unless a split explicitly sets its
    own "entity" key.

    Mechanism: QBXML's JournalEntryModRq (confirmed against the QBXML SDK
    reference schema) only supports JournalLineMod on lines that already
    exist -- there is no way to add a new line to an existing JE. So this
    deletes the whole entry and recreates it with the target line replaced
    by `splits`, everything else identical. The undo log captures a full
    pre-delete snapshot, so revert recreates the original entry exactly
    (as a new TxnID -- QuickBooks does not allow restoring a deleted
    transaction's original TxnID).
    """
    _check_expected_file(expected_company_file)
    if len(splits) < 2:
        raise ValueError("splits must have at least 2 entries -- otherwise there's nothing to split")

    snapshot = _query_journal_entry_snapshot(txn_id)
    target = next((l for l in snapshot["lines"] if l["line_id"] == target_line_id), None)
    if target is None:
        raise ValueError("line " + target_line_id + " not found on JournalEntry " + txn_id)

    total_split = round(sum(s["amount"] for s in splits), 2)
    if total_split != round(target["amount"], 2):
        raise ValueError(
            "splits sum to %.2f but target line's current amount is %.2f -- refusing to post a mismatched split"
            % (total_split, target["amount"])
        )

    new_lines = [l for l in snapshot["lines"] if l["line_id"] != target_line_id] + [
        {
            "kind": target["kind"],
            "account": s["account"],
            "amount": s["amount"],
            "memo": s.get("memo"),
            "entity": s.get("entity", target.get("entity")),
        }
        for s in splits
    ]

    run_log, run_id, log_path = _new_run_log()
    try:
        rev_qbxml, kind = reverse_for_journal_recreate(snapshot)
        run_log.record(
            "delete JournalEntry " + txn_id + " (pre-split snapshot)", rev_qbxml, kind,
        )
        _session.send_single(
            wrap_envelope(
                ['<TxnDelRq requestID="1"><TxnDelType>JournalEntry</TxnDelType><TxnID>' + txn_id + '</TxnID></TxnDelRq>'],
                on_error="stopOnError",
            ),
            "TxnDelRs",
        )

        add_fragment = build_journal_entry_add_request(
            1, snapshot["txn_date"], new_lines,
            refnum=snapshot.get("refnum"), header_memo=snapshot.get("header_memo"),
            is_adjustment=snapshot.get("is_adjustment"),
        )
        _, body, _ = _session.send_single(wrap_envelope([add_fragment], on_error="stopOnError"), "JournalEntryAddRs")
        new_txn_id = extract(r"<TxnID>([^<]+)</TxnID>", body)

        run_log.record(
            "recreate JournalEntry as " + new_txn_id + " with line " + target_line_id + " split into " + str(len(splits)) + " lines",
            *reverse_for_add("JournalEntry", new_txn_id),
        )
    finally:
        run_log.close()

    return {"run_id": run_id, "run_log_path": log_path, "old_txn_id": txn_id, "new_txn_id": new_txn_id}


@mcp.tool()
def qb_modify_transaction_line(
    txn_type: str,
    txn_id: str,
    edit_sequence: str,
    line_id: str,
    new_account: str,
    new_amount: float,
    new_memo: Optional[str] = None,
    original_account: str = "",
    original_amount: float = 0.0,
    original_memo: Optional[str] = None,
    expected_company_file: Optional[str] = None,
) -> dict:
    """
    Modifies one expense/deposit line on an existing transaction. You must
    pass the ORIGINAL account/amount/memo (query the transaction first if
    you don't already have them) so the undo log can restore them exactly.
    """
    _check_expected_file(expected_company_file)
    line_mod_tag = {"Check": "ExpenseLineMod", "Deposit": "DepositLineMod",
                     "CreditCardCharge": "ExpenseLineMod", "CreditCardCredit": "ExpenseLineMod"}[txn_type]
    memo_xml = "<Memo>" + new_memo.replace("&", "&amp;") + "</Memo>" if new_memo else ""
    qbxml = wrap_envelope(
        ['<' + txn_type + 'ModRq requestID="1"><' + txn_type + 'Mod>'
         '<TxnID>' + txn_id + '</TxnID><EditSequence>' + edit_sequence + '</EditSequence>'
         '<' + line_mod_tag + '><TxnLineID>' + line_id + '</TxnLineID>'
         '<AccountRef><FullName>' + new_account.replace("&", "&amp;") + '</FullName></AccountRef>'
         '<Amount>' + ("%.2f" % new_amount) + '</Amount>' + memo_xml +
         '</' + line_mod_tag + '></' + txn_type + 'Mod></' + txn_type + 'ModRq>'],
        on_error="stopOnError",
    )

    run_log, run_id, log_path = _new_run_log()
    try:
        rev_qbxml, kind, placeholder = reverse_for_line_mod(
            txn_type, txn_id, edit_sequence, line_id, original_account, original_amount, original_memo,
        )
        run_log.record(
            txn_type + " " + txn_id + " line " + line_id + " (" + ("%.2f" % original_amount) + " -> " + ("%.2f" % new_amount) + ")",
            rev_qbxml, kind, edit_seq_placeholder=placeholder,
        )
        _session.send_single(qbxml, txn_type + "ModRs")
    finally:
        run_log.close()

    return {"run_id": run_id, "run_log_path": log_path}


@mcp.tool()
def qb_delete_transaction(
    txn_type: str,
    txn_id: str,
    before_snapshot: dict,
    expected_company_file: Optional[str] = None,
) -> dict:
    """
    Deletes a transaction. You must pass before_snapshot -- the full
    transaction as it exists right now (query it first: account, payee,
    refnum, txn_date, header_memo, lines) -- so the undo log can recreate
    it if this needs to be reverted. Deleting without an accurate snapshot
    means it cannot be undone.
    """
    _check_expected_file(expected_company_file)
    run_log, run_id, log_path = _new_run_log()
    try:
        rev_qbxml, kind = reverse_for_recreate(txn_type, before_snapshot)
        run_log.record("delete " + txn_type + " " + txn_id, rev_qbxml, kind)
        qbxml = wrap_envelope(
            ['<TxnDelRq requestID="1"><TxnDelType>' + txn_type + '</TxnDelType><TxnID>' + txn_id + '</TxnID></TxnDelRq>'],
            on_error="stopOnError",
        )
        _session.send_single(qbxml, "TxnDelRs")
    finally:
        run_log.close()

    return {"run_id": run_id, "run_log_path": log_path}


@mcp.tool()
def qb_add_vendor(name: str, expected_company_file: Optional[str] = None) -> dict:
    """Creates a vendor if it doesn't already exist. Check qb_list_vendors
    first -- QuickBooks Desktop vendor names must be unique, and the same
    real-world vendor sometimes already exists under slightly different
    spelling/casing."""
    _check_expected_file(expected_company_file)
    qbxml = wrap_envelope(
        ['<VendorAddRq requestID="1"><VendorAdd><Name>' + name.replace("&", "&amp;") + '</Name></VendorAdd></VendorAddRq>'],
        on_error="stopOnError",
    )
    run_log, run_id, log_path = _new_run_log()
    try:
        _, body, _ = _session.send_single(qbxml, "VendorAddRs")
        list_id = extract(r"<ListID>([^<]+)</ListID>", body)
        rev_qbxml, kind = reverse_for_vendor_add(list_id)
        run_log.record("add vendor " + name, rev_qbxml, kind)
    finally:
        run_log.close()
    return {"list_id": list_id, "run_id": run_id, "run_log_path": log_path}


# --- safety / meta tools ---------------------------------------------------

@mcp.tool()
def qb_revert_run(run_log_path: str, dry_run: bool = False) -> dict:
    """
    Reverts every operation in a run log, in reverse order. Set dry_run=true
    first to preview what would happen without changing anything.
    """
    results = revert_run(run_log_path, _session.send, dry_run=dry_run)
    return {
        "reverted": sum(1 for _, s, _ in results if s.startswith("reverted") or s == "would_revert"),
        "failed": sum(1 for _, s, _ in results if s == "error"),
        "details": [{"description": e["description"], "status": s, "detail": str(d)[:300]} for e, s, d in results],
    }


@mcp.tool()
def qb_list_runs(limit: int = 20) -> list[dict]:
    """Lists recent run logs (most recent first), so you can find a run to
    inspect or revert without knowing its exact filename."""
    files = sorted(RUNS_DIR.glob("*.undolog.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"run_log_path": str(f), "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()} for f in files[:limit]]


def main():
    mcp.run()


if __name__ == "__main__":
    main()
