# OpenMontage

**MANDATORY: Read `AGENT_GUIDE.md` before responding to ANY user message.**

Do not act on the user's request until you have read AGENT_GUIDE.md.
It contains routing rules that determine your first action based on what the user asked.
Skipping it WILL cause you to take the wrong action.

There are no instructions in this file. All instructions are in AGENT_GUIDE.md.

## ECC task guidance (Vandam integration)

Read `.ecc/project.json` for the project-specific skills and restrictions. ECC skills are namespaced `ecc-*` in `.agents/skills` (Codex) and `.claude/skills` (Claude). The shared canonical installation is the sibling `claudevandam/integrations/ecc/`; keep repositories as siblings, or create missing links with its `ecc.py activate --project .` command after cloning.

At task intake run `python3 ../claudevandam/integrations/ecc/ecc.py select --project . --task "<task>"`, then load at most two selected skills. Run the same tool's `doctor --project .` to distinguish discovery from executable readiness. Existing project instructions, runtime source, approval gates and tests take precedence. No automatic observation hooks, separate memory service, provider changes or paid calls are enabled. A skill install is not a product acceptance test.
