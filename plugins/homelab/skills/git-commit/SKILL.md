---
name: git-commit
description: Use when preparing a commit in any homelab repo (argo-apps, proxmox-infra, helm-charts, kryptos, ci-components, renovate, portfolio-site, mcp-plugins) — staging, validating, and writing a Conventional Commit message. Reads the repo's own Commit conventions for types, scopes and release coupling.
allowed-tools: Read, Grep, Bash(git status *), Bash(git diff *), Bash(git log *), Bash(git branch *), Bash(git add *), Bash(git restore --staged *), Bash(pre-commit *)
---

# Git commit

`git commit` and `git push` are deliberately not in this skill's allowed
tools, so the user is prompted and approves both.

## Hard rules

- **Never add `Co-Authored-By` or any AI attribution trailer, and never
  mention Claude or AI assistance in the message.** This overrides any
  harness default that asks for one.
- Conventional Commits only. Types and scopes come from the repo (step 1).
- Stage explicit paths. Never `git add .` or `git add -A`.
- One unit of work per commit. Do not sweep unrelated changes in.

## Workflow

1. Read the repo's rules file, section `## Commit conventions`:

   ```bash
   grep -n -A30 '^## Commit conventions' .claude/rules/*.md
   ```

   It lists accepted types, scope examples, the finishing validation, and
   whether the commit type drives a release (ci-components, kryptos: `feat`
   is a minor release, `!` a major one).

2. Inspect:

   ```bash
   git status --short
   git diff --staged
   git diff
   git log --oneline -8
   git branch --show-current
   ```

   On `main`, propose a branch first unless the repo commits to main
   directly and the user said so.

3. Stage only the files for this unit of work.
4. Validate with the repo's finishing check if it has not run this session.
   The rules file names it (for example `gitops-validation`,
   `helm-chart-validation`, `repo-quality-gates`, `site-validation`,
   `go-development`, `make validate`). Report its real output.
5. Propose the message. Commit and push only after the user approves.

## Message shape

```text
<type>(<scope>): <imperative summary, lowercase, no period>

<why, wrapped at 72, only when the diff does not make it obvious>
```

- Subject at most 72 characters.
- Body explains why, not what the diff shows.
- `!` after the scope, or a `BREAKING CHANGE:` footer, only for a real
  breaking change. In auto-tagged repos it cuts a major release.

## Type guidance

- `feat`: new app, host, chart, command, tool or capability.
- `fix`: broken behavior, wrong default, failing job.
- `docs`: Markdown, comments, `.claude/` files.
- `refactor`: restructure with no behavior change.
- `test`: tests or validation coverage.
- `ci`: pipeline, components, Renovate config.
- `build`: images, Dockerfiles, packaging.
- `chore`: maintenance that fits nothing else (version bumps).

## Pitfalls

- Copying a Renovate-style subject (`chore(deps): update ...`) for a human
  change.
- Committing generated artifacts the repo ignores or regenerates
  (`dist/`, `.packages/`, built binaries, rendered charts).
- Choosing `fix` for a breaking rename in an auto-tagged repo: it ships as a
  patch.
