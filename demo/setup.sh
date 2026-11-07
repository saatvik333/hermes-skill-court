#!/usr/bin/env bash
# Install the demo skill library into a Hermes profile and give each skill three checked tasks.
#
#   demo/setup.sh <profile>
#
# The four skills stand in for skills the agent wrote itself; `curator adopt` marks them as
# agent-written so `hermes court trial --all` picks them up, and `curator pin` keeps the
# background curator from archiving them. Use a lab profile with the plugin enabled, not the
# one you work in. Safe to run again: tasks already present are kept as they are.
set -euo pipefail

profile=${1:?usage: demo/setup.sh <profile>}
here=$(cd "$(dirname "$0")" && pwd)
home=$(dirname "$(hermes -p "$profile" config path)")
dest="$home/skills/demo"

mkdir -p "$dest"
cp -R "$here"/skills/* "$dest"/
names=(team-changelog sales-csv-totals python-venv-setup json-format)
hermes -p "$profile" curator adopt "${names[@]}"
for name in "${names[@]}"; do hermes -p "$profile" curator pin "$name"; done

# task <skill> <prompt> <check>: the check runs in the replay's sandbox afterwards; exit 0 = pass.
task() { hermes -p "$profile" court tasks add "$1" --prompt "$2" --check "$3" || true; }

# team-changelog: a convention the model cannot guess. Expect: helps.
task team-changelog \
  "Create /workspace/CHANGELOG.md containing just the line '# Changelog' if it does not exist. Then add an entry for ticket ABC-12, which fixes the login timeout. Follow the team's changelog conventions." \
  "$(cat <<'CHECK'
grep -qx '## Next release' /workspace/CHANGELOG.md && grep -Eqx '\* ABC-12: [a-z][a-z ]*login timeout' /workspace/CHANGELOG.md
CHECK
)"
task team-changelog \
  "Log ticket ABC-31 in /workspace/CHANGELOG.md (create the file with just the line '# Changelog' if it is missing): the export button now handles empty reports. Follow the team's changelog conventions." \
  "$(cat <<'CHECK'
grep -qx '## Next release' /workspace/CHANGELOG.md && grep -Eqx '\* ABC-31: [a-z][a-z ]*empty reports?' /workspace/CHANGELOG.md
CHECK
)"
task team-changelog \
  "Our release notes file /workspace/CHANGELOG.md does not exist yet. Create it with a '# Changelog' title and record ticket ABC-7, which removes the deprecated v1 API, following the team's changelog conventions." \
  "$(cat <<'CHECK'
grep -qx '## Next release' /workspace/CHANGELOG.md && grep -Eqx '\* ABC-7: [a-z][a-z0-9 ]*v1 api' /workspace/CHANGELOG.md
CHECK
)"

# sales-csv-totals: a lesson overgeneralised from one export ("amounts are in cents").
task sales-csv-totals \
  "Here is this week's sales export from our shop. Save it to /workspace/sales.csv exactly as given, then work out the total sales for the week and write just that number to /workspace/total.txt.

id,item,amount
1,keyboard,120
2,mouse,45
3,monitor,300" \
  "$(cat <<'CHECK'
grep -Eqx '465(\.0+)?' /workspace/total.txt
CHECK
)"
task sales-csv-totals \
  "Our March sales export is below. Save it to /workspace/march.csv exactly as given, then write the total sales for March, as just a number, to /workspace/total.txt.

id,item,amount
1,desk,250
2,chair,90
3,lamp,35" \
  "$(cat <<'CHECK'
grep -Eqx '375(\.0+)?' /workspace/total.txt
CHECK
)"
task sales-csv-totals \
  "Here is today's sales export from our shop. Save it to /workspace/today.csv exactly as given and write today's total sales, as just a number, to /workspace/total.txt.

id,item,amount
1,cable,12
2,charger,30
3,case,18" \
  "$(cat <<'CHECK'
grep -Eqx '60(\.0+)?' /workspace/total.txt
CHECK
)"

# python-venv-setup: a setup ritual learned in another repo (extra work for nothing).
task python-venv-setup \
  "In this project's sandbox, write a Python script /workspace/fizz.py that prints FizzBuzz for the numbers 1 to 15, one per line. Run it and save its output to /workspace/fizz.txt." \
  "$(cat <<'CHECK'
python3 /workspace/fizz.py | cmp -s - /workspace/fizz.txt && sed -n 15p /workspace/fizz.txt | grep -qx FizzBuzz
CHECK
)"
task python-venv-setup \
  "In this project's sandbox, write /workspace/primes.py that prints the prime numbers below 30, one per line. Run it and save its output to /workspace/primes.txt." \
  "$(cat <<'CHECK'
[ "$(tr '\n' ' ' < /workspace/primes.txt | xargs)" = "2 3 5 7 11 13 17 19 23 29" ]
CHECK
)"
task python-venv-setup \
  "In this project's sandbox, create /workspace/text.txt containing 'the quick brown fox jumps over the lazy dog'. Write /workspace/wordcount.py that prints the number of words in that file, run it, and save its output to /workspace/count.txt." \
  "$(cat <<'CHECK'
grep -Eqx '[[:space:]]*9[[:space:]]*' /workspace/count.txt
CHECK
)"

# json-format: a harmless tip. Expect: neutral.
task json-format \
  'In our sandbox, create /workspace/data.json containing exactly {"b":1,"a":[1,2]}. Then write a pretty-printed copy with 2-space indentation and keys in alphabetical order to /workspace/pretty.json.' \
  "$(cat <<'CHECK'
python3 -c 'import json,sys; t=open("/workspace/pretty.json").read(); k=[x.strip()[1] for x in t.splitlines() if x.startswith("  \"")]; sys.exit(not (json.loads(t)=={"a":[1,2],"b":1} and k==["a","b"]))'
CHECK
)"
task json-format \
  'In our sandbox, create /workspace/config.json containing exactly {"z":true,"m":{"b":2,"a":1}}. Check whether it is valid JSON and write VALID or INVALID to /workspace/status.txt.' \
  "$(cat <<'CHECK'
grep -qx VALID /workspace/status.txt
CHECK
)"
task json-format \
  'In our sandbox, create /workspace/broken.json containing exactly {"a": 1,} and check whether it is valid JSON. Write VALID or INVALID to /workspace/status.txt.' \
  "$(cat <<'CHECK'
grep -qx INVALID /workspace/status.txt
CHECK
)"

echo "Demo library installed in $dest. Try: hermes -p $profile court trial --all --tasks 3 --apply"
