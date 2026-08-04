"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist for the eligible roster."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
        patch("hermes_cli.kanban_decompose._load_config", return_value={"kanban": {
            "decompose_allowed_assignees": names,
            "orchestrator_profile": names[0] if names else "",
            "default_assignee": names[-1] if names else "",
        }}),
    ]


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", assignee="researcher", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root and c0 and c1
    assert root.status == "todo"
    assert root.assignee == "researcher"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_false_assigns_default_when_unassigned(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="just one thing", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "**Goal**\nDo the thing.",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback", "sophie"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"decompose_allowed_assignees": ["orchestrator", "fallback"], "orchestrator_profile": "orchestrator", "default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    assert outcome.new_title == "Tightened title"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    # specify path with no parents -> recompute_ready flips to 'ready'
    assert task.status == "ready"
    assert task.title == "Tightened title"
    assert task.assignee == "fallback"


def test_decompose_fanout_false_preserves_existing_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="already routed",
            assignee="engineer",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Keep existing lane.",
        "assignee": "fallback",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"decompose_allowed_assignees": ["orchestrator", "engineer", "fallback"], "orchestrator_profile": "orchestrator", "default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"
    assert task.title == "Tightened title"


def test_decompose_fanout_false_uses_valid_llm_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to specialist.",
        "assignee": "engineer",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"decompose_allowed_assignees": ["orchestrator", "engineer", "fallback"], "orchestrator_profile": "orchestrator", "default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"decompose_allowed_assignees": ["orchestrator", "fallback"], "orchestrator_profile": "orchestrator", "default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_decompose_unknown_assignee_falls_back_to_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test",
        "tasks": [
            {"title": "do X", "body": "", "assignee": "made_up", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "fallback", "sophie"])
    for p in patches:
        p.start()
    try:
        with patch.dict(
            "os.environ", {}, clear=False,
        ), patch("hermes_cli.profiles.get_active_profile_name", return_value="sophie"), \
            _patch_aux_client(llm_payload) as call_llm, _patch_extra_body(), \
            patch(
                "hermes_cli.kanban_decompose._load_config",
                return_value={
                    "kanban": {
                        "decompose_allowed_assignees": ["orchestrator", "fallback"],
                        "orchestrator_profile": "orchestrator",
                        "default_assignee": "fallback",
                    }
                },
            ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
        root = kb.get_task(conn, tid)
    assert child and root
    assert child.assignee == "fallback"
    assert root.assignee == "orchestrator"
    assert "\n  - sophie" not in call_llm.call_args.kwargs["messages"][1]["content"]


def test_decompose_handles_malformed_llm_json(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("not json at all, sorry"), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "malformed JSON" in outcome.reason


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_no_aux_client_configured(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("No LLM provider configured"),
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "LLM error" in outcome.reason


@pytest.mark.parametrize(
    "allowed,extra,existing,error,root",
    [([], {}, None, "decompose-routing-policy-missing", None),
     ("worker", {}, None, "decompose-routing-policy-invalid", None),
     (["worker", "worker"], {}, None, "decompose-routing-policy-invalid", None),
     (["orch", "worker"], {"orchestrator_profile": "orch"}, "sophie", None, "orch"),
     (["orch", "worker"], {}, "worker", None, "worker"),
     (["worker"], {}, None, "decompose-routing-root-invalid", None),
     (["orch"], {"orchestrator_profile": "orch", "default_assignee": "sophie"}, None,
      "decompose-routing-default-invalid", "orch")],
)
def test_routing_policy_invariants(allowed, extra, existing, error, root):
    from types import SimpleNamespace
    installed = ["orch", "worker", "sophie"]
    profiles = [SimpleNamespace(name=n, description=n) for n in installed]
    cfg = {"kanban": {"decompose_allowed_assignees": allowed, **extra}}
    with patch("hermes_cli.profiles.list_profiles", return_value=profiles):
        policy = decomp.resolve_routing_policy(cfg, existing_assignee=existing)
    assert policy.error == error
    assert policy.root_assignee == root
    assert len(policy.allowed) == len(set(policy.allowed))
    assert set(policy.allowed) <= set(installed)
    assert {r["name"] for r in policy.roster or []} == set(policy.allowed)


@pytest.mark.parametrize(
    "choice,reason", [(None, "decompose-routing-default-invalid"),
                       ("bad/name", "decompose-routing-default-invalid"),
                       ("sophie", "decompose-routing-child-out-of-policy")],
)
def test_child_routing_failure_writes_nothing(kanban_home, choice, reason):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="safe routing", triage=True)
    payload = jsonlib.dumps({"fanout": True, "tasks": [
        {"title": "child", "assignee": choice, "parents": []},
    ]})
    patches = _patch_list_profiles(["orch", "worker", "sophie"])
    for p in patches:
        p.start()
    cfg = {"kanban": {"decompose_allowed_assignees": ["orch", "worker"],
                       "orchestrator_profile": "orch", "default_assignee": ""}}
    tables = ("tasks", "task_links", "task_comments", "task_events")
    try:
        with kb.connect() as conn:
            before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}
        with _patch_aux_client(payload), patch("hermes_cli.kanban_decompose._load_config", return_value=cfg):
            outcome = decomp.decompose_task(tid, author="me")
        with kb.connect() as conn:
            after = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}
    finally:
        for p in patches:
            p.stop()
    assert outcome.reason == reason
    assert after == before


@pytest.mark.parametrize(
    "cfg,reason",
    [({"kanban": {}}, "decompose-routing-policy-missing"),
     ({"kanban": {"decompose_allowed_assignees": ["ghost"]}},
      "decompose-routing-policy-invalid"),
     ({"kanban": {"decompose_allowed_assignees": ["orch"],
                   "orchestrator_profile": "orch", "default_assignee": "sophie"}},
      "decompose-routing-default-invalid"),
     ({"kanban": {"decompose_allowed_assignees": ["worker"]}},
      "decompose-routing-root-invalid")],
)
def test_pre_llm_routing_failure_writes_nothing(kanban_home, cfg, reason):
    tables = ("tasks", "task_links", "task_comments", "task_events")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="safe routing", triage=True)
        before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}
    patches = _patch_list_profiles(["orch", "worker", "sophie"])
    for p in patches:
        p.start()
    try:
        with patch("hermes_cli.kanban_decompose._load_config", return_value=cfg), patch(
            "agent.auxiliary_client.call_llm",
        ) as call_llm:
            outcome = decomp.decompose_task(tid, author="me")
        with kb.connect() as conn:
            after = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}
    finally:
        for p in patches:
            p.stop()
    assert outcome.reason == reason
    assert after == before
    call_llm.assert_not_called()
