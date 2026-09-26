"""Goal-mode completion must not depend on a reachable judge (t_26812b2a).

Two ways an unusable goal judge used to wedge a ``goal_mode`` kanban card:

1. **Completion gate** — ``kanban.py`` / ``tools/kanban_tools.py`` called
   ``judge_goal`` and discarded its ``transport_failed`` flag. A judge whose
   provider answered HTTP 400 (dead provider / zero balance, the bai case)
   returns ``("continue", "judge error: BadRequestError", False, None, True)``
   — so every ``kanban_complete`` was rejected as "premature", forever. Live
   incident: ``t_fa046d3a`` sat ready/running ~34.5h.

2. **Goal loop** — ``run_kanban_goal_loop`` ignored the same flags and
   treated each unusable verdict as "not done yet", re-poking the worker
   until the whole turn budget was gone.

Both are fail-OPEN now, and the loop blocks the card after N consecutive
unusable verdicts instead of burning the budget.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_cli import goals
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# 1. Goal loop stops after N consecutive unusable judge verdicts
# ---------------------------------------------------------------------------


def _patch_judge(monkeypatch, results):
    """Script judge_goal with full 5-tuples (verdict, reason, parse, wait, transport)."""
    seq = list(results)
    calls = []

    def _fake_judge(goal, response, **_kw):
        calls.append(response)
        if seq:
            return seq.pop(0)
        return "continue", "scripted:fallback", False, None, False

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)
    return calls


_UNUSABLE = ("continue", "judge error: BadRequestError", False, None, True)


def test_loop_blocks_after_consecutive_transport_failures(monkeypatch):
    """The t_fa046d3a shape: every judge call is a transport error.

    The loop must stop and block the card after the ceiling — NOT run the
    full turn budget (which is what turned into a 34.5h stall).
    """
    _patch_judge(monkeypatch, [_UNUSABLE] * 20)
    turns = []
    blocks = []

    res = goals.run_kanban_goal_loop(
        task_id="t_stuck",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "still working",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocks.append(r),
        max_turns=20,
        max_judge_failures=3,
        first_response="first turn done",
    )

    assert res["outcome"] == "blocked_judge_unavailable"
    assert len(blocks) == 1
    assert "judge unusable" in blocks[0] or "judge unusable" in res["reason"]
    # Ceiling hit at the 3rd unusable verdict; at most 2 continuation turns
    # were spent before the loop gave up (vs. 19 under the old behaviour).
    assert res["turns_used"] <= 3
    assert len(turns) <= 2


def test_loop_blocks_after_consecutive_parse_failures(monkeypatch):
    """An unparseable judge (weak model returning prose) is equally unusable."""
    _patch_judge(
        monkeypatch,
        [("continue", "judge reply was not JSON: 'oops'", True, None, False)] * 10,
    )
    turns = []
    blocks = []

    res = goals.run_kanban_goal_loop(
        task_id="t_weak_judge",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "working",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocks.append(r),
        max_turns=20,
        max_judge_failures=2,
        first_response="first",
    )

    assert res["outcome"] == "blocked_judge_unavailable"
    assert len(blocks) == 1
    assert len(turns) <= 1


def test_loop_failure_streak_resets_after_a_usable_verdict(monkeypatch):
    """One transient blip must not count toward the ceiling."""
    _patch_judge(
        monkeypatch,
        [
            _UNUSABLE,
            ("continue", "still not done", False, None, False),
            _UNUSABLE,
            ("done", "complete", False, None, False),
        ],
    )
    turns = []
    blocks = []

    res = goals.run_kanban_goal_loop(
        task_id="t_blip",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "working",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocks.append(r),
        max_turns=10,
        max_judge_failures=2,
        first_response="first",
    )

    # The lone blip did NOT trip the ceiling: the loop ran on to the turn
    # budget instead of blocking on judge unavailability.
    assert res["outcome"] == "blocked_budget"
    assert not any("judge unusable" in (b or "") for b in blocks)
    assert len(turns) >= 5
    # The scripted "done" verdict produced the finalize nudge.
    assert any("looks complete" in p for p in turns)


def test_loop_ceiling_defaults_when_unset(monkeypatch):
    """No explicit ceiling → the config-backed default, not the turn budget."""
    _patch_judge(monkeypatch, [_UNUSABLE] * 30)
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t_default",
        goal_text="g",
        run_turn=lambda p: turns.append(p) or "x",
        task_status_fn=lambda: "running",
        block_fn=lambda r: None,
        max_turns=20,
        first_response="first",
    )

    assert res["outcome"] == "blocked_judge_unavailable"
    assert len(turns) <= goals.DEFAULT_MAX_CONSECUTIVE_JUDGE_FAILURES


def test_judge_failure_ceiling_reads_config(monkeypatch):
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod, "load_config", lambda: {"goals": {"max_consecutive_judge_failures": 7}}
    )
    assert goals._goal_judge_failure_ceiling() == 7

    monkeypatch.setattr(
        config_mod, "load_config", lambda: {"goals": {"max_consecutive_judge_failures": "x"}}
    )
    assert (
        goals._goal_judge_failure_ceiling()
        == goals.DEFAULT_MAX_CONSECUTIVE_JUDGE_FAILURES
    )


# ---------------------------------------------------------------------------
# 2. Completion gate fails OPEN when the judge is unreachable
# ---------------------------------------------------------------------------
# NOTE on the return contract: both ``_goal_mode_handoff_rejection`` implementations return
# ``(verdict, reason_or_None)`` — NOT a bare reason/None. ``("done", None)`` = allow,
# ``("continue", reason)`` = reject, ``("blocked", reason)`` = judged unachievable. The tuple is
# pinned by tests/hermes_cli/test_kanban_goal_judge_affinity.py (upstream) and is what
# ``_goal_gate_error`` / ``_goal_gate`` need to pick the right guidance per verdict.


def _gate_task():
    return SimpleNamespace(
        id="t_gate",
        goal_mode=True,
        title="Finish report",
        body="acceptance: criteria",
    )


def test_cli_gate_allows_handoff_when_judge_unreachable(monkeypatch):
    from hermes_cli import kanban as kb_cli

    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda name: (object(), "judge-model"),
    )
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: ("continue", "judge error: BadRequestError", False, None, True),
    )

    assert kb_cli._goal_mode_handoff_rejection(_gate_task(), "evidence") == ("done", None)


def test_cli_gate_still_rejects_a_real_not_done_verdict(monkeypatch):
    from hermes_cli import kanban as kb_cli

    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda name: (object(), "judge-model"),
    )
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: ("continue", "criteria not met", False, None, False),
    )

    assert kb_cli._goal_mode_handoff_rejection(_gate_task(), "evidence") == (
        "continue", "criteria not met"
    )


def test_tool_gate_allows_handoff_when_judge_unreachable(monkeypatch):
    from tools import kanban_tools

    monkeypatch.setattr(kanban_tools, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(
        kanban_tools, "judge_goal",
        lambda **kw: ("continue", "judge error: TimeoutError", False, None, True),
    )

    assert kanban_tools._goal_mode_handoff_rejection(_gate_task(), "evidence") == ("done", None)


def test_tool_gate_still_rejects_a_real_not_done_verdict(monkeypatch):
    from tools import kanban_tools

    monkeypatch.setattr(kanban_tools, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(
        kanban_tools, "judge_goal",
        lambda **kw: ("continue", "criteria not met", False, None, False),
    )

    assert kanban_tools._goal_mode_handoff_rejection(_gate_task(), "evidence") == (
        "continue", "criteria not met"
    )
    # ...and the gate itself still turns that real verdict into a rejection.
    with pytest.raises(kanban_tools._Reject):
        kanban_tools._goal_gate("kanban_complete", _gate_task(), "task-1", "ev")


# ---------------------------------------------------------------------------
# 3. A completed card never collects a phantom timed_out / retry_status
# ---------------------------------------------------------------------------


def _events(conn, tid):
    return conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()


def test_record_task_failure_ignores_done_task(kanban_home: Path) -> None:
    """t_0b949bda shape: worker completes, THEN its process reports a timeout.

    The card must stay ``done`` with no ``timed_out`` event, no
    ``retry_status`` and no failure counter bump.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already finished")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done")
        assert kb.get_task(conn, tid).status == "done"
        before = len(_events(conn, tid))

        blocked = kb._record_task_failure(
            conn,
            tid,
            error="Iteration budget exhausted (90/90) — task could not complete",
            outcome="timed_out",
            release_claim=True,
            end_run=True,
        )

        assert blocked is False
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert int(task.consecutive_failures or 0) == 0
        after = _events(conn, tid)
        assert len(after) == before
        assert not [e for e in after if e["kind"] in ("timed_out", "gave_up")]


def test_record_task_failure_still_counts_for_open_tasks(kanban_home: Path) -> None:
    """The terminal guard must not swallow the normal failure path."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="in flight")
        kb.claim_task(conn, tid)
        kb._record_task_failure(
            conn, tid, error="elapsed 5s > limit 1s", outcome="timed_out",
            release_claim=True, end_run=True,
        )
        task = kb.get_task(conn, tid)
        assert int(task.consecutive_failures or 0) == 1
        kinds = [e["kind"] for e in _events(conn, tid)]
        assert "timed_out" in kinds


def test_budget_exhausted_reporter_skips_terminal_card(monkeypatch, kanban_home):
    """The worker-side fallback must skip a card that already reached done."""
    import logging

    from agent import turn_finalizer

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already finished")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done")

    record = MagicMock(name="_record_task_failure")
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)

    turn_finalizer._record_kanban_budget_exhausted(
        tid, 90, 90, logging.getLogger("test.goal_judge_fallback")
    )

    record.assert_not_called()
