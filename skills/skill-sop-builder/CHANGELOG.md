# Changelog

All notable changes to this skill are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-23

Lessons from the first test run, on `shipping-cost-report`. That skill turned out not to be on
GitHub at all.

- **Finds skills that aren't in a `skill-*` repo.** Tries the `americanflat-skill-*` naming
  too, then checks `ai-skills-registry`, `skill-candidates` branches, and the org-installed
  copy in Claude, and labels the SOP's source accordingly.
- **Reads related skills.** An upstream "download the inputs" skill is usually where the
  answer to "where do the inputs come from?" lives.
- **Honest manual fallback and defect notes.** Separates what the script produces from what
  Claude adds, and records likely bugs found while reading as open questions.
- **Example SOP** in `examples/SOP-shipping-cost-report.md`.

## [0.1.0] - 2026-10-23

First draft, ahead of review through the Builder create → publish flow.

- **Paste a skill name, repo, or GitHub link; get an SOP.** Resolves the input to an
  `americanflat/skill-*` repo, reads SKILL.md, skill.toml, CHANGELOG, README and bundled
  scripts, and writes a two-part SOP from `templates/sop-template.md`.
- **Part B covers the owner being out.** Prerequisites, where each input comes from, exact
  prompts, output checks, troubleshooting, manual fallback, and escalation.
- **No guessing.** Anything the skill's files don't state is marked `⚠ Unknown` and listed
  as an open question for the owner, and the SOP header cites the version and commit it was
  built from so stale SOPs are easy to spot.
