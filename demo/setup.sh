#!/usr/bin/env bash
# Install the demo skill library into a Hermes profile and give each skill a checked replay task.
#
#   demo/setup.sh <profile>
#
# The four skills stand in for skills the agent wrote itself; `curator adopt` marks them as
# agent-written so `hermes court trial --all` picks them up, and `curator pin` keeps the
# background curator from archiving them. Use a lab profile, not the one you work in.
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
task() { hermes -p "$profile" court tasks add "$1" --prompt "$2" --check "$3"; }

task team-changelog \
  "Create /workspace/CHANGELOG.md containing just the line '# Changelog' if it does not exist. Then add an entry for ticket ABC-12, which fixes the login timeout. Follow the team's changelog conventions." \
  "$(cat <<'CHECK'
grep -qx '## Next release' /workspace/CHANGELOG.md && grep -Eqx '\* ABC-12: [a-z][a-z ]*login timeout' /workspace/CHANGELOG.md
CHECK
)"

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

task python-venv-setup \
  "In this project's sandbox, write a Python script /workspace/fizz.py that prints FizzBuzz for the numbers 1 to 15, one per line. Run it and save its output to /workspace/fizz.txt." \
  "$(cat <<'CHECK'
python3 /workspace/fizz.py | cmp -s - /workspace/fizz.txt && sed -n 15p /workspace/fizz.txt | grep -qx FizzBuzz
CHECK
)"

task json-format \
  'In our sandbox, create /workspace/data.json containing exactly {"b":1,"a":[1,2]}. Then write a pretty-printed copy with 2-space indentation and keys in alphabetical order to /workspace/pretty.json.' \
  "$(cat <<'CHECK'
python3 -c 'import json,sys; t=open("/workspace/pretty.json").read(); k=[x.strip()[1] for x in t.splitlines() if x.startswith("  \"")]; sys.exit(not (json.loads(t)=={"a":[1,2],"b":1} and k==["a","b"]))'
CHECK
)"

echo "Demo library installed in $dest. Try: hermes -p $profile court trial --all --reps 3"
