# QBBridge

**An MCP frontend for QuickBooks Desktop.**

Gives any MCP-compatible client (Claude Desktop, Claude Code, or anything
else that speaks MCP) safe, structured access to a local QuickBooks Desktop
company file: read accounts/vendors/transactions, post new transactions,
and modify or delete existing ones -- with every write automatically logged
so it can be reverted.

QuickBooks Desktop's automation API (QBXML/QBFC) only works on the machine
(or LAN) where QuickBooks is actually installed -- there is no cloud proxy
for it. QBBridge runs locally, right next to QuickBooks, and your company
file's data never has to leave that machine to be used by an MCP client.

## Why this exists

QBXML is powerful but has real, undocumented sharp edges: several element
orderings are order-sensitive in ways the schema alone doesn't tell you,
and getting them wrong produces a bare COM parse exception with no
indication of which field was the problem. This package exists so you
don't have to rediscover those the hard way -- they're encoded and tested
once, here.

## What it gives you

- **Read**: chart of accounts, vendor list, a vendor's transaction history
  (so you can classify a new transaction against how this vendor has
  actually been booked before, not a guess), flexible transaction queries,
  and a duplicate/overlap checker.
- **Write**: add/modify/delete transactions (Check, Deposit,
  CreditCardCharge, CreditCardCredit) and vendors.
- **Safety**: every write is automatically recorded to an append-only undo
  log as it happens (not batched at the end -- a crash mid-run still leaves
  a usable log of everything that *did* complete). Any run can be reverted,
  in reverse order, with one call.

## What it deliberately does NOT do

This server has no business-specific logic in it -- no classification
rules, no confidence thresholds, no assumptions about what kind of
business you run. Deciding *which* vendor maps to *which* account, when to
trust a match versus flag it for a human, and how to parse a specific
bank's statement format is policy, and policy differs by business. That
belongs in the agent/skill calling this server, not in the server itself.
This keeps the server itself universal.

## The companion skill

The server is the mechanism; [`skills/qbbridge-bookkeeping/SKILL.md`](skills/qbbridge-bookkeeping/SKILL.md)
is the judgment. It's a Claude skill that teaches an agent how to actually
use these tools well -- when to trust a vendor's transaction history vs.
flag it for a human, how to handle payment/credit lines vs. purchases,
and the safety habits (checking the open company file, checking for
duplicates) to follow before every write. Install the MCP server for the
plumbing; add this skill for the bookkeeping judgment on top of it.

## Requirements

- Windows, with QuickBooks Desktop installed and the target company file
  open.
- Python 3.10+.
- `pywin32` (installed automatically on Windows).

## Install

```bash
pip install qbbridge-mcp
```

## Configure as an MCP server

Point your MCP client at the `qbbridge-mcp` command. For Claude Desktop,
add to your MCP config:

```json
{
  "mcpServers": {
    "qbbridge": {
      "command": "qbbridge-mcp"
    }
  }
}
```

## Safety notes

- Every write tool accepts an optional `expected_company_file` parameter.
  Pass the path you expect QuickBooks to have open (from a prior
  `qb_company_info` call); the tool refuses to run if a different file is
  actually open. This is the single check that prevents posting to the
  wrong client's file.
- `qb_delete_transaction` requires you to pass a full snapshot of the
  transaction (query it first) -- without it, a delete cannot be undone.
- Undo logs are written to `./qbbridge_runs` by default, or
  `$QBBRIDGE_RUNS_DIR` if set.

## License

MIT
