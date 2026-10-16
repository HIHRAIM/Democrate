"""The outbox of the banks' audit channels.

The economy notices administrative events in synchronous code — a payroll
run inside the monthly tick, a fine inside a command — and cannot post to a
platform from there. It writes a row instead, and main.py: audit_loop posts
the rows in the channel's language and deletes them. A row survives a
restart, so an event is posted late rather than lost; an event of a bank
with no audit channel is never written at all (economy/audit.py decides).

Not this module's zone: which events exist and how they read
(economy/audit.py, the `audit_<event>` keys), where a bank's channel is
(db/banks.py: set_bank_audit).
"""
import json

from db import conn, cur, _now

def enqueue_audit(bank_code, event, fields):
    """Queue one event of a bank for its audit channel."""
    cur.execute("INSERT INTO audit_outbox (bank_code, event, fields, created_at)"
                " VALUES (?,?,?,?)", (bank_code, event, json.dumps(fields, ensure_ascii=False),
                                      _now()))
    conn.commit()

def due_audits(limit=50):
    """The oldest queued events, oldest first, with `fields` decoded."""
    rows = cur.execute("SELECT * FROM audit_outbox ORDER BY id LIMIT ?", (int(limit),)).fetchall()
    return [{"id": r["id"], "bank_code": r["bank_code"], "event": r["event"],
             "fields": json.loads(r["fields"] or "{}"), "created_at": r["created_at"]}
            for r in rows]

def delete_audit(audit_id):
    """Drop a posted (or undeliverable) event."""
    cur.execute("DELETE FROM audit_outbox WHERE id=?", (int(audit_id),))
    conn.commit()
