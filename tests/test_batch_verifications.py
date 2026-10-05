"""End-to-end tests for the batch verification endpoint (POST /verifications/batch).

Covers: input order + index echo, sequential quota/pool consumption within a
batch, business rejections that stay traceable, in-batch and historical
idempotent replay, whole-batch atomicity on same-key/different-content and on
format errors, and concurrency against single verifications and cancellations.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Store

START = "2026-06-01T20:00:00Z"
WINDOW = {
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 100,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp


def create_pool(client, film="film-1", cap=10):
    resp = client.post("/pools", json={"film_id": film, "max_sessions": cap})
    assert resp.status_code == 201, resp.text
    return resp.json()


def item(key, start=START, film="film-1", venue="hall-a"):
    return {
        "film_id": film,
        "venue_id": venue,
        "start_time": start,
        "idempotency_key": key,
    }


def batch_verify(client, items):
    return client.post("/verifications/batch", json={"items": items})


# ----------------------------------------------------------------------
# request validation
# ----------------------------------------------------------------------
def test_batch_rejects_empty_list(client):
    resp = batch_verify(client, [])
    assert resp.status_code == 422


def test_batch_rejects_more_than_100_items(client):
    make_license(client)
    resp = batch_verify(client, [item(f"k-{i:03d}") for i in range(101)])
    assert resp.status_code == 422


def test_batch_accepts_exactly_100_items_and_echoes_indexes(client):
    make_license(client, max_sessions=100)
    resp = batch_verify(client, [item(f"k-{i:03d}") for i in range(100)])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["count"] == 100
    assert len(data["results"]) == 100
    assert [r["index"] for r in results_sorted(data)] == list(range(100))
    assert all(r["decision"] == "approved" for r in data["results"])


def test_batch_invalid_item_shape_is_422_and_writes_nothing(client):
    make_license(client, max_sessions=10)
    bad_items = [
        item("ok-1"),
        {"film_id": "film-1", "venue_id": "hall-a",
         "start_time": "not-a-time", "idempotency_key": "bad-time"},
    ]
    resp = batch_verify(client, bad_items)
    assert resp.status_code == 422
    # the error is attributed to the offending array position
    locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
    assert any("items" in loc and 1 in loc for loc in locs)
    assert client.get("/verifications").json() == []

    resp = batch_verify(client, [{"film_id": "f", "venue_id": "v",
                                  "start_time": START}])  # missing key
    assert resp.status_code == 422
    assert client.get("/verifications").json() == []


def test_single_verification_endpoint_still_works_alongside_batch(client):
    make_license(client, max_sessions=5)
    resp = client.post(
        "/verifications",
        json=item("single-1"),
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "approved"
    assert "index" not in resp.json()


# ----------------------------------------------------------------------
# ordering, sequential consumption and traceable rejections
# ----------------------------------------------------------------------
def test_batch_results_follow_input_order(client):
    early = make_license(client, valid_to="2026-09-01T00:00:00Z",
                         max_sessions=1).json()
    late = make_license(client, valid_to="2027-06-01T00:00:00Z",
                        max_sessions=5).json()
    resp = batch_verify(client, [item("o-1"), item("o-2"), item("o-3")])
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["idempotency_key"] for r in results] == ["o-1", "o-2", "o-3"]
    assert [r["index"] for r in results] == [0, 1, 2]
    # earliest-expiring license consumed by the first item, then fallback
    assert results[0]["license_id"] == early["id"]
    assert results[1]["license_id"] == late["id"]
    assert results[2]["license_id"] == late["id"]


def test_batch_quota_consumed_sequentially_and_rejection_continues(client):
    make_license(client, max_sessions=2)
    resp = batch_verify(client, [item("s-1"), item("s-2"), item("s-3")])
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["decision"] for r in results] == [
        "approved", "approved", "rejected"]
    assert results[0]["remaining_quota"] == 1
    assert results[1]["remaining_quota"] == 0
    assert results[2]["reject_reason"] == "QUOTA_EXHAUSTED"

    # every item — including the rejection — is an independently traceable flow
    flows = client.get("/verifications").json()
    assert {f["idempotency_key"] for f in flows} == {"s-1", "s-2", "s-3"}
    rejected = client.get("/verifications",
                          params={"decision": "rejected"}).json()
    assert len(rejected) == 1 and rejected[0]["idempotency_key"] == "s-3"

    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 2
    assert lic["remaining_sessions"] == 0


def test_batch_consumes_shared_pool_sequentially(client):
    pool = create_pool(client, cap=2)
    make_license(client, pool_id=pool["id"], max_sessions=10)
    resp = batch_verify(client, [item("p-1"), item("p-2"), item("p-3")])
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["decision"] for r in results] == [
        "approved", "approved", "rejected"]
    assert results[0]["pool_id"] == pool["id"]
    assert results[0]["pool_remaining_quota"] == 1
    assert results[1]["pool_remaining_quota"] == 0
    assert results[2]["reject_reason"] == "POOL_EXHAUSTED"
    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 2
    assert pool_after["remaining_sessions"] == 0


def test_batch_rejected_item_does_not_consume_quota_for_later_item(client):
    make_license(client, max_sessions=1)
    resp = batch_verify(client, [
        item("m-1", film="other", venue="nowhere"),  # NO_LICENSE, no quota used
        item("m-2"),                                 # gets the only quota
        item("m-3"),                                 # QUOTA_EXHAUSTED
    ])
    results = resp.json()["results"]
    assert [r["reject_reason"] for r in results] == [
        "NO_LICENSE", None, "QUOTA_EXHAUSTED"]


# ----------------------------------------------------------------------
# idempotency inside and across batches
# ----------------------------------------------------------------------
def test_batch_same_key_same_content_within_batch_replays_without_double_deduction(client):
    make_license(client, max_sessions=1)
    resp = batch_verify(client, [
        item("dup-1"), item("dup-1"), item("dup-2"),
    ])
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results[0]["replay"] is False
    assert results[1]["replay"] is True
    assert results[1]["id"] == results[0]["id"]
    # the replay must not consume quota: the third item exhausts exactly 1
    assert results[2]["decision"] == "rejected"
    assert results[2]["reject_reason"] == "QUOTA_EXHAUSTED"
    assert len(client.get("/verifications").json()) == 2


def test_batch_same_key_different_content_within_batch_fails_entire_batch(client):
    make_license(client, max_sessions=5)
    resp = batch_verify(client, [
        item("c-1"),
        item("c-2"),
        item("c-2", start="2026-06-03T20:00:00Z"),  # same key, changed slot
        item("c-3"),
    ])
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["index"] == 2
    assert body["existing"]["idempotency_key"] == "c-2"
    # whole batch rolled back: no flows, no quota occupation
    assert client.get("/verifications").json() == []
    assert client.get("/licenses").json()[0]["used_sessions"] == 0


def test_batch_replays_historical_flows_without_deduction(client):
    make_license(client, max_sessions=1)
    first = client.post("/verifications", json=item("h-1")).json()

    resp = batch_verify(client, [
        item("h-1"),                                  # historical replay
        item("h-2"),                                  # must be rejected: quota used
    ])
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["replay"] is True
    assert results[0]["id"] == first["id"]
    assert results[0]["decision"] == "approved"
    assert results[1]["decision"] == "rejected"
    assert results[1]["reject_reason"] == "QUOTA_EXHAUSTED"


def test_batch_historical_key_with_changed_payload_fails_atomically(client):
    make_license(client, max_sessions=5)
    client.post("/verifications", json=item("h-9")).json()

    resp = batch_verify(client, [
        item("h-8"),                                          # would approve
        item("h-9", start="2026-06-09T20:00:00Z"),            # key reused, changed
        item("h-7"),                                          # would approve
    ])
    assert resp.status_code == 409
    body = resp.json()
    assert body["index"] == 1
    assert body["existing"]["idempotency_key"] == "h-9"

    keys = {f["idempotency_key"]
            for f in client.get("/verifications").json()}
    assert keys == {"h-9"}  # only the pre-existing flow survives
    assert client.get("/licenses").json()[0]["used_sessions"] == 1


def test_batch_replay_is_indifferent_to_later_suspension(client):
    lic = make_license(client, max_sessions=5)
    batch_verify(client, [item("sus-1")])
    client.post(f"/licenses/{lic.json()['id']}/suspensions", json={
        "suspend_from": "2026-05-31T00:00:00Z",
        "suspend_to": "2026-06-30T00:00:00Z",
        "reason": "临时暂停",
    })
    # a fresh item is blocked by the suspension, the historical item replays
    resp = batch_verify(client, [item("sus-2"), item("sus-1")])
    results = resp.json()["results"]
    assert results[0]["reject_reason"] == "LICENSE_SUSPENDED"
    assert results[1]["replay"] is True
    assert results[1]["decision"] == "approved"


# ----------------------------------------------------------------------
# interplay with single verifications and cancellations
# ----------------------------------------------------------------------
def test_batch_sees_quota_consumed_by_single_verification(client):
    make_license(client, max_sessions=2)
    assert client.post("/verifications", json=item("one-1")).status_code == 200
    resp = batch_verify(client, [item("one-2"), item("one-3")])
    results = resp.json()["results"]
    assert results[0]["decision"] == "approved"
    assert results[1]["decision"] == "rejected"
    assert results[1]["reject_reason"] == "QUOTA_EXHAUSTED"


def test_single_verification_sees_quota_consumed_by_batch(client):
    make_license(client, max_sessions=1)
    batch_verify(client, [item("b-1")])
    resp = client.post("/verifications", json=item("b-2"))
    assert resp.json()["decision"] == "rejected"
    assert resp.json()["reject_reason"] == "QUOTA_EXHAUSTED"


def test_cancelled_batch_session_releases_quota_for_later_batch(client):
    make_license(client, max_sessions=1)
    batch_verify(client, [item("cancel-1", start="2026-12-01T20:00:00Z")])
    # exhausted now
    assert batch_verify(client, [item("cancel-2",
                                      start="2026-12-02T20:00:00Z")]).json()[
        "results"][0]["reject_reason"] == "QUOTA_EXHAUSTED"

    resp = client.post("/verifications/cancel-1/cancel",
                       json={"reason": "设备故障"})
    assert resp.status_code == 200
    # freed quota is reusable by the next batch
    again = batch_verify(client, [item("cancel-3",
                                       start="2026-12-03T20:00:00Z")])
    assert again.json()["results"][0]["decision"] == "approved"


# ----------------------------------------------------------------------
# concurrency: batches and single verifications must never oversell
# ----------------------------------------------------------------------
def test_concurrent_batches_and_single_verifications_never_exceed_quota(tmp_path):
    store = Store(str(tmp_path / "concurrent.db"))
    store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        max_sessions=5,
    )
    start = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
    key_counter = {"n": 0}
    counter_lock = threading.Lock()

    def next_key() -> str:
        with counter_lock:
            key_counter["n"] += 1
            return f"ck-{key_counter['n']}"

    approved, rejected, errors = [], [], []
    result_lock = threading.Lock()

    def batch_worker() -> None:
        keys = []
        with counter_lock:
            for _ in range(2):
                key_counter["n"] += 1
                keys.append(f"ck-{key_counter['n']}")
        try:
            results = store.verify_batch([
                {"film_id": "film-x", "venue_id": "hall-1",
                 "start_time": start, "idempotency_key": k}
                for k in keys
            ])
            with result_lock:
                for r in results:
                    (approved if r["decision"] == "approved"
                     else rejected).append(r)
        except Exception as exc:  # pragma: no cover - failure path
            with result_lock:
                errors.append(exc)

    def single_worker() -> None:
        key = next_key()
        try:
            r = store.verify(film_id="film-x", venue_id="hall-1",
                             start_time=start, idempotency_key=key)
            with result_lock:
                (approved if r["decision"] == "approved"
                 else rejected).append(r)
        except Exception as exc:  # pragma: no cover - failure path
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=batch_worker) for _ in range(8)]
    threads += [threading.Thread(target=single_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(approved) == 5
    assert len(approved) + len(rejected) == 20  # 8*2 + 4 attempts
    assert all(r["reject_reason"] == "QUOTA_EXHAUSTED" for r in rejected)
    lic = store.list_licenses()[0]
    assert lic["used_sessions"] == 5
    assert lic["remaining_sessions"] == 0


# ----------------------------------------------------------------------
# concurrency: a batch must never interleave with a cancellation
# ----------------------------------------------------------------------
def test_concurrent_batch_and_cancellation_keep_consistent_state(tmp_path):
    store = Store(str(tmp_path / "concurrent-cancel.db"))
    store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        max_sessions=3,
    )
    start = datetime(2026, 12, 1, 20, 0, tzinfo=timezone.utc)
    store.verify_batch([
        {"film_id": "film-x", "venue_id": "hall-1",
         "start_time": start, "idempotency_key": f"seed-{i}"}
        for i in range(3)
    ])
    assert store.list_licenses()[0]["used_sessions"] == 3

    errors: list = []

    def batch_worker() -> None:
        # three contenders for whatever quota the cancellation frees
        try:
            store.verify_batch([
                {"film_id": "film-x", "venue_id": "hall-1",
                 "start_time": start + timedelta(days=1),
                 "idempotency_key": f"new-{i}"}
                for i in range(3)
            ])
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def cancel_worker() -> None:
        try:
            store.cancel_verification("seed-1", reason="设备故障")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    t1 = threading.Thread(target=batch_worker)
    t2 = threading.Thread(target=cancel_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors

    # The derived counters must always equal the number of active approved
    # flows; the cap of 3 may never be crossed regardless of commit order.
    active_approved = [
        v for v in store.list_verifications(limit=1000)
        if v["decision"] == "approved" and not v["cancelled"]
    ]
    lic = store.list_licenses()[0]
    assert lic["used_sessions"] == len(active_approved)
    assert lic["used_sessions"] <= 3


def results_sorted(payload):
    """Results are already ordered; helper kept explicit for index assertions."""
    return sorted(payload["results"], key=lambda r: r["index"])
