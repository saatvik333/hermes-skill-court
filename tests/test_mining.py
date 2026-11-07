import json

from skill_court.mining import tasks_in_session


def view(name, **extra):
    return {"id": "t", "type": "function",
            "function": {"name": "skill_view", "arguments": json.dumps({"name": name, **extra})}}


def assistant(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def test_task_is_the_user_message_that_triggered_the_load():
    messages = [
        {"role": "user", "content": "Deploy the preview build of the docs site"},
        assistant(view("devops/deploy-preview")),
        {"role": "tool", "tool_name": "skill_view", "content": "{}"},
        {"role": "user", "content": "Now clean up the stale docker images please"},
        assistant(view("docker-cleanup")),
    ]
    assert tasks_in_session(messages, None) == {
        "deploy-preview": "Deploy the preview build of the docs site",
        "docker-cleanup": "Now clean up the stale docker images please",
    }


def test_only_the_first_load_per_skill_and_only_wanted_skills():
    messages = [
        {"role": "user", "content": "First request that loads the skill"},
        assistant(view("arxiv")),
        {"role": "user", "content": "Second request that loads it again"},
        assistant(view("arxiv"), view("other-skill")),
    ]
    assert tasks_in_session(messages, frozenset({"arxiv"})) == {"arxiv": "First request that loads the skill"}


def test_linked_file_reads_and_trivial_prompts_are_ignored():
    messages = [
        {"role": "user", "content": "ok"},
        assistant(view("arxiv")),
        {"role": "user", "content": [{"type": "text", "text": "Summarise the latest arXiv papers on agents"}]},
        assistant(view("pdf-tools", file_path="references/api.md")),
    ]
    assert tasks_in_session(messages, None) == {}


def test_malformed_tool_calls_are_skipped():
    messages = [
        {"role": "user", "content": "A real request with enough text"},
        assistant({"function": {"name": "skill_view", "arguments": "{not json"}}, "garbage"),
        {"role": "assistant", "content": "", "tool_calls": None},
    ]
    assert tasks_in_session(messages, None) == {}
