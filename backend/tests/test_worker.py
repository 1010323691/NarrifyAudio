from backend.worker import PARSE_WORKER_MAX, parse_worker_slot_count


def test_parse_worker_slots_double_llm_concurrency():
    assert parse_worker_slot_count(1) == 2
    assert parse_worker_slot_count(4) == 8
    assert parse_worker_slot_count(16) == 32


def test_parse_worker_slots_respect_hard_maximum():
    assert parse_worker_slot_count(32) == PARSE_WORKER_MAX == 64
