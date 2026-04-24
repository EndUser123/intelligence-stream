"""Tests for the shared NotebookLM retry pool."""

from csf.shared_retry_pool import claim_ready, enqueue, mark_complete, pending_count, reset_pool, reschedule


def test_enqueue_and_claim_ready_round_trip():
    reset_pool()
    assert enqueue("dQw4w9WgXcQ", retry_count=0, delay_s=0.0, last_error="test") is True
    claimed = claim_ready(limit=5, claimant_id="worker-01")
    assert len(claimed) == 1
    assert claimed[0].video_id == "dQw4w9WgXcQ"
    assert claimed[0].retry_count == 0
    assert claimed[0].status == "claimed"
    assert pending_count() == 0
    assert mark_complete("dQw4w9WgXcQ") is True


def test_reschedule_puts_item_back_into_pending_pool():
    reset_pool()
    assert enqueue("dQw4w9WgXcQ", retry_count=1, delay_s=0.0, last_error="first") is True
    claimed = claim_ready(limit=5, claimant_id="worker-01")
    assert len(claimed) == 1
    assert reschedule("dQw4w9WgXcQ", retry_count=2, delay_s=0.0, last_error="again") is True
    claimed_again = claim_ready(limit=5, claimant_id="worker-02")
    assert len(claimed_again) == 1
    assert claimed_again[0].video_id == "dQw4w9WgXcQ"
    assert claimed_again[0].retry_count == 2
