# Skill Court

**Does the skill your agent wrote actually help?** Skill Court runs the same tasks twice, once with
the skill and once without it, each run in its own Docker sandbox, and compares the results. Skills
that make results worse for a model are hidden from that model. Their files are never edited, and
one command brings them back.

![Skill Court verdicts on a demo skill library](../docs/banner.png)

## Why

Hermes writes skills for itself, then grades its own work: the agent is "simultaneously the
**author**, **executor**, and **quality inspector** of its own skills"
([#25833](https://github.com/NousResearch/hermes-agent/issues/25833)). The curator tracks how often a
skill is used, not whether it helps. Measured from outside, loaded skills do cause failures: "Agent
Skills Can Be Harmful" ([arXiv 2608.11888](https://arxiv.org/abs/2608.11888)) traced 307 failures to
specific skills by running each task with and without the skill. On
[#13534](https://github.com/NousResearch/hermes-agent/issues/13534#issuecomment-5292383616) Teknium
wrote that "per-skill **outcome** attribution matters" and that this paired harness "is cheap to
reproduce", and [RFC #96704](https://github.com/NousResearch/hermes-agent/issues/96704) asks for a
paired-arm harness with per-model validation. Skill Court is that harness, wired into the live prompt.

## What it found on a demo library

Four small skills of the kinds an agent writes for itself, each tried on three different tasks, every
task checked inside its sandbox afterwards:

| Skill | What it is | Claude Sonnet 5.5 | Claude Haiku 5.5 |
|---|---|---|---|
| `team-changelog` | A team convention the model can't guess | **helps**: success 0% → 100% | **helps**: success 0% → 100% |
| `sales-csv-totals` | A lesson overgeneralised from one session ("amounts are in cents") | **hurts**: success 100% → 0%, quarantined | neutral: success 100% → 67% |
| `python-venv-setup` | A setup ritual learned in another repo | neutral | neutral |
| `json-format` | A harmless tip | neutral | neutral |

Sonnet followed the "cents" rule on every task, in each of the three trials we ran, so the skill is
quarantined for Sonnet. Haiku followed it only sometimes (one or two wrong totals out of three in
two of its three trials), which is not consistent enough for a verdict at three tasks, so Haiku
still sees it. The demo is in `demo/` (see [Try it](#try-it)).

## How a trial works

```
hermes court trial --all
```

1. **Finds tasks.** For each skill: the requests in your session history that made the agent load it
   (`skill_view`), plus tasks you add, optionally with a pass/fail check.
2. **Builds two replay profiles per skill.** Both are fresh clones of your profile, so they carry your
   model, prompt, memory and skills; one hides the skill under test with `skills.disabled`. Skills your
   live prompt already hides are hidden in both. The clones are cut off from everything outside their
   sandbox (see [Disclosure](#disclosure)) and rebuilt for every skill, so nothing one trial did carries
   into the next.
3. **Replays in pairs.** Each task runs in both profiles at the same time as a single-query session
   (`hermes chat -Q --oneshot`), on your model or the one you pass, each run in a fresh Docker sandbox.
4. **Scores.** A task's check runs inside the run's sandbox afterwards (exit 0 = pass); a task without
   a check counts the run completing. Token use is compared pair by pair. A run the model never
   answered (provider, credit or credential errors) has no outcome and is left out, instead of being
   counted against the skill; a run that hits its iteration limit counts as not completed.
5. **Rules.** Repeated runs of one task are averaged first, so evidence is counted per task. `hurts`
   when success drops with the skill, or when success is unchanged and the skill costs at least 25%
   more tokens; `helps` is the reverse; `neutral` when neither shows; `inconclusive` under three
   tasks with outcomes in both arms. A verdict needs its whole 95% bootstrap interval on one side,
   which with three tasks means every task moved the same way. Each verdict is tied to the skill's
   content hash and the model.
6. **Quarantines** a harmful skill for that model, with `--apply` or by hand.

A quarantined skill is hidden at three points, because hiding it from the index alone is not enough:
its row is removed from `<available_skills>` (`llm_request` middleware), it is removed from
`skills_list` results (`transform_tool_result`), and loading it with `skill_view` is refused with the
reason (`pre_tool_call`). Other models keep seeing it.

## Commands

| Command | What it does |
|---|---|
| `hermes court trial <skill>… \| --all` | Paired replays and a verdict per skill |
| `hermes court report` | Latest verdict per skill and model, and whether the skill changed since |
| `hermes court tasks list <skill>` | A skill's replay tasks |
| `hermes court tasks add <skill> --prompt … [--check …]` | Add a task; the check is a shell command run in the replay's sandbox |
| `hermes court tasks mine [skill…]` | Collect tasks from session history |
| `hermes court tasks rm <id>` | Remove a task |
| `hermes court quarantine <skill> [--model M]` | Hide a skill (default: from every model) |
| `hermes court release <skill> [--model M]` | Show it again (default: to every model) |
| `hermes court list` | Quarantined skills |

`trial` options: `--tasks 5` per skill, `--reps 1` per task and arm, `--parallel 4` concurrent runs,
`--budget 3` (USD; no new runs start past it), `--allow-unpriced` to keep going when the provider
reports no price, `--timeout 900` seconds per run before it is interrupted, `--model` and
`--provider` for the replays, `--apply` to quarantine harmful skills, `--keep-sandboxes` to keep the
replay profiles for inspection.

`--all` covers the skills the agent wrote (the curator records them as `created_by` `agent` or
`learn`). Name any other skill explicitly.

## Desktop

The plugin adds a **Skill Court** page to Hermes Desktop and the Webapp (turn its Desktop half on in
**Capabilities → Plugins**). It lists the latest verdict per skill and model, a skill's verdicts on
different models side by side, harmful skills first, and lets you quarantine or release each one.

![The Skill Court page in Hermes Desktop](../docs/desktop.png)

## Install

Needs Hermes 0.23.0 or newer, on Linux or macOS, with Docker or Podman.

```
hermes plugins install skill-court
hermes plugins enable skill-court
```

Straight from GitHub: `hermes plugins install saatvik333/hermes-skill-court/skill-court --enable`.

## Try it

From a checkout of [the repository](https://github.com/saatvik333/hermes-skill-court),
`demo/setup.sh <profile>` copies the four demo skills into a profile, marks them as agent-written
(`hermes curator adopt`), pins them so the curator leaves them alone, and gives each three checked
tasks. Use a lab profile with the plugin enabled, not the one you work in:

```
hermes profile create courtlab --clone
hermes -p courtlab plugins install saatvik333/hermes-skill-court/skill-court --enable
demo/setup.sh courtlab
hermes -p courtlab court trial --all --tasks 3 --apply
hermes -p courtlab court trial --all --tasks 3 --apply --model claude-haiku-5-5 --provider anthropic
hermes -p courtlab court report
```

## Limits

- Tasks mined from history replay in an empty sandbox, without the files the original session had,
  so they mostly measure completion and token use. Add tasks with checks for pass/fail evidence.
- A skill the model never loads for a task can only come out `neutral` on it.
- Replays run without the tools that act outside the sandbox or keep state between runs (see
  Disclosure), so a skill that depends on one of them is measured without it.
- Three tasks is the minimum for a verdict, not strong evidence: it means all three moved the same
  way. Quarantine is reversible for that reason. Use more tasks before trusting a close call.
- A replay runs for at most 60 tool-calling iterations; one that hits the limit counts as not
  completed. Near `--timeout` the agent is asked to wrap up; past it the run is interrupted and has
  no outcome, and its spend may be missing from the total, which is therefore a lower bound.
- A provider error partway through a run (after the model has already answered some calls) counts
  as the run not completing, against whichever arm it hit. Only runs the model never answered are
  left out.
- Quarantine hides a skill from the agent's skill index and skill tools; the file stays readable to
  file tools.
- In `codex_app_server` mode the prompt bypasses `llm_request`, so the model is never known to the
  plugin there: only quarantines set for every model apply, through the `skills_list` filter and the
  `skill_view` refusal.

## Disclosure

- **Runs commands, only when you run `hermes court trial`:** `hermes profile create/delete`,
  `hermes -p … config set/get` and `hermes -p … tools disable` for the replay profiles,
  `hermes -p <replay profile> chat -Q --oneshot` for each run, and `docker` (or `podman`) `version`,
  `ps`, `inspect`, `exec` (the task's check) and `rm -f` on the replay sandboxes.
- **Creates two profiles while it tests a skill,** `court-<id>-with` and `court-<id>-without`, where
  `<id>` is derived from your profile's name. They are cloned with `hermes profile create
  --clone-from` (config, API keys, SOUL, memory, skills; never messaging channels), rebuilt for each
  skill and deleted with their sandboxes at the end (`--keep-sandboxes` keeps the last pair). It
  refuses to touch a profile with one of those names that it did not create.
- **Replays are real agent runs.** They spend tokens with your model provider (`--budget`, default
  $3, stops new runs) and send the replayed task text to it, as the original session did. They run
  as single-query sessions, not with `-z`: approvals are set to manual and an approval prompt is
  denied (`approvals.single_query_mode: deny`), never auto-approved. Hermes launch flags in your
  environment (`HERMES_YOLO_MODE`, `HERMES_ACCEPT_HOOKS` and the like) are not passed to replays.
  Shell commands run inside the Docker sandbox, where Hermes does not ask for command approval.
- **What a replay profile changes from your profile:** Docker backend with one fresh container per
  run; no host directories mounted, no extra `docker run` flags, no container env, no forwarded host
  env and no `env_passthrough`; egress proxy off (its credentials belong to your profile and are not
  cloned); no MCP servers, external memory provider, other plugins or shell hooks; skill edits
  staged rather than applied; and these toolsets turned off: code execution, computer use, browser,
  cron jobs, memory, session search, connections, kanban, image, video and speech generation.
  Environment variables that a loaded skill declares it needs are still passed into the sandbox, as
  in normal Hermes use, and web search uses your own search keys.
- **Reads:** session history through Hermes' read-only `SessionDB`, the skill catalog through Hermes'
  public skill helpers and the skills directory's `.usage.json`, and `skills.disabled` /
  `model.default` through `hermes config get`.
- **Writes:** only its own `plugin-data/skill-court/data.db` and run scratch files. It never edits or
  deletes a skill.
- **No network calls of its own** and no telemetry.

## License

MIT
