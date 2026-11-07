# Demo video script (under 90 seconds)

Record on a lab profile (`courtlab`) after `demo/setup.sh courtlab`. The figures below are from the
runs on 8 Nov 2026 (Hermes 0.23.0); a fresh recording can differ by a task or two, so read the
numbers off your own screen.

| Time | Screen | Caption / voice-over |
|---|---|---|
| 0–8 s | `hermes -p courtlab skills list`, the four demo skills highlighted | "Hermes writes its own skills. Nothing checks whether they help." |
| 8–30 s | `hermes -p courtlab court trial --all --tasks 3 --apply`, sped up; per-run lines scroll, with and without each skill, each run in its own Docker sandbox | "Skill Court runs every task twice, with the skill and without it, and checks the result inside the sandbox." |
| 30–45 s | The final table: `team-changelog` helps (0% → 100%), `sales-csv-totals` hurts (100% → 0%, quarantined), the other two neutral | "The team convention helps. The rule 'amounts are in cents', learned from one export, turns every total wrong, so it is quarantined." |
| 45–58 s | `hermes -p courtlab chat -Q --oneshot -q "Without calling any tools, list the skills in your <available_skills> index under the demo category"`: `sales-csv-totals` is gone; then `ls ~/.hermes/profiles/courtlab/skills/demo` still shows it | "The next session no longer sees it. The file is untouched." |
| 58–75 s | The same trial with `--model claude-haiku-5-5 --provider anthropic`, then the Desktop **Skill Court** page with both models side by side | "Verdicts are per model. On Haiku the same skill was not consistently harmful, so Haiku still sees it." |
| 75–88 s | Desktop: press **Release** on `sales-csv-totals`, then `hermes -p courtlab court list` | "One click brings it back. Skill Court: evidence instead of self-assessment." |

Post on X tagging @NousResearch, with the catalog PR link and https://github.com/saatvik333/hermes-skill-court,
then submit both links in the hackathon form.
