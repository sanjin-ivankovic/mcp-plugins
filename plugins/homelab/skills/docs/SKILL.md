---
name: docs
description: Use when creating, editing, or reviewing documentation in any homelab repo — README.md, docs/*.md, per-app or per-chart READMEs, runbooks, NOTES.txt, doc comments, or a Markdown diff. Checks claims against the repo's own Docs contract.
allowed-tools: Read, Write, Edit, Grep, Glob, Bash(markdownlint-cli2 *), Bash(pre-commit *), Bash(git diff *), Bash(git status *), Bash(git log *), Bash(ls *), Bash(test *)
---

# Documentation (write and review)

## First: load the repo's contract

```bash
grep -n -A60 '^## Docs contract' .claude/rules/*.md
```

That section lists the canonical docs, what each owns, and the facts a
reviewer must check (retired hosts, current layout, exposure model, version
claims). Treat it as the checklist. If a fact there looks wrong against the
repo, fix the rules file too.

## Writing

- Lead with what exists and how it works. Back claims with repo paths.
- Read the files before writing any count, version, path or command. Never
  write a number you did not just verify.
- Concrete and operational. No "easy", "simply", "robust", "seamless".
- Headings state the point. Relative links to repo files.
- Human docs use ALL-CAPS names (`README.md`, `docs/ARCHITECTURE.md`,
  multi-word with hyphens). `.claude/**` and tool-mandated names stay
  lowercase. Never restyle vendored or `.archive/**` docs.
- Do not create summary, changelog or "what I did" docs unless asked.
- No secrets, tokens, keys or plaintext `Secret` examples, ever. Hostnames
  under `example.com` are fine in repos that sanitize on mirror; the repo's
  contract says which.
- Comments in code and config follow the user-level rule: current state
  only, keep the ones that prevent a failure.

## Reviewing

Flag in this order:

1. Wrong facts against the repo and its Docs contract (stale paths,
   retired hosts or services, wrong versions, commands that do not exist).
2. Secret exposure.
3. Commands that would fail in the current layout. Run them where safe.
4. Inflated claims (counts, coverage) not backed by files.
5. Reader friction (buried action, vague headings, wrong order).
6. Markdown that the linter would reject.

Number every finding: `**Issue 1: <title>**` with file:line, the problem,
and the replacement text. If nothing material is wrong, say so and say what
you checked.

## Validation

```bash
# repo config if present, otherwise ci-base's baked default applies in CI
markdownlint-cli2 --config .config/.markdownlint-cli2.jsonc <files>
```

Common lint traps across the homelab configs: 80-column lines (strict),
ASCII only, fence languages limited to the config's allow list (often no
`python` or `toml`; use `text`), dash lists, asterisk emphasis.

Check every relative link you wrote resolves:

```bash
grep -o '](\.[^)]*)' <file>   # then test -e each target from the file's dir
```
