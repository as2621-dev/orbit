"""Structural checks for the §8 onboarding README, the repo-root README, and the
plugin marketplace manifest (Phase 6 sub-phase 4).

WHY these tests exist: the §8 README and the public marketplace manifest are the
outward-facing, distribution artifacts. Their *structure* is a product contract from the
brief (§8.1-§8.6: the five permissions, the un-softened risk disclosure, the cost guidance)
and the manifest must stay valid JSON that declares the orbit plugin/skill or installs
break. These tests fail loudly if a required section, permission row, risk clause, or
manifest field is dropped — not merely if formatting changes. The honest end-to-end *reading*
of the README is a human-review item and is intentionally NOT asserted here.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_README = REPO_ROOT / "skills" / "orbit" / "README.md"
ROOT_README = REPO_ROOT / "README.md"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"


@pytest.fixture(scope="module")
def skill_readme_text() -> str:
    """The §8 onboarding README content (lowercased for case-insensitive grep)."""
    return SKILL_README.read_text(encoding="utf-8").lower()


def test_skill_readme_exists() -> None:
    """WHY: the §8 README is the PRIMARY M4 deliverable; if it's missing, onboarding
    and the permissions/risk disclosure do not exist for a new user at all."""
    assert SKILL_README.is_file(), f"missing primary deliverable: {SKILL_README}"


def test_skill_readme_has_all_section_8_headings(skill_readme_text: str) -> None:
    """WHY: the brief mandates the §8.1-§8.6 structure verbatim. A missing section means a
    user is missing prerequisites, setup, permissions, risk, or troubleshooting — each is a
    real onboarding gap, not a cosmetic one."""
    required_headings = ["8.1", "8.2", "8.3", "8.4", "8.5", "8.6"]
    missing = [h for h in required_headings if h not in skill_readme_text]
    assert not missing, f"README missing required §8 section headings: {missing}"


def test_skill_readme_section_titles_present(skill_readme_text: str) -> None:
    """WHY: section numbers alone aren't enough — the named intent of each section must be
    present so the doc actually covers what the brief requires."""
    required_phrases = [
        "what orbit does",  # 8.1
        "prerequisites",  # 8.2
        "setup",  # 8.3
        "permissions",  # 8.4
        "risk disclosure",  # 8.5
        "troubleshooting",  # 8.6
    ]
    missing = [p for p in required_phrases if p not in skill_readme_text]
    assert not missing, f"README missing section intent phrases: {missing}"


def test_permissions_table_covers_all_five_permissions(skill_readme_text: str) -> None:
    """WHY: §8.4 must disclose EACH of the five things Orbit asks for. A dropped permission
    row means an undisclosed capability — a transparency failure, the exact thing this doc
    is supposed to prevent."""
    required_permissions = [
        "cookies",  # read browser cookies
        "filesystem",  # filesystem write
        "network",  # network access
        "applescript",  # run AppleScript (iMessage)
        "llm",  # LLM usage on the user's plan
    ]
    missing = [p for p in required_permissions if p not in skill_readme_text]
    assert not missing, f"permissions table missing rows for: {missing}"


def test_permissions_table_has_why_and_what_we_do_columns(skill_readme_text: str) -> None:
    """WHY: the brief requires each permission row to state BOTH why it's needed AND what we
    do/don't do. Without the 'what we do/don't' column the disclosure is half-honest."""
    assert "why we need it" in skill_readme_text, "permissions table missing the 'why' column"
    assert "what we do / don't do" in skill_readme_text, (
        "permissions table missing the 'what we do / don't do' column"
    )


def test_section_8_5_risk_clauses_present_and_unsoftened(skill_readme_text: str) -> None:
    """WHY: §8.5 is a product requirement, explicitly 'do not soften'. Each clause below
    protects the user from a real harm (credential theft, account flagging, not knowing how
    to revoke, false sense of a cloud service). Dropping any one is a safety regression."""
    # auth_token = full account access
    assert "auth_token" in skill_readme_text, "§8.5 must name auth_token"
    assert "full account access" in skill_readme_text, (
        "§8.5 must state auth_token is full account access"
    )
    # ToS-gray unofficial method
    assert "tos-gray" in skill_readme_text, "§8.5 must disclose the ToS-gray nature"
    # revocation = log out
    assert "log out" in skill_readme_text, "§8.5 must explain revocation via logging out"
    # everything local, no server
    assert "no orbit server" in skill_readme_text, (
        "§8.5 must state no Orbit server exists / everything is local"
    )


def test_section_8_5_keeps_blunt_password_framing(skill_readme_text: str) -> None:
    """WHY: the blunt 'treat your cookies like a password' framing is what makes the risk
    land for a non-expert. A softened reword would defeat the section's purpose."""
    assert "like a password" in skill_readme_text, (
        "§8.5 must keep the blunt 'treat your cookies like a password' framing"
    )


def test_troubleshooting_covers_the_four_failure_modes(skill_readme_text: str) -> None:
    """WHY: §8.6 must cover the four real failure modes from integrations.md; each maps to a
    user who would otherwise be stuck with no recourse."""
    required = ["404", "no cookies found", "expired", "rate-limit"]
    missing = [r for r in required if r not in skill_readme_text]
    assert not missing, f"§8.6 troubleshooting missing failure modes: {missing}"


def test_cost_estimate_and_default_depth_recommendation_present(skill_readme_text: str) -> None:
    """WHY: the brief (§7) requires a rough daily-cost-by-depth estimate AND a 'start with
    default' recommendation so users aren't surprised by token spend. This is the cost-
    transparency contract."""
    # all three depth tiers named in a cost context
    for tier in ["quick", "default", "deep"]:
        assert tier in skill_readme_text, f"cost section must mention depth tier '{tier}'"
    # a cost signal (currency / per-day) is present
    assert "$" in skill_readme_text or "cost" in skill_readme_text, (
        "README must include a rough daily-cost estimate"
    )
    # the explicit recommendation to start with default
    assert "start with" in skill_readme_text and "default" in skill_readme_text, (
        "README must recommend starting with depth=default"
    )


def test_marketplace_json_parses() -> None:
    """WHY: marketplace.json is the public-distribution artifact. If it doesn't parse as JSON,
    every install fails — this is the irreversible packaging surface, so it must be valid."""
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "marketplace.json must be a JSON object"


def test_marketplace_declares_orbit_plugin_and_skill() -> None:
    """WHY: the manifest must declare the `orbit` plugin and the `orbit` skill at
    skills/orbit, or Claude Code can't find/install the skill the README points users at."""
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    assert data.get("name") == "orbit", "marketplace name must be 'orbit'"

    plugins = data.get("plugins", [])
    assert plugins, "marketplace must declare at least one plugin"
    orbit_plugin = next((p for p in plugins if p.get("name") == "orbit"), None)
    assert orbit_plugin is not None, "marketplace must declare the 'orbit' plugin"
    assert orbit_plugin.get("description"), "the orbit plugin must carry a description"

    skills = orbit_plugin.get("skills", [])
    orbit_skill = next((s for s in skills if s.get("name") == "orbit"), None)
    assert orbit_skill is not None, "the orbit plugin must declare the 'orbit' skill"
    assert orbit_skill.get("path") == "skills/orbit", (
        "the orbit skill must point at skills/orbit"
    )


def test_root_readme_points_at_skill_readme() -> None:
    """WHY: README-update discipline requires the repo-root README to exist and route users
    to the full §8 onboarding/permissions doc rather than duplicating or omitting it."""
    assert ROOT_README.is_file(), "repo-root README.md must exist"
    text = ROOT_README.read_text(encoding="utf-8").lower()
    assert "skills/orbit/readme.md" in text, (
        "root README must point at the full skills/orbit/README.md onboarding doc"
    )
