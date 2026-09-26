"""Tests for ghost skill detection and prevention in kanban_db."""
import json
import sqlite3
from unittest.mock import patch

from hermes_cli.kanban_db import (
    clear_invalid_skills_for_assignee,
    validate_skill_for_profile,
    get_profile_skill_path,
)


def test_get_profile_skill_path_returns_none_for_missing():
    """get_profile_skill_path returns None when the skill does not exist."""
    assert get_profile_skill_path("kensho-worker", "nonexistent-skill-xyz") is None


def test_validate_skill_for_profile_false_for_missing():
    """validate_skill_for_profile returns False for a non-existent skill."""
    assert validate_skill_for_profile("nonexistent-skill-xyz", "kensho-worker") is False


def test_clear_invalid_skills_for_assignee_removes_ghosts():
    """clear_invalid_skills_for_assignee removes skills not in the assignee's profile."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            assignee TEXT,
            status TEXT,
            priority INTEGER,
            created_by TEXT,
            created_at INTEGER,
            workspace_kind TEXT,
            workspace_path TEXT,
            branch_name TEXT,
            project_id TEXT,
            tenant TEXT,
            idempotency_key TEXT,
            max_runtime_seconds INTEGER,
            skills TEXT,
            max_retries INTEGER,
            model_override TEXT,
            provider_override TEXT,
            reasoning_effort TEXT,
            goal_mode INTEGER,
            goal_max_turns INTEGER,
            session_id TEXT,
            claim_lock TEXT,
            claim_expires INTEGER,
            consecutive_failures INTEGER,
            last_failure_error TEXT
        )
    """)

    task_id = "test_task"
    assignee = "test_profile"
    skills_list = ["valid_skill", "another_skill", "invalid_skill", "also_invalid"]
    conn.execute("""
        INSERT INTO tasks (
            id, title, body, assignee, status, priority, created_by, created_at,
            workspace_kind, workspace_path, branch_name, project_id, tenant,
            idempotency_key, max_runtime_seconds, skills, max_retries,
            model_override, provider_override, reasoning_effort, goal_mode,
            goal_max_turns, session_id, claim_lock, claim_expires,
            consecutive_failures, last_failure_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task_id, "Test Task", "Test Body", assignee, "ready", 0, "test", 0,
        "scratch", None, None, None, None, None, None,
        json.dumps(skills_list), 0, None, None, None, 0, None, None, None, None, None, None
    ))
    conn.commit()

    # Mock get_profile_skill_path to return valid paths only for known skills
    def mock_get_profile_skill_path(profile, skill_name):
        valid_skills = {"valid_skill", "another_skill"}
        if skill_name in valid_skills:
            return f"/fake/path/{skill_name}/SKILL.md"
        return None

    with patch("hermes_cli.kanban_db.get_profile_skill_path", side_effect=mock_get_profile_skill_path):
        removed = clear_invalid_skills_for_assignee(conn, task_id, assignee)

    assert set(removed) == {"invalid_skill", "also_invalid"}

    row = conn.execute("SELECT skills FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    current_skills = json.loads(row["skills"]) if row["skills"] else []
    assert set(current_skills) == {"valid_skill", "another_skill"}

    # All valid skills: nothing removed
    conn.execute("UPDATE tasks SET skills = ? WHERE id = ?",
                 (json.dumps(["valid_skill", "another_skill"]), task_id))
    conn.commit()
    with patch("hermes_cli.kanban_db.get_profile_skill_path", side_effect=mock_get_profile_skill_path):
        removed = clear_invalid_skills_for_assignee(conn, task_id, assignee)
    assert removed == []

    # No skills: nothing removed
    conn.execute("UPDATE tasks SET skills = ? WHERE id = ?", (None, task_id))
    conn.commit()
    with patch("hermes_cli.kanban_db.get_profile_skill_path", side_effect=mock_get_profile_skill_path):
        removed = clear_invalid_skills_for_assignee(conn, task_id, assignee)
    assert removed == []

    # Empty skills list: nothing removed
    conn.execute("UPDATE tasks SET skills = ? WHERE id = ?",
                 (json.dumps([]), task_id))
    conn.commit()
    with patch("hermes_cli.kanban_db.get_profile_skill_path", side_effect=mock_get_profile_skill_path):
        removed = clear_invalid_skills_for_assignee(conn, task_id, assignee)
    assert removed == []


if __name__ == "__main__":
    test_get_profile_skill_path_returns_none_for_missing()
    test_validate_skill_for_profile_false_for_missing()
    test_clear_invalid_skills_for_assignee_removes_ghosts()
    print("All tests passed!")