# Changelog

All notable changes to this skill are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [0.5.0] - 2026-10-23

An existing SOP is never overwritten silently.

- **Asks what to do with a duplicate: amend, replace, or leave alone.** The check runs at the
  start, before any work, so "leave it alone" costs nothing. Amend is recommended: it keeps
  what people added (Loom videos, screenshots, tips) and shows the proposed changes before
  writing. Replace shows what will be lost first, and keeps the same page and link.
- **Why now.** Rewriting an SOP someone has curated would throw away their edits.

## [0.4.0] - 2026-10-23

SOPs now go into the company 📕 SOPs database by default, in its house format.

- **Default destination is the SOPs database**, filed under the runner's name. The database's
  main view is grouped by Owner, so the SOP is created with Owner = the person running the
  skill and Status = In Review. The opening question also confirms the one Department, which
  drives the per-department views.
- **Matches the house "Easy SOP" layout.** Table of Contents, Purpose, Context and Video
  sections, with ⚠ Unknowns as callouts and a placeholder for the owner's Loom walkthrough.
- **Checks for an existing SOP** in the database before creating one, and offers to update
  it instead.
- **Why now.** The team keeps every SOP in one database; SOPs scattered across personal pages
  wouldn't be found.

## [0.3.0] - 2026-10-23

SOPs now land in Notion by default.

- **Asks where to put the page before doing anything else.** Recommends a page under the name
  of the person running the skill (found via their Notion profile and a search), or a new
  private page with their name if none exists. Other choices: private pages, or any page they
  link.
- **Publishes and verifies in Notion.** Creates the SOP as a child page, avoids duplicates by
  offering to update an existing SOP, and re-fetches the page before reporting success.
- **Markdown is the fallback** when Notion isn't connected, or when the user asks for a file.
- **Why now.** SOPs need to live where the team already keeps its docs.

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
