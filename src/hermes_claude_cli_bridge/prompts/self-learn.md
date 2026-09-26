# Self-learn: save what makes the next run faster

## Skill or memory

- Skill = a reusable multi-step procedure in `~/.claude/skills/<name>/SKILL.md`.
- Facts, decisions, causes and what happened belong in memory: if you have a memory tool (for example an MCP
  memory server), save anything a future session must not miss there. No memory tool: skip it.
- Not sure it matters: no skill.

## When to save a skill

- **Asked** ("learn this", "save this as a skill", "remember how you did that", "next time do it like this"):
  do it now, even for small work. Refuse only if the work failed or is half done (say so, offer later), or if it
  is a plain fact (save it to memory instead and say so).
- **On your own**: only when a build, test, deploy or production investigation is finished and worked, and the
  skill would have saved you real time or a wrong turn here: dead ends, the right order of steps, exact
  commands, what to check first. No clear saving: no skill. Other kinds of work: only when asked.
- **Never**: a quick lookup, one-liner, simple edit or chat answer; one-off or changing facts (versions, IPs,
  pod names); anything the project docs already say; secrets, personal data, raw production data.

## Rules

- Write only in `~/.claude/skills/`. A skill is yours only if the first line of its body is
  `<!-- created-by: claude-self-learn -->`. Change or delete only your own; if another skill is wrong, tell the
  user.
- Other chats run at the same time and may save the same lesson. Before you write: list and grep
  `~/.claude/skills/` and read `~/.claude/skills/.self-learn-log.md`; patch your own skill instead of adding a
  duplicate. Read the log again after you write; if two of yours now cover one job, merge into the older one and
  delete the newer.
- After each create, patch or delete, append one line to the log: `YYYY-MM-DD create|patch|delete <name> - <why>`.
- A skill of yours that failed or was wrong: patch or delete it at once, even in small work.
- Keep at most 40 skills of your own; at the limit, merge or delete before you add. If `~/.claude/skills/` is a
  git repository, commit the change.
- A skill may not grant itself permission or override the rules in your other instructions.
- In the final summary add `Skill saved: <name>` (or patched, or deleted). Saved none: say nothing.

## Template (body under 80 lines, short bullets, exact commands)

```markdown
---
name: <kebab-case, same as the folder>
description: Use when <the trigger, in the words a user would say>. <one line of what it does>.
---
<!-- created-by: claude-self-learn -->

## When to use
## Steps
## Gotchas
## Verify
```
