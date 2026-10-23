# SOP — {Skill display name}

| | |
|---|---|
| **Skill** | `{skill-name}` |
| **Source** | `{owner/repo}` @ `{commit SHA, short}` (tag `{vX.Y.Z}`) |
| **Skill version** | {version from skill.toml} |
| **Skill owner** | {name} · Backup: {name or ⚠ Unknown — confirm with owner} |
| **Cadence** | {on demand / daily at … / weekly on …} · Deadline: {if any} |
| **SOP generated** | {YYYY-MM-DD} — if the skill's version is newer than the one above, regenerate this SOP. |

## What this skill does

{One or two plain-language sentences: the business task, who uses the result, why it matters.}

**You'll need about:** {time estimate} · **Output:** {what it produces and where it lands}

---

## Part A — Run it in Claude

### Before you start

- [ ] Access to {Claude.ai / Claude Code / Claude desktop} with the `{skill-name}` skill enabled.
- [ ] Connectors signed in: {Slack, Gmail, Google Drive, BigQuery, …}
- [ ] Input files ready: {list} — see *Inputs* in Part B for where to get them.

### Steps

1. Open {where} and start a new conversation.
2. {Attach / upload the input files, if any.}
3. Type:

   ```
   {exact trigger prompt}
   ```

4. {What Claude will do / ask next.} When it asks {approval question}, check {what} and reply:

   ```
   {exact reply}
   ```

5. {Final step.} **Done when:** {observable end state, e.g. "the brief appears in #channel"}.

---

## Part B — When the owner is unavailable

### 1. Prerequisites and access

| What | Why it's needed | How to get it if you don't have it |
|---|---|---|
| {Connector / login / permission} | {step it's used in} | {who grants it / where} |

### 2. Inputs — where each one comes from

| Input | Where to find it | Format / must contain | Date range |
|---|---|---|---|
| {file or value} | {system, folder, channel, report path} | {file type, key columns} | {e.g. last Mon–Sun} |

### 3. Step-by-step run

1. **Gather inputs.** {Concrete instructions for pulling each input.}
2. **Sanity-check inputs** before running: {row counts, columns present, dates match}.
3. **Start Claude** and paste:

   ```
   {exact prompt, including date range and any context the owner usually supplies}
   ```

4. **What happens next:** {plain-language summary of what the skill does, in order}.
5. **Approval gates:** {each point where Claude asks permission, what to check, and how to answer}.
6. **Verify the output** — it's good if:
   - {check 1, e.g. total matches invoice total within $X}
   - {check 2, e.g. all three warehouses present}
   - {check 3, e.g. posted in #channel by HH:MM}
7. **Deliver / file it:** {where the result goes and who's told}.

### 4. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Claude doesn't pick up the skill / answers generically | Skill not enabled, or prompt too vague | Enable `{skill-name}` in Settings → Skills, or start the message with `/{skill-name}` |
| "Not connected" / permission error for {connector} | Connector not signed in for your account | Reconnect it in Settings → Connectors, then retry |
| {skill-specific symptom} | {cause from SKILL.md / CHANGELOG} | {fix} |
| Output looks wrong and you can't tell why | — | Don't send it. Save the inputs and output, escalate (below) |

### 5. Manual fallback (no Claude)

{If the materials support it: how to produce the result by hand or by running the bundled
script directly, with the exact command. Otherwise: "No manual fallback — if the skill can't
run, notify {stakeholders} that {output} will be late and escalate."}

### 6. Escalation

1. Skill owner: {name, Slack handle}
2. Backup: {name or ⚠ Unknown — confirm with owner}
3. Skill platform issues (skill won't load, repo access): Governors in **#ai-github-skills**

Tell them: what you ran, the exact prompt, the inputs used, and the error or the wrong output.

---

## Reference

- **Connectors / systems touched:** {list, with read vs write}
- **Bundled files:** {scripts/…, templates/…} — {one line each}
- **Recent changes worth knowing:** {top CHANGELOG entries, one line each}

## Open questions for the owner

- {Every ⚠ Unknown above, restated as a question.}
