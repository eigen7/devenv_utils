# Shared Claude Code skills

Skills in this directory are shared across all devenv_utils consumers. Each
`<name>/SKILL.md` here is the canonical skill: its frontmatter and body are
the single source of truth.

Claude Code only discovers skills under the consumer repo's own
`.claude/skills/`, so a consumer adopts a shared skill by adding a **thin
pointer skill** there:

1. Create `.claude/skills/<name>/SKILL.md` in the consumer repo.
2. Copy the frontmatter (`name`, `description`) verbatim from the canonical
   file — the harness reads the description from the consumer's file to
   decide when the skill applies.
3. Give it a two-line body deferring to the canonical file:

   ```markdown
   ---
   name: <name>
   description: <copied verbatim from the canonical SKILL.md>
   ---

   This skill is shared across devenv_utils consumers. Read
   submodules/devenv_utils/skills/<name>/SKILL.md and follow it as if its
   contents appeared here.
   ```

When a canonical skill's description changes, each consumer's pointer file
must be updated to match as part of (or following) the pointer bump that
brings in the change.
