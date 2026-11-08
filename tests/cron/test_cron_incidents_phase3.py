"""Phase 3a/3b of the hiba-ticket pilot (T2 report §3): the ``delivery`` failure_type
follows the delivery subsystem's OUTCOME instead of error-text keywords, and a deleted
job's open incidents are closed with the job (they can never auto-resolve afterwards).

Measured defect this pins (T2 §3.1): the triage-probe's intentional failure carried
``failure_deliver`` in its stderr, the keyword classifier matched "deliver" anywhere, and
the incident was labelled ``delivery`` even though the send itself succeeded.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron import incidents


def _point_db(monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return incidents


# ── 3a: outcome-driven classification ─────────────────────────────────────────


def test_prose_mentioning_delivery_never_mints_the_bucket(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    incident_id, _ = inc.upsert_incident(
        "job-1", "script exited 1: failure_deliver bot-chat:triage exploded")
    assert inc.get_incident(incident_id)["failure_type"] == "script"


def test_upsert_with_known_failed_send_mints_delivery(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    incident_id, _ = inc.upsert_incident("job-1", "send never left the process", delivery_failed=True)
    assert inc.get_incident(incident_id)["failure_type"] == "delivery"


def test_reclassify_clears_keyword_minted_delivery_when_send_succeeded(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    incident_id, _ = inc.upsert_incident(
        "job-1", "script exited 1: failure_deliver bot-chat:triage exploded",
        failure_type="delivery")  # a pre-phase-3a row
    assert inc.reclassify_incident_for_delivery(incident_id, delivery_failed=False) is True
    assert inc.get_incident(incident_id)["failure_type"] == "script"


def test_reclassify_sets_delivery_only_from_the_outcome(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    unknown_id, _ = inc.upsert_incident("job-1", "something completely unexpected happened")
    assert inc.get_incident(unknown_id)["failure_type"] == "unknown"
    assert inc.reclassify_incident_for_delivery(unknown_id, delivery_failed=True) is True
    assert inc.get_incident(unknown_id)["failure_type"] == "delivery"

    # A root-cause bucket beats the ping outcome: the run died of a 429, and the send
    # failing afterwards must not hide that behind ``delivery``.
    rate_id, _ = inc.upsert_incident("job-2", "HTTP 429: rate limit exceeded")
    assert inc.reclassify_incident_for_delivery(rate_id, delivery_failed=True) is False
    assert inc.get_incident(rate_id)["failure_type"] == "rate_limit"

    # Successful send, non-delivery label: nothing to fix, no write.
    assert inc.reclassify_incident_for_delivery(rate_id, delivery_failed=False) is False


def test_reclassify_is_idempotent_and_tolerates_unknown_ids(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    incident_id, _ = inc.upsert_incident("job-1", "boom", delivery_failed=True)
    assert inc.get_incident(incident_id)["failure_type"] == "delivery"
    assert inc.reclassify_incident_for_delivery(incident_id, delivery_failed=True) is False
    assert inc.reclassify_incident_for_delivery("nope_missing", delivery_failed=True) is False
    assert inc.reclassify_incident_for_delivery("", delivery_failed=True) is False


# ── 3b: a deleted job's incidents close with it ──────────────────────────────


def test_close_incidents_for_deleted_job_closes_only_open_rows(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    detected, _ = inc.upsert_incident("job-gone", "first failure")
    alerted, _ = inc.upsert_incident("job-gone", "second failure")
    inc.set_incident_state(alerted, "alerted")
    resolved, _ = inc.upsert_incident("job-gone", "third failure")
    inc.set_incident_state(resolved, "resolved")
    acked, _ = inc.upsert_incident("job-gone", "fourth failure")
    inc.ack_incident(acked)
    other, _ = inc.upsert_incident("job-alive", "same first failure")

    assert inc.close_incidents_for_deleted_job("job-gone") == 2

    assert inc.get_incident(detected)["state"] == "closed"
    assert inc.get_incident(alerted)["state"] == "closed"
    assert inc.get_incident(detected)["acked_at"]
    # resolved stays resolved (re-openable if the id ever comes back), acked stays closed.
    assert inc.get_incident(resolved)["state"] == "resolved"
    assert inc.get_incident(acked)["state"] == "closed"
    # Another job's incidents are untouched; a repeat call is a no-op.
    assert inc.get_incident(other)["state"] == "detected"
    assert inc.close_incidents_for_deleted_job("job-gone") == 0
    assert inc.close_incidents_for_deleted_job("") == 0
