from chatty.timing import (
    RequestTimings,
    build_split,
    format_duration,
    format_elapsed,
    format_footer,
    format_one_liner,
    format_timing_line,
    format_timing_report,
)


def make_record(
    *,
    usage=None,
    submitted=0.0,
    dispatched=0.002,
    headers=1.052,
    reasoning=None,
    first_content=1.692,
    last_content=9.792,
    finished=14.0,
    clock="14:32:05",
):
    record = RequestTimings()
    record.clock = clock
    record.submitted_at = submitted
    record.dispatched_at = dispatched
    record.headers_at = headers
    record.first_reasoning_at = reasoning
    record.first_content_at = first_content
    record.last_content_at = last_content
    record.finished_at = finished
    record.usage = usage
    return record


# ── Duration formatting ────────────────────────────────────────────────────


def test_format_duration_units():
    assert format_duration(None) == "n/a"
    assert format_duration(0.0004) == "<1ms"
    assert format_duration(0.002) == "2ms"
    assert format_duration(0.64) == "640ms"
    assert format_duration(1.05) == "1.05s"
    assert format_duration(61.5) == "1m 2s"
    assert format_duration(3725) == "1h 2m"


def test_format_elapsed_units():
    assert format_elapsed(None) == "n/a"
    assert format_elapsed(14.0) == "14s"
    assert format_elapsed(60) == "1m"
    assert format_elapsed(90) == "1m 30s"


# ── Token split cases ─────────────────────────────────────────────────────


def test_split_exact_reasoning_counts():
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "completion_tokens_details": {"reasoning_tokens": 5},
    }
    split = build_split(usage, None)
    assert (split.prompt, split.completion, split.output, split.reasoning) == (10, 20, 15, 5)
    assert split.reasoning_estimated is False
    assert split.reasoning_included is True


def test_split_estimates_reasoning_from_streamed_text():
    split = build_split({"prompt_tokens": 10, "completion_tokens": 20}, 7)
    assert split.reasoning == 7
    assert split.reasoning_estimated is True
    assert split.output == 13
    assert split.reasoning_included is True


def test_split_detects_completion_excluding_reasoning():
    # More reasoning than completion proves the server reports them separately.
    split = build_split({"prompt_tokens": 10, "completion_tokens": 20}, 25)
    assert split.reasoning == 25
    assert split.output == 20
    assert split.reasoning_included is False


def test_split_without_reasoning_or_usage():
    split = build_split({"prompt_tokens": 10, "completion_tokens": 20}, None)
    assert (split.prompt, split.completion, split.reasoning, split.output) == (10, 20, None, None)
    assert build_split(None, 12).completion is None


def test_split_never_rewrites_reported_completion():
    usage = {"completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 5}}
    assert build_split(usage, 99).completion == 20


# ── Capture ───────────────────────────────────────────────────────────────


def test_observe_records_stages_and_usage():
    record = RequestTimings()
    record.mark_dispatched()
    record.mark_headers({"openai-processing-ms": "250"})
    record.observe("reasoning", "hm")
    record.observe("delta", "Hello")
    record.observe("delta", " world")
    record.observe("usage", '{"prompt_tokens": 2, "completion_tokens": 3}')
    record.finish()

    assert record.first_reasoning_at is not None
    assert record.reasoning_chars == 2
    assert record.usage["completion_tokens"] == 3
    assert record.server_prompt_seconds == 0.25
    assert record.first_token_at == record.first_reasoning_at
    assert record.thinking_duration is not None
    assert record.finished_at is not None


def test_observe_reads_provider_timings_block():
    record = RequestTimings()
    record.observe("timings", '{"prompt_ms": 800, "predicted_ms": 2000}')
    assert record.server_timings["predicted_ms"] == 2000
    assert record.server_prompt_seconds == 0.8


def test_observe_ignores_malformed_payloads():
    record = RequestTimings()
    record.observe("usage", "not json")
    record.observe("timings", "not json")
    assert record.usage is None
    assert record.server_timings == {}


def test_decode_rate_excludes_prompt_and_render():
    record = make_record(usage={"completion_tokens": 810, "prompt_tokens": 420})
    # Decode spans first token to last token (8.1s), not submit to finish (14s).
    assert round(record.decode_rate, 1) == 100.0


def test_decode_rate_is_absent_without_a_measurable_span():
    record = make_record(usage={"completion_tokens": 10}, first_content=2.0, last_content=2.0)
    assert record.decode_rate is None


# ── Timing line ───────────────────────────────────────────────────────────


def test_timing_line_lists_exclusive_stages():
    line = format_timing_line(make_record())
    assert line == ("14:32:05 app 2ms → net 1.05s → prompt 640ms → ttft 1.69s → output 8.10s → total 14.00s")


def test_timing_line_includes_thinking_when_streamed():
    line = format_timing_line(make_record(reasoning=1.692, first_content=5.892, last_content=9.792))
    assert "prompt 640ms → ttft 1.69s → thinking 4.20s → output 3.90s" in line


def test_timing_line_labels_buffered_responses():
    # Headers arriving with the first token hide server-side waiting.
    line = format_timing_line(make_record(headers=1.692))
    assert "net/queue" in line
    assert "prompt n/a" in line


def test_timing_line_marks_silent_reasoning():
    usage = {"completion_tokens_details": {"reasoning_tokens": 120}}
    line = format_timing_line(make_record(usage=usage))
    assert "prompt+thinking (est.) 1.69s" in line
    assert "→ thinking " not in line


def test_timing_line_flags_interrupted_and_errors():
    record = make_record()
    record.interrupted = True
    record.error = "HTTP 500"
    line = format_timing_line(record)
    assert line.endswith("· interrupted · error")


def test_timing_line_reports_unmeasured_stages_as_na():
    record = RequestTimings()
    line = format_timing_line(record)
    assert "net n/a" in line
    assert "ttft n/a" in line
    assert "total n/a" in line


# ── Footer and compact status line ────────────────────────────────────────


def test_footer_includes_cost_and_cache_hits():
    usage = {
        "prompt_tokens": 420,
        "completion_tokens": 1220,
        "completion_tokens_details": {"reasoning_tokens": 150},
        "prompt_tokens_details": {"cached_tokens": 300},
        "cost": 0.0042,
    }
    footer = format_footer(make_record(usage=usage))
    assert footer == "out 1,220 · reasoning 150 (out incl.) · cached 300 (71% hit) · 150.6 tok/s · $0.0042"


def test_footer_marks_estimated_reasoning():
    usage = {"prompt_tokens": 420, "completion_tokens": 1220}
    record = make_record(usage=usage)
    record.reasoning_chars = 600  # ~150 tokens
    footer = format_footer(record)
    assert "reasoning ~150 (out incl.)" in footer


def test_footer_is_absent_without_usage():
    assert format_footer(RequestTimings()) is None


def test_status_line_shows_clock_and_split():
    usage = {
        "prompt_tokens": 420,
        "completion_tokens": 1220,
        "total_tokens": 1640,
        "completion_tokens_details": {"reasoning_tokens": 150},
    }
    line = format_one_liner(make_record(usage=usage))
    assert line == "[14:32:05 tokens: 1,640 (420 in + 1,220 out: 150 rea + 1,070 mes), 150.6 tok/s, 14s]"


def test_status_line_omits_split_when_not_applicable():
    line = format_one_liner(make_record(usage={"prompt_tokens": 10, "completion_tokens": 5}))
    assert "out:" not in line
    assert "(10 in + 5 out)" in line


def test_status_line_marks_estimated_reasoning():
    record = make_record(usage={"prompt_tokens": 10, "completion_tokens": 20})
    record.reasoning_chars = 40  # ~10 tokens
    assert "~10 rea" in format_one_liner(record)


def test_report_is_timing_line_plus_optional_footer():
    assert len(format_timing_report(make_record())) == 1
    full = format_timing_report(make_record(usage={"prompt_tokens": 10, "completion_tokens": 20}))
    assert len(full) == 2
