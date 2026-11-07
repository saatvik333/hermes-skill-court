import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

from skill_court import appeal, scoring, store
from skill_court.procs import CommandError
from skill_court.skills import Skill


def _dir_hash(directory: Path) -> str:
    hasher = hashlib.md5()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            hasher.update(str(path.relative_to(directory)).encode())
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


_sync = types.ModuleType("tools.skills_sync")
_sync.dir_hash = _dir_hash
sys.modules.setdefault("tools", types.ModuleType("tools"))
sys.modules.setdefault("tools.skills_sync", _sync)

MODEL = "claude-sonnet-5-5"
# CRLF line ends and no final newline: undo must not normalise either.
ORIGINAL = b"---\r\nname: csv-totals\r\n---\r\n- The amount column is in cents.\r\n- Divide by 100."
DRAFT = "---\nname: csv-totals\n---\n- When an export says its amounts are in cents, divide by 100.\n"


def score(verdict, success_with, success_without=1.0):
    return scoring.Score(3, 3, success_with, success_without, None, None, None, None, verdict, f"{verdict} reason")


def test_a_draft_is_granted_only_when_it_stops_hurting_and_beats_the_original():
    assert appeal.decide(0.0, score(scoring.NEUTRAL, 1.0))[0] == appeal.GRANTED
    assert appeal.decide(0.0, score(scoring.HELPS, 0.67))[0] == appeal.GRANTED
    denied = {
        "hurts": appeal.decide(0.0, score(scoring.HURTS, 0.33)),
        "inconclusive": appeal.decide(0.0, score(scoring.INCONCLUSIVE, None)),
        "no gain": appeal.decide(0.33, score(scoring.NEUTRAL, 0.33)),
    }
    assert all(verdict == appeal.DENIED for verdict, _ in denied.values())
    assert "still hurts" in denied["hurts"][1] and "did not beat" in denied["no gain"][1]


def test_a_draft_must_keep_the_name_and_change_something():
    original = ORIGINAL.decode()
    assert appeal.check_draft(original, DRAFT.rstrip("\n")) == DRAFT
    with pytest.raises(CommandError, match="renames"):
        appeal.check_draft(original, DRAFT.replace("name: csv-totals", "name: other"))
    with pytest.raises(CommandError, match="changes nothing"):
        appeal.check_draft(original, original + "\n")


@pytest.fixture
def skill(hermes_home):
    path = hermes_home / "skills" / "demo" / "csv-totals"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_bytes(ORIGINAL)
    (path / "notes.md").write_text("kept as is")
    return Skill("csv-totals", path, "agent")


def drafted_appeal(skill: Skill, verdict: str = appeal.GRANTED) -> int:
    """A decided appeal of a quarantined skill, as ``draft_and_try`` leaves it."""
    hurts = store.start_trial(skill.name, skill.content_hash(), MODEL)
    store.finish_trial(hurts, MODEL, scoring.HURTS, json.dumps({"success_with": 0.0}))
    store.quarantine(skill.name, MODEL, f"Skill Court trial #{hurts}")
    draft_dir = skill.path.parent / "draft-copy"
    draft_dir.mkdir()
    (draft_dir / "SKILL.md").write_text(DRAFT)
    (draft_dir / "notes.md").write_text("kept as is")
    appeal_id = store.add_appeal(skill.name, MODEL, hurts, skill.content_hash(), ORIGINAL, DRAFT,
                                 _dir_hash(draft_dir), appeal.diff(skill.name, ORIGINAL.decode(), DRAFT))
    for path in draft_dir.iterdir():
        path.unlink()
    draft_dir.rmdir()
    draft_trial = store.start_trial(skill.name, store.latest_appeal(skill.name)["draft_hash"], MODEL, appeal_id=appeal_id)
    store.finish_trial(draft_trial, MODEL, scoring.NEUTRAL, "{}")
    store.decide_appeal(appeal_id, draft_trial, verdict, "reason")
    return appeal_id


def test_apply_writes_the_draft_lifts_the_quarantine_and_undo_restores_both_byte_for_byte(skill):
    drafted_appeal(skill)
    assert [t["verdict"] for t in store.latest_trials()] == ["hurts"]  # the draft is not the skill yet

    appeal.apply(skill, MODEL)
    assert (skill.path / "SKILL.md").read_text() == DRAFT
    assert store.hidden_for(MODEL) == {}
    assert [t["verdict"] for t in store.latest_trials()] == ["neutral"]  # the draft's trial is current

    appeal.undo(skill, MODEL)
    assert (skill.path / "SKILL.md").read_bytes() == ORIGINAL
    assert (skill.path / "notes.md").read_text() == "kept as is"
    assert list(store.hidden_for(MODEL)) == [skill.name]
    assert [t["verdict"] for t in store.latest_trials()] == ["hurts"]
    assert sorted(p.name for p in skill.path.iterdir()) == ["SKILL.md", "notes.md"]  # no temp files left


def test_apply_refuses_a_denied_appeal(skill):
    drafted_appeal(skill, appeal.DENIED)
    with pytest.raises(CommandError, match="denied"):
        appeal.apply(skill, MODEL)
    assert (skill.path / "SKILL.md").read_bytes() == ORIGINAL


def test_apply_refuses_when_the_skill_changed_since_the_draft(skill):
    drafted_appeal(skill)
    (skill.path / "SKILL.md").write_text("edited by hand\n")
    with pytest.raises(CommandError, match="changed since"):
        appeal.apply(skill, MODEL)
    assert (skill.path / "SKILL.md").read_text() == "edited by hand\n"


def test_apply_refuses_a_locked_skill(skill):
    drafted_appeal(skill)
    with pytest.raises(CommandError, match="locked"):
        appeal.apply(Skill(skill.name, skill.path, "agent", locked=True), MODEL)
    assert (skill.path / "SKILL.md").read_bytes() == ORIGINAL


def test_undo_refuses_when_the_applied_fix_was_edited_since(skill):
    drafted_appeal(skill)
    appeal.apply(skill, MODEL)
    (skill.path / "SKILL.md").write_text(DRAFT + "- a later edit\n")
    with pytest.raises(CommandError, match="changed since the fix was applied"):
        appeal.undo(skill, MODEL)
