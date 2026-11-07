import json
from types import SimpleNamespace

import pytest

from skill_court.judge import Attempt, Judge, agreed, excerpt, successes, verify_quote

SKILL = """---
name: sales-csv-totals
description: Use when computing totals from our sales CSV files.
---
# Sales CSV totals

- The `amount` column in sales CSV exports is stored in cents.
- Sum the column, then divide by 100 to get dollars before reporting.
"""


class FakeLlm:
    """Answers each comparison with ``pick(text)``; records every prompt it was sent."""

    def __init__(self, pick):
        self.pick = pick
        self.prompts = []

    def complete_structured(self, *, instructions, input, **_):
        text = input[0]["text"]
        self.prompts.append(instructions + "\n" + text)
        better = self.pick(text)
        if isinstance(better, Exception):
            raise better
        return SimpleNamespace(parsed={"better": better, "reason": "r"}, text="", model="judge-model",
                               usage=SimpleNamespace(total_tokens=10))


def attempt_a(text):
    return text.split("## Attempt A")[1].split("## Attempt B")[0]


WITH = Attempt("4.65", "--- /workspace/total.txt (4 bytes)\n4.65")
WITHOUT = Attempt("465", "--- /workspace/total.txt (3 bytes)\n465")


def test_a_winner_needs_both_orders():
    assert agreed("without", "without") == "without"
    assert agreed("with", "with") == "with"
    assert agreed("with", "without") == "tie"
    assert agreed("tie", "with") == "tie"
    assert agreed("tie", "tie") == "tie"


def test_judged_successes():
    assert successes("with") == (True, False)
    assert successes("without") == (False, True)
    assert successes("tie") == (True, True)


def test_the_better_result_wins_whichever_position_it_is_shown_in():
    llm = FakeLlm(lambda text: "A" if "465" in attempt_a(text) and "4.65" not in attempt_a(text) else "B")
    judgment = Judge(llm).compare("Total the sales", WITH, WITHOUT)
    assert (judgment.first, judgment.second, judgment.winner) == ("without", "without", "without")
    assert judgment.tokens == 20 and judgment.error is None
    # The with-skill run is shown as A first, then as B; the judge is never told about skills.
    assert "4.65" in attempt_a(llm.prompts[0]) and "4.65" not in attempt_a(llm.prompts[1])
    assert all("skill" not in prompt.lower() for prompt in llm.prompts)


def test_a_judge_that_favours_a_position_produces_a_tie():
    judgment = Judge(FakeLlm(lambda _text: "A")).compare("Total the sales", WITH, WITHOUT)
    assert (judgment.first, judgment.second, judgment.winner) == ("with", "without", "tie")


def test_a_failed_or_malformed_judgment_gives_no_outcome():
    failed = Judge(FakeLlm(lambda _text: RuntimeError("provider down"))).compare("t", WITH, WITHOUT)
    assert failed.winner is None and "provider down" in failed.error
    malformed = Judge(FakeLlm(lambda _text: "C")).compare("t", WITH, WITHOUT)
    assert malformed.winner is None and "'C'" in malformed.error


def test_quote_is_the_skill_line_it_comes_from():
    line = "- The `amount` column in sales CSV exports is stored in cents."
    assert verify_quote(line, SKILL) == line
    # Markdown code marks, surrounding quotes and a missing bullet do not change the words.
    assert verify_quote('"The amount column in sales CSV exports is stored in cents."', SKILL) == line
    assert verify_quote("divide by 100 to get dollars", SKILL) == "- Sum the column, then divide by 100 to get dollars before reporting."


def test_quote_not_in_the_skill_is_dropped():
    assert verify_quote("Amounts are always stored in cents.", SKILL) is None
    assert verify_quote("in cents", SKILL) is None  # too short to identify a line
    # Two lines run together are not a line of the file.
    assert verify_quote("stored in cents. - Sum the column", SKILL) is None


def view(name):
    return {"function": {"name": "skill_view", "arguments": json.dumps({"name": name})}}


def call(name, **args):
    return {"function": {"name": name, "arguments": json.dumps(args)}}


def test_excerpt_runs_from_the_skill_load_to_the_final_answer():
    messages = [
        {"role": "user", "content": "Total the sales"},
        {"role": "assistant", "content": "", "tool_calls": [call("terminal", command="ls")]},
        {"role": "tool", "tool_name": "terminal", "content": "sales.csv"},
        {"role": "assistant", "content": "", "tool_calls": [view("sales-csv-totals")]},
        {"role": "tool", "tool_name": "skill_view", "content": "amounts are in cents"},
        {"role": "assistant", "content": "", "tool_calls": [call("terminal", command="echo 4.65 > total.txt")]},
        {"role": "tool", "tool_name": "terminal", "content": ""},
        {"role": "assistant", "content": "The total is 4.65."},
    ]
    text = excerpt(messages, "sales-csv-totals")
    assert '"command": "ls"' not in text
    assert text.splitlines()[0].startswith("[calls skill_view]")
    assert "echo 4.65 > total.txt" in text
    assert text.endswith("[final answer] The total is 4.65.")


@pytest.mark.parametrize("steps", [5, 200])
def test_excerpt_keeps_the_final_answer_within_its_limit(steps):
    messages = [{"role": "assistant", "content": "", "tool_calls": [view("s")]}]
    messages += [{"role": "tool", "tool_name": "terminal", "content": "x" * 300} for _ in range(steps)]
    messages.append({"role": "assistant", "content": "done"})
    text = excerpt(messages, "s", limit=2_000)
    assert len(text) <= 2_100
    assert text.endswith("[final answer] done")
    assert ("more step(s) not shown" in text) == (steps == 200)
