---
name: team-changelog
description: Use when adding entries to CHANGELOG.md in this team's repos (section name, line format, ticket IDs).
version: 1.0.0
---
# Team changelog convention

Learned when the user corrected the v2.3 release notes.

- Unreleased changes go under a `## Next release` heading directly below the `# Changelog` title.
  Create the heading if it is missing. Never use `Unreleased` or a date.
- One line per change, in this exact form: `* TICKET-ID: summary in lower case`
  (asterisk bullet, ticket ID, colon, lower-case summary, no trailing period).
- Newest entry first within the section.
