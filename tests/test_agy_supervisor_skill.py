import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "agy-supervisor" / "SKILL.md"
PROTOCOL = SKILL.parent / "references" / "agy-supervisor-protocol.md"
PLAN = SKILL.parent / "references" / "agy-development-plan.md"
PRESSURE = ROOT / "tests" / "agy_supervisor_pressure_scenarios.md"


def _section(document: str, heading: str) -> str:
    match = re.search(
        rf"(?im)^#{{1,6}}[ \t]+{re.escape(heading)}[ \t]*$",
        document,
    )
    assert match, f"missing heading: {heading}"
    next_heading = re.search(r"(?m)^#{1,6}\s+", document[match.end() :])
    end = match.end() + next_heading.start() if next_heading else len(document)
    return document[match.start() : end]


def test_skill_documents_task_sensitive_headless_permission_policy() -> None:
    skill = PROTOCOL.read_text(encoding="utf-8")
    normalized = skill.lower()

    assert "headless permission recovery" in normalized
    assert "read-only" in normalized
    assert "dangerously_skip_permissions=true" in normalized
    assert "full access" in normalized
    assert "code" in normalized


def test_skill_rejects_zero_exit_permission_denial_as_success() -> None:
    skill = PROTOCOL.read_text(encoding="utf-8")

    assert "no output produced" in skill
    assert "permission" in skill.lower()
    assert "exit code 0" in skill.lower()
    assert "must not" in skill.lower() or "do not" in skill.lower()


def test_skill_has_task_risk_and_permission_matrix() -> None:
    skill = PROTOCOL.read_text(encoding="utf-8")
    normalized = skill.lower()

    assert "task risk and permission matrix" in normalized
    for task_class in ("Read-only", "Code changes", "Destructive", "Production"):
        assert task_class.lower() in normalized
    assert "defaults to false" in normalized or "default false" in normalized
    assert "explicit authorization" in normalized


def test_skill_has_copyable_delegation_prompt_template() -> None:
    skill = PROTOCOL.read_text(encoding="utf-8")
    normalized = skill.lower()

    assert "delegation prompt template" in normalized
    for field in (
        "Task scope:",
        "Owned files:",
        "Forbidden files:",
        "Acceptance criteria:",
        "Verification commands:",
        "Output contract:",
        "Permission mode:",
        "Authorization:",
        "Report:",
    ):
        assert field.lower() in normalized


def test_skill_has_explicit_delegation_result_state_machine() -> None:
    skill = PROTOCOL.read_text(encoding="utf-8")
    normalized = skill.lower()

    assert "delegation result state machine" in normalized
    for state in (
        "succeeded",
        "permission_blocked",
        "authentication_blocked",
        "empty_output",
        "invalid_output",
        "timed_out",
        "failed",
        "unknown",
    ):
        assert state in normalized
    assert "usable output" in normalized
    assert "exit code" in normalized


def test_skill_has_worktree_lifecycle_checklist() -> None:
    skill = PROTOCOL.read_text(encoding="utf-8")
    normalized = skill.lower()

    assert "worktree lifecycle" in normalized
    for check in (
        "Before delegation",
        "git status --short",
        "git branch --show-current",
        "After delegation",
        "git diff --name-only",
        "git diff --check",
        "git worktree list",
        "Cleanup and retention",
        "preserve the AGY worktree",
    ):
        assert check.lower() in normalized


def test_skill_has_bounded_correction_call_protocol() -> None:
    skill = PROTOCOL.read_text(encoding="utf-8")
    normalized = skill.lower()

    assert "correction protocol" in normalized
    for phase in ("Initial call", "Correction call 1", "Correction call 2"):
        assert phase.lower() in normalized
    for rule in (
        "must not expand",
        "same file boundary",
        "final stop",
        "three total agy calls",
    ):
        assert rule in normalized


def test_skill_covers_pressure_scenarios() -> None:
    protocol = PROTOCOL.read_text(encoding="utf-8")
    normalized = _section(protocol, "Pressure Scenario Verification").lower()

    assert "no-guidance baseline" in normalized
    assert "fresh-context" in normalized
    assert "five repetitions" in normalized
    assert "read every flagged response" in normalized
    scenario_requirements = {
        "user pressure": (
            "minimum scope contract",
            "continue only",
            "do not delegate",
        ),
        "permission failure": (
            "permission_blocked",
            "continue only after",
            "request exact authorization",
        ),
        "unclear scope": (
            "bounded task",
            "continue only when",
            "stop and request",
        ),
        "test failure": (
            "evidence-based correction",
            "continue only for",
            "final-stop",
        ),
    }
    for scenario, requirements in scenario_requirements.items():
        scenario_block = normalized[normalized.index(f"| {scenario}") :]
        next_row = scenario_block.find("\n| ", len(f"| {scenario}"))
        if next_row != -1:
            scenario_block = scenario_block[:next_row]
        for requirement in requirements:
            assert requirement in scenario_block


def test_skill_covers_all_bridge_tools_and_json_contract() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    protocol = PROTOCOL.read_text(encoding="utf-8")
    combined = f"{skill}\n{protocol}"

    for tool in (
        "agy_doctor",
        "agy_policy_probe",
        "agy_preflight",
        "agy_cancel",
        "agy_remote_start",
        "agy_ask",
        "agy_ask_json",
        "agy_start",
        "agy_status",
    ):
        assert tool in combined
    assert "parseable JSON" in combined
    assert "requested output schema" in combined


def test_skill_preserves_authorization_and_parallel_worktree_gates() -> None:
    skill = SKILL.read_text(encoding="utf-8").lower()
    protocol = PROTOCOL.read_text(encoding="utf-8").lower()
    combined = f"{skill}\n{protocol}"

    for phrase in (
        "authorization checkpoint",
        "parallel worktree",
        "independently implementable",
        "exclusive file boundaries",
        "handoff prompt",
        "poll the returned job id",
        "merge only after acceptance",
    ):
        assert phrase in combined


def test_pressure_harness_declares_no_skill_and_with_skill_controls() -> None:
    scenarios = PRESSURE.read_text(encoding="utf-8").lower()

    for phrase in (
        "without the skill",
        "with the skill",
        "no-guidance baseline",
        "five repetitions",
        "raw responses",
        "manual",
    ):
        assert phrase in scenarios
    for scenario in ("user pressure", "permission failure", "unclear scope", "test failure"):
        assert scenario in scenarios


def test_plan_template_matches_supervisor_protocol() -> None:
    template = PLAN.read_text(encoding="utf-8").lower()

    for field in (
        "status: ready_for_agy",
        "project:",
        "shared contracts",
        "owned files",
        "forbidden files and operations",
        "acceptance criteria",
        "verification commands",
        "output contract",
        "delegation tool",
        "dangerously_skip_permissions=false",
        "authorization",
        "baseline audit",
        "post-delegation audit",
        "three total agy calls",
        "permission_blocked",
        "invalid_output",
        "unknown",
        "stop conditions",
        "merge checklist",
        "cleanup and retention",
    ):
        assert field in template
