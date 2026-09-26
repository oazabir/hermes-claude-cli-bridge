# Subagents

Orchestrate: plan, decide, verify. Delegate the rest with the Agent tool and always set its `model`. If agents
with these names are defined, use them; otherwise use `general-purpose` (or `Explore` for search) with the model
shown.

| Work | Agent (model) |
|---|---|
| Design: architecture, system, UI/UX | `designer` (opus) |
| Code over ~20 lines | `coder` (sonnet) |
| Review every change: fresh eyes, diff and requirements only | `reviewer` (sonnet) |
| Commands, builds, tests, lint, logs | `runner` (haiku) |
| Code search | `search` (haiku) |

- Trivial work (one command, a one-line edit, one small file): do it yourself. At most 3 agents in parallel.
  A change is not done until the reviewer passes it.
- Give each agent the goal, paths, constraints and what "done" means. Ask for a summary back: result, exact
  errors, changed paths; no raw logs or whole files. Filter large output first; for over ~150K tokens of input
  use sonnet instead of haiku.
- Subagents do not start their own subagents.
