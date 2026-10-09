"""DL-ROUTE-01/02: node capability profiles and the advisory prompt-size check (no router import)."""

import json
import threading

import pytest

from davellm_node_profiles import (
    NodeActivity, NodeProfile, estimate_prompt_tokens, parse_node_profiles, prompt_size_check,
    prompt_size_estimate, reads_only_new_message,
)


def nodes(*entries):
    return json.dumps([{"id": f"n{i}", "name": f"N{i}", "url": "http://n.test:11434", **entry}
                       for i, entry in enumerate(entries)])


def test_profiles_parse_by_node_id_and_nodes_without_one_are_left_out():
    profiles = parse_node_profiles(nodes(
        {"profile": {"compute": "cpu", "prompt_token_limit": 1300}},
        {},
        {"profile": {"compute": "gpu", "model_prompt_token_limits": {"gpt-oss:120b": 2500}}},
        {"profile": None},
    ))
    assert set(profiles) == {"n0", "n2"}
    assert profiles["n0"] == NodeProfile(compute="cpu", prompt_token_limit=1300)
    assert profiles["n2"].model_prompt_token_limits == {"gpt-oss:120b": 2500}


def test_a_bad_profile_is_dropped_with_a_warning_and_the_rest_are_kept():
    warnings = []
    profiles = parse_node_profiles(nodes(
        {"profile": {"compute": "tpu"}},
        {"profile": {"prompt_token_limit": 0}},
        {"profile": {"prompt_token_limt": 1300}},
        {"profile": {"model_prompt_token_limits": {"m": -1}}},
        {"profile": "fast"},
        {"profile": {"compute": "gpu"}},
    ), warn=warnings.append)
    assert set(profiles) == {"n5"}
    assert len(warnings) == 5
    assert all(w.startswith("⚠️ Ignoring the profile for node 'n") for w in warnings)
    assert "compute" in warnings[0] and "prompt_token_limt" in warnings[2]


def test_unparseable_or_missing_node_lists_give_no_profiles():
    for raw in (None, "", "not json", "{}", '"x"', json.dumps([["n0"]]), json.dumps([{"profile": {}}])):
        assert parse_node_profiles(raw, warn=lambda _: None) == {}


def test_a_model_limit_overrides_the_node_limit_and_latest_tags_match():
    profile = NodeProfile(prompt_token_limit=4000, model_prompt_token_limits={"gpt-oss:120b": 2500, "llama3": 900})
    assert profile.limit_for("gpt-oss:120b") == 2500
    assert profile.limit_for("gpt-oss:20b") == 4000
    assert profile.limit_for("llama3:latest") == 900
    assert profile.limit_for("llama3:8b") == 4000
    assert NodeProfile(model_prompt_token_limits={"qwen3:latest": 700}).limit_for("qwen3") == 700
    assert NodeProfile(compute="gpu").limit_for("anything") is None


def test_the_prompt_check_reports_only_prompts_over_the_limit():
    cpu = NodeProfile(compute="cpu", prompt_token_limit=1300)
    assert prompt_size_check(cpu, "llama3", 1301) == {"prompt_tokens": 1301, "prompt_token_limit": 1300}
    assert prompt_size_check(cpu, "llama3", 1300) is None
    assert prompt_size_check(None, "llama3", 99_999) is None
    assert prompt_size_check(NodeProfile(compute="gpu"), "llama3", 99_999) is None


def test_the_pre_send_estimate_always_reports_and_shares_the_limit_rule():
    duncan = NodeProfile(compute="gpu", model_prompt_token_limits={"gpt-oss:120b": 2500})
    assert prompt_size_estimate(duncan, "gpt-oss:120b", 13_520) == {
        "prompt_tokens": 13_520, "prompt_token_limit": 2500, "over_limit": True,
    }
    assert prompt_size_estimate(duncan, "gpt-oss:120b", 2500)["over_limit"] is False
    assert prompt_size_estimate(duncan, "llama3", 99_999) == {
        "prompt_tokens": 99_999, "prompt_token_limit": None, "over_limit": False,
    }
    assert prompt_size_estimate(None, "llama3", 5)["prompt_token_limit"] is None
    for tokens in (1, 2500, 2501, 50_000):
        over = prompt_size_estimate(duncan, "gpt-oss:120b", tokens)["over_limit"]
        assert over is (prompt_size_check(duncan, "gpt-oss:120b", tokens) is not None)


def test_only_a_cached_loaded_idle_node_reads_just_the_new_message():
    assert reads_only_new_message(True, True, 0) is True
    assert reads_only_new_message(False, True, 0) is False
    assert reads_only_new_message(True, None, 0) is False  # residency unknown
    assert reads_only_new_message(True, False, 0) is False
    assert reads_only_new_message(True, True, 1) is False


def test_prompt_estimate_counts_every_message_text():
    messages = [
        {"role": "system", "content": "s" * 400},
        {"role": "user", "content": "u" * 40, "images": ["aGk="]},
        {"role": "assistant", "content": None},
    ]
    assert estimate_prompt_tokens(messages, lambda text: len(text) // 4) == 110


def test_node_activity_counts_others_in_flight_and_releases_on_errors():
    activity = NodeActivity()
    assert activity.start("a") == 0
    assert activity.start("a") == 1
    assert activity.start("b") == 0
    activity.finish("a")
    assert activity.in_flight("a") == 1
    with pytest.raises(RuntimeError):
        with activity.track("a") as ahead:
            assert ahead == 1 and activity.in_flight("a") == 2
            raise RuntimeError("node failed")
    assert activity.in_flight("a") == 1
    activity.finish("a")
    activity.finish("a")  # an extra finish never goes negative
    assert activity.in_flight("a") == 0 and activity.in_flight("b") == 1


def test_node_activity_is_consistent_across_threads():
    activity = NodeActivity()

    def work():
        for _ in range(500):
            with activity.track("n"):
                pass

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert activity.in_flight("n") == 0


def test_node_activity_remembers_the_prompt_each_node_last_started():
    activity = NodeActivity()
    assert activity.last_prompt("a") is None
    with activity.track("a", "llama3\nc1"):
        pass
    assert activity.last_prompt("a") == "llama3\nc1"
    with activity.track("a"):  # a summary or tool run leaves nothing reusable
        pass
    assert activity.last_prompt("a") is None and activity.last_prompt("b") is None

