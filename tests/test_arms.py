import json

from skill_court import arms


def write_profile(home, name, body):
    path = home / "profiles" / name
    path.mkdir(parents=True)
    (path / "profile.yaml").write_text(body)


def test_arm_names_are_short_distinct_and_valid_profile_ids():
    long_source = "a" * 64
    names = {arms.arm_profile(source, arm) for source in ("default", long_source) for arm in arms.ARMS}
    assert len(names) == 4
    for name in names:
        assert len(name) <= 24 and name == name.lower() and name.replace("-", "").isalnum()


def test_replay_profile_with_folded_description_is_recognised(hermes_home):
    head, tail = arms.DESCRIPTION.rsplit(" ", 1)
    name = arms.arm_profile("work", "with")
    write_profile(hermes_home, name, f"description: {head}\n  {tail}\ndescription_auto: false\n")
    assert arms._owned(name)


def test_a_users_profile_with_the_same_name_is_never_claimed(hermes_home):
    name = arms.arm_profile("work", "with")
    write_profile(hermes_home, name, "description: my own experiments\n")
    assert not arms._owned(name)
    assert not arms._owned("missing-profile")


def test_skill_under_test_is_hidden_only_in_the_without_arm(monkeypatch):
    calls = []
    monkeypatch.setattr(arms, "hermes", lambda *args, **kw: calls.append(args))
    replay = arms.for_source("work")
    arms.set_skill_under_test(replay, "deploy", ["old-skill", "deploy", "on"])
    disabled = {args[1]: json.loads(args[5]) for args in calls}
    assert disabled == {
        replay.profiles["with"]: ["old-skill", "on"],
        replay.profiles["without"]: ["deploy", "old-skill", "on"],
    }
