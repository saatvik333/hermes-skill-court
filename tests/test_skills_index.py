import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill-court"))

from skills_index import strip_request, strip_text  # noqa: E402

PROMPT = """## Skills
Before replying, scan the skills below.

<available_skills>
  devops:
    - deploy-preview: Deploy a preview build
    - docker-cleanup: Prune stale images
  research:
    - arxiv: Search arXiv
  - solo-skill: The only skill in its category
  media [names only]: gif-search, deploy-preview-media, songsee
</available_skills>

Only proceed without loading a skill if genuinely none are relevant to the task."""


def test_removes_row_and_keeps_siblings():
    new, removed = strip_text(PROMPT, frozenset({"deploy-preview"}))
    assert removed == {"deploy-preview"}
    assert "- deploy-preview:" not in new
    assert "    - docker-cleanup: Prune stale images" in new
    assert "  devops:" in new
    assert "deploy-preview-media" in new  # a longer name sharing the prefix stays


def test_drops_header_of_emptied_category():
    new, removed = strip_text(PROMPT, frozenset({"arxiv"}))
    assert removed == {"arxiv"}
    assert "research:" not in new
    assert "  - solo-skill: The only skill in its category" in new


def test_single_skill_row_and_names_only_entries():
    new, removed = strip_text(PROMPT, frozenset({"solo-skill", "gif-search"}))
    assert removed == {"solo-skill", "gif-search"}
    assert "solo-skill" not in new
    assert "  media [names only]: deploy-preview-media, songsee" in new


def test_names_only_line_disappears_when_empty():
    new, _ = strip_text(PROMPT, frozenset({"gif-search", "deploy-preview-media", "songsee"}))
    assert "[names only]" not in new


def test_text_outside_the_block_is_untouched():
    new, _ = strip_text(PROMPT, frozenset({"arxiv"}))
    before, after = PROMPT.split("<available_skills>")[0], PROMPT.split("</available_skills>")[1]
    assert new.startswith(before) and new.endswith(after)


def test_no_change_returns_none():
    assert strip_text(PROMPT, frozenset({"not-a-skill"})) == (None, set())
    assert strip_text("no index here", frozenset({"arxiv"})) == (None, set())


def test_request_shapes():
    hidden = frozenset({"arxiv"})
    chat = {"messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": "hi"}]}
    anthropic = {"system": [{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}]}
    responses = {"instructions": PROMPT}
    for request in (chat, anthropic, responses):
        new, removed = strip_request(request, hidden)
        assert removed == {"arxiv"}
        assert "- arxiv:" not in str(new)
        assert "- arxiv:" in str(request)  # the input is never mutated
    new, _ = strip_request(anthropic, hidden)
    assert new["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_user_messages_are_not_rewritten():
    request = {"messages": [{"role": "user", "content": PROMPT}]}
    assert strip_request(request, frozenset({"arxiv"})) == (None, set())
