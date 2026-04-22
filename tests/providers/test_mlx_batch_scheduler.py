from __future__ import annotations

from tests.providers.local_provider_test_helpers import _make_scheduler


def test_iter_batch_responses_flattens_nested_containers():
    scheduler = _make_scheduler()
    r1 = {"uid": 1, "token": 10, "finish_reason": None}
    r2 = {"uid": 2, "token": 11, "finish_reason": None}
    r3 = {"uid": 3, "token": 12, "finish_reason": "stop"}
    nested = [[r1, [r2]], (r3,)]

    flattened = list(scheduler._iter_batch_responses(nested))

    assert flattened == [r1, r2, r3]


def test_response_field_helpers_support_dict_payloads():
    scheduler = _make_scheduler()
    resp = {"uid": 7, "token": "42", "finish_reason": "stop"}

    assert scheduler._response_uid(resp) == 7
    assert scheduler._response_token(resp) == 42
    assert scheduler._response_finish_reason(resp) == "stop"


def test_batch_generator_pending_probe_is_compatible_with_missing_field():
    scheduler = _make_scheduler()

    class FakeBatchGenerator:
        pass

    scheduler._batch_generator = FakeBatchGenerator()

    assert scheduler._batch_generator_has_unprocessed_prompts() is False


def test_batch_generator_pending_probe_reads_unprocessed_prompts_field():
    scheduler = _make_scheduler()

    class FakeBatchGenerator:
        def __init__(self):
            self.unprocessed_prompts = [1]

    scheduler._batch_generator = FakeBatchGenerator()

    assert scheduler._batch_generator_has_unprocessed_prompts() is True


def test_extract_generation_responses_prefers_generation_part_for_tuple():
    scheduler = _make_scheduler()
    prompt_responses = [{"uid": 1, "token": None, "finish_reason": None}]
    generation_responses = [{"uid": 2, "token": 7, "finish_reason": None}]

    extracted = scheduler._extract_generation_responses((prompt_responses, generation_responses))

    assert extracted == generation_responses


def test_extract_generation_responses_keeps_non_tuple_payload():
    scheduler = _make_scheduler()
    payload = [{"uid": 2, "token": 7, "finish_reason": None}]

    extracted = scheduler._extract_generation_responses(payload)

    assert extracted == payload


def test_mlx_batch_scheduler_response_helpers_with_object_attrs():
    class FakeResponse:
        uid = 99
        token = 42
        finish_reason = "stop"

    scheduler = _make_scheduler()
    resp = FakeResponse()
    assert scheduler._response_uid(resp) == 99
    assert scheduler._response_token(resp) == 42
    assert scheduler._response_finish_reason(resp) == "stop"


def test_mlx_batch_scheduler_iter_handles_none():
    scheduler = _make_scheduler()
    assert list(scheduler._iter_batch_responses(None)) == []
