"""
Per-run undo log. Every Add/Modify/Delete this server performs is recorded
here, one JSON line per operation, flushed to disk immediately (not
batched) so a crash mid-run still leaves a usable log of everything that
*did* complete.

Design choice: each log entry carries its own ready-to-replay reverse QBXML,
built at record time (when the caller has full context: the exact fields it
just wrote, or queried right before changing/deleting them) -- not
reconstructed generically at revert time from a loose snapshot. This keeps
revert simple (replay a stored request) and correct (no guessing how to
rebuild a transaction from scratch).

Revert replays the log in reverse (LIFO): later operations can depend on
earlier ones in the same run (e.g. a transaction posted against a vendor
created earlier in that run), so undoing out of order can fail or leave
orphaned references.
"""

import json
import re
from datetime import datetime, timezone

from .qbxml import build_add_request, build_journal_entry_add_request, wrap_envelope, TxnLine


class RunLog:
    def __init__(self, path, run_id):
        self.path = path
        self.run_id = run_id
        self.seq = 0
        self._fh = open(path, "a", encoding="utf-8")

    def _write(self, entry):
        self.seq += 1
        entry["seq"] = self.seq
        entry["run_id"] = self.run_id
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._fh.write(json.dumps(entry) + "\n")
        self._fh.flush()

    def record(self, description, reverse_qbxml, reverse_kind, edit_seq_placeholder=None):
        """
        description: human-readable summary for audit/reporting.
        reverse_qbxml: the QBXML request that undoes this operation.
        reverse_kind: "del" | "mod" | "recreate" -- used only for reporting;
            replay always just sends reverse_qbxml.
        edit_seq_placeholder: for a "mod" reversal, the EditSequence value
            embedded in reverse_qbxml. If QuickBooks rejects it as stale at
            revert time (statusCode 3200), it conveniently echoes the
            object's current EditSequence in its own error response --
            replay substitutes that in for this placeholder and retries
            once, no separate lookup call needed.
        """
        self._write({
            "description": description,
            "reverse_qbxml": reverse_qbxml,
            "reverse_kind": reverse_kind,
            "edit_seq_placeholder": edit_seq_placeholder,
        })

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def load_entries(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # Partial last line from a crash mid-write -- stop here,
                # everything before it is still valid and revertible.
                break
    return entries


def revert_run(log_path, send_qbxml, dry_run=False):
    """
    send_qbxml: callable(qbxml_str) -> raw response string. Pass
    QuickBooksSession().send for a real revert.

    Reverts every operation in log_path, in reverse order. Returns a list of
    (entry, status, detail) so the caller can report exactly what happened --
    a partial revert (some entries fail) is reported, not swallowed.
    """
    entries = load_entries(log_path)
    results = []

    for entry in reversed(entries):
        if dry_run:
            results.append((entry, "would_revert", entry["description"]))
            continue

        try:
            resp = send_qbxml(entry["reverse_qbxml"])
        except Exception as e:
            results.append((entry, "error", "exception: " + str(e)))
            continue

        if 'statusCode="0"' in resp:
            results.append((entry, "reverted", entry["description"]))
            continue

        placeholder = entry.get("edit_seq_placeholder")
        if placeholder and "3200" in resp:
            new_seq = re.search(r"<EditSequence>([^<]+)</EditSequence>", resp)
            if new_seq:
                # Must replace only the <EditSequence> tag's content, not do
                # a blind string substitution: the placeholder value is a
                # numeric suffix that can also be embedded inside the
                # TxnID (e.g. "ECEC-1783008421"), so a naive .replace()
                # would corrupt the TxnID too and point the retry at a
                # nonexistent object.
                retried_qbxml = re.sub(
                    r"<EditSequence>" + re.escape(placeholder) + r"</EditSequence>",
                    "<EditSequence>" + new_seq.group(1) + "</EditSequence>",
                    entry["reverse_qbxml"], count=1,
                )
                try:
                    resp2 = send_qbxml(retried_qbxml)
                except Exception as e:
                    results.append((entry, "error", "retry exception: " + str(e)))
                    continue
                if 'statusCode="0"' in resp2:
                    results.append((entry, "reverted_after_retry", entry["description"]))
                    continue
                resp = resp2

        results.append((entry, "error", resp[:400]))

    return results


# --- helpers for building reverse QBXML at record time -----------------

def reverse_for_add(txn_type, txn_id):
    """An Add is undone by deleting the TxnID QuickBooks handed back."""
    qbxml = wrap_envelope(
        ['<TxnDelRq requestID="1"><TxnDelType>' + txn_type + '</TxnDelType><TxnID>' + txn_id + '</TxnID></TxnDelRq>'],
        on_error="stopOnError",
    )
    return qbxml, "del"


def reverse_for_vendor_add(list_id):
    """
    A VendorAdd is undone by deleting the list entry. This only succeeds if
    nothing references it -- true once every txn that used it (posted later
    in the same run) has already been reverted by the LIFO replay order.
    Caller should fall back to reverse_for_vendor_deactivate if this fails
    with "does not exist" / "in use" (statusCode 3120).
    """
    qbxml = wrap_envelope(
        ['<ListDelRq requestID="1"><ListDelType>Vendor</ListDelType><ListID>' + list_id + '</ListID></ListDelRq>'],
        on_error="stopOnError",
    )
    return qbxml, "del"


def reverse_for_vendor_deactivate(list_id, edit_seq):
    qbxml = wrap_envelope(
        ['<VendorModRq requestID="1"><VendorMod>'
         '<ListID>' + list_id + '</ListID>'
         '<EditSequence>' + edit_seq + '</EditSequence>'
         '<IsActive>false</IsActive>'
         '</VendorMod></VendorModRq>'],
        on_error="stopOnError",
    )
    return qbxml, "mod"


def reverse_for_line_mod(txn_type, txn_id, edit_seq, line_id, original_account, original_amount, original_memo=None):
    """Undoes an ExpenseLineMod/DepositLineMod amount/account change by
    restoring the original values."""
    line_mod_tag = {"Check": "ExpenseLineMod", "Deposit": "DepositLineMod",
                     "CreditCardCharge": "ExpenseLineMod", "CreditCardCredit": "ExpenseLineMod"}[txn_type]
    memo_xml = "<Memo>" + original_memo.replace("&", "&amp;") + "</Memo>" if original_memo else ""
    qbxml = wrap_envelope(
        ['<' + txn_type + 'ModRq requestID="1"><' + txn_type + 'Mod>'
         '<TxnID>' + txn_id + '</TxnID>'
         '<EditSequence>' + edit_seq + '</EditSequence>'
         '<' + line_mod_tag + '>'
         '<TxnLineID>' + line_id + '</TxnLineID>'
         '<AccountRef><FullName>' + original_account.replace("&", "&amp;") + '</FullName></AccountRef>'
         '<Amount>' + ("%.2f" % original_amount) + '</Amount>'
         + memo_xml +
         '</' + line_mod_tag + '>'
         '</' + txn_type + 'Mod></' + txn_type + 'ModRq>'],
        on_error="stopOnError",
    )
    return qbxml, "mod", edit_seq


def reverse_for_journal_recreate(before):
    """
    Undoes a JournalEntry delete-and-recreate (see
    qb_split_journal_entry_line) by rebuilding the entry from a full
    snapshot captured immediately before deletion. `before` must have:
    txn_date, refnum (or None), header_memo (or None), is_adjustment (or
    None), lines (list of dicts with kind/account/amount/memo/entity).
    """
    fragment = build_journal_entry_add_request(
        1, before["txn_date"], before["lines"],
        refnum=before.get("refnum"), header_memo=before.get("header_memo"),
        is_adjustment=before.get("is_adjustment"),
    )
    qbxml = wrap_envelope([fragment], on_error="stopOnError")
    return qbxml, "recreate"


def reverse_for_recreate(txn_type, before):
    """
    Undoes a Delete by recreating the transaction from a full snapshot
    captured immediately before deletion. `before` must have: account,
    payee (or None), refnum (or None), txn_date, header_memo (or None),
    lines (list of dicts with account/amount/memo).
    """
    lines = [TxnLine(account=l["account"], amount=l["amount"], memo=l.get("memo")) for l in before["lines"]]
    fragment = build_add_request(
        txn_type, 1, before["account"], before["txn_date"], lines,
        payee=before.get("payee"), refnum=before.get("refnum"), header_memo=before.get("header_memo"),
    )
    qbxml = wrap_envelope([fragment], on_error="stopOnError")
    return qbxml, "recreate"
