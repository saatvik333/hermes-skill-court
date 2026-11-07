# Demo video script (under 90 seconds)

Recorded on a lab profile (`courtlab`) with the demo library from `demo/setup.sh`. Every number on
screen comes from the real runs; replace the placeholders in brackets with the figures from your
recording before posting.

| Time | Screen | Voice-over / caption |
|---|---|---|
| 0–8 s | Terminal: `hermes -p courtlab skills list`, the four demo skills highlighted | "Hermes writes its own skills. Nobody checks whether they help." |
| 8–30 s | `hermes -p courtlab court trial --all --tasks 3 --apply`, sped up 8×; per-run lines scroll, both arms run in Docker | "Skill Court runs each task twice, with the skill and without it, in fresh Docker sandboxes, and checks the result inside the sandbox." |
| 30–45 s | The final table: `team-changelog` helps, `sales-csv-totals` hurts ([success with → without]), `python-venv-setup` hurts ([+N]% tokens), `json-format` neutral; "quarantined: yes" on the two | "One skill learned a convention: it helps. One learned 'amounts are in cents' from a single session: every total comes out wrong. One adds a ritual that burns tokens. Both get quarantined." |
| 45–58 s | `hermes -p courtlab -z "Without calling any tools, list the skills in your <available_skills> index under the demo category"` → only `json-format`, `team-changelog` | "The next session doesn't see them. The files are untouched." (cut to `git diff --stat` / `ls skills/demo`) |
| 58–75 s | Same trial with `--model claude-haiku-5-5`; then the Desktop **Skill Court** page with both models side by side | "Verdicts are per model. The same skill can hurt Sonnet and do nothing on Haiku, so it is hidden only where it hurts." |
| 75–88 s | Desktop: press **Release** on `sales-csv-totals`; the live announcement; `hermes court list` | "One click brings it back. Skill Court: proof, not self-assessment." |

Post on X tagging @NousResearch with the catalog PR link and the repo:
https://github.com/saatvik333/hermes-skill-court
