# Demo video (about 75 seconds)

Everything after the title card is real footage from a scratch profile on Hermes 0.23.0 with Claude Sonnet 5.5, recorded with Skill Court 1.1 (where automatic trials had to be turned on; since 1.2 they are on by default). Terminal sessions were recorded with asciinema and rendered frame by frame. The Desktop page was captured from the live Webapp. Sped-up stretches carry a "×N" badge. Verdicts and numbers differ from run to run, so read them off your own screen.

| Screen | Caption |
|---|---|
| Title card | Skill Court: every lesson your agent writes goes on trial. |
| Chat: three sales exports totalled; the user accepts each | Hermes totals three sales exports. The user accepts each one. |
| Chat: the Q2 export, then the user's remark | Then one remark: "our sales exports are in cents". |
| Chat: "Self-improvement review: Skill 'sales-export-totals' created" | Hermes' own review saves it as a skill. |
| `auto.log`: paired replays in Docker sandboxes | Skill Court replays the session with and without the skill, in sandboxes. |
| `auto.log`: judged pairs and the HURTS verdict | With the skill, the totals the user accepted come out wrong. |
| `auto.log`: the drafted fix on trial | Then it drafts a fix and puts the fix on trial. |
| Next chat turn: the Skill Court notice (verdict, why, skill line, proven fix) | Next turn: hidden for this model, with the line that caused it. |
| `hermes court appeal show`: the diff | The fix keeps a bare-number answer bare, and passes the same tasks. |
| Desktop Skill Court page: the verdict, cause and proven fix | In Hermes Desktop: the verdict, the cause and a proven fix. |
| Desktop: the Apply fix dialog | Apply fix asks first and names the file it overwrites. |
| Desktop: after apply | Applied. Nothing quarantined; undo is one command. |
| `hermes court list` / `report` | Every step stays on the record, with undo. |
| End card: the install command and the repo | — |

## Reproducing it

1. Clone a profile and install the plugin. Nothing to turn on: the first chat turn shows one notice saying automatic trials are on ($1.00 a day of replays, 300,000 judge tokens) and how to stop them.
2. In a chat:
   - ask for the totals of three small sales CSVs ("Just the number"), then a Q2 export;
   - then say "No, our sales exports are in cents, not dollars."
3. With `skills.creation_nudge_interval` at 5, Hermes' review saves a skill after that turn.
4. The trial starts when the session ends (in Docker sandboxes, or answer-only without Docker); its verdict appears at the next turn.

Rehearsed with 1.2 on 8 Nov 2026 in a fresh clone with no Skill Court settings: the first turn showed the first-run notice once; Hermes' review saved `sales-export-totals`; the trial started as the session ended and found it `hurts` on Claude Sonnet 5.5 (100% → 0%, 5 judged), with a proven fix, for $0.18 of replays and 29,392 judge tokens; the verdict appeared at the next turn. Using the skill in a Claude Haiku 5.5 session then queued a re-check there (`neutral`, 83% → 67%), and its notice began "you used `sales-export-totals` on claude-haiku-5-5".

When the correction named only that one file ("that Q2 file is in cents"), Claude Sonnet 5.5 and Haiku 5.5 kept the rule to that file in all six rehearsals. The one we put on trial came out neutral.
