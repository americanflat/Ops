# skill-sop-builder

Turns any Americanflat Claude skill into a Standard Operating Procedure (SOP) that a teammate
can follow when the skill's owner isn't available, and files it in the company **📕 SOPs**
Notion database.

## How to use it

In Claude, paste the skill you want documented. Any of these forms works:

```
Build an SOP for skill-shipping-cost-report
Make an SOP from americanflat/skill-daily-sales-check
Write a runbook for https://github.com/americanflat/skill-foo
```

Claude then:

1. **Asks where to file the SOP**, and which department it belongs to. The recommendation is
   the SOPs database, under your name.
2. **Checks for an existing SOP** for the same skill. If there is one, it asks whether to
   amend it, replace it, or leave it alone.
3. **Reads the skill**: its instructions, settings, changelog, scripts, and any related
   skills.
4. **Writes the SOP** in the house "Easy SOP" layout, with Status set to **In Review**:
   - **Part A — Run it in Claude:** the exact prompt and what "done" looks like.
   - **Part B — When the owner is unavailable:** access needed, where inputs come from, a
     step-by-step run, troubleshooting, a manual fallback, and escalation.
5. **Replies with a link to the page**, plus a list of open questions for the skill's owner.

Anything the skill's files don't say is marked **⚠ Unknown** instead of being guessed.

## What it needs

- **Notion** connected, with access to the SOPs database. Without Notion, it offers a
  Markdown file instead.
- **GitHub** access to the skill's repository, for skills that live on GitHub.

## What it changes

It creates or updates **one Notion page per run**, and only after you've chosen where it
goes. It asks before amending or replacing an existing SOP. It never deletes pages, and it
doesn't change the skill it documents.

## After it runs

The SOP starts as **In Review**. To make it Live:

1. Answer the open questions at the bottom of the page.
2. Add a Loom walkthrough to the Video section, and tick **Follow Easy SOP Guideline**.
3. Set Status to **Live**.

## Files

| File | Purpose |
|---|---|
| `SKILL.md` | Instructions Claude follows |
| `templates/sop-template.md` | The SOP structure |
| `examples/SOP-shipping-cost-report.md` | A worked example |
| `CHANGELOG.md` | Release history |

Owner: Anthony <anthony@americanflat.com>
