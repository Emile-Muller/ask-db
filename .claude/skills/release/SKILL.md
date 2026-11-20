---
name: release
description: Cut a release in a single commit - run all tests, stage all changes, update VERSION and the README version history, commit once with a Conventional Commits message (feat/fix/docs/... picked from the changes) and create the annotated vX.Y.Z tag. Use when the user asks to release or cut a version.
argument-hint: "[major|minor|patch]"
disable-model-invocation: true
---

# Release workflow

Bump type requested: `$ARGUMENTS`

The whole release is **one commit**: pending work + `VERSION` + README version history, then a tag on it.
Run the steps in order. If a step fails, **stop** and report it: no commit, no tag. Never push unless asked.

## 0. Parse the argument
- Allowed: `major`, `minor`, `patch` (case-insensitive).
- Empty: ask the `version-manager` subagent to propose a bump type, then confirm it with the user.
- Anything else: stop and list the allowed values.

## 1. Pre-flight
- Branch is `main` (`git branch --show-current`); otherwise ask before continuing.
- There is something to release: uncommitted changes (`git status --porcelain`) or commits since the
  last tag (`git log <last-tag>..HEAD --oneline`). If neither, stop.

## 2. Run all tests
- Use the project venv: `.\.venv\Scripts\python.exe`.
- If tests exist (`tests/`, `test_*.py`, `*_test.py`): `.\.venv\Scripts\python.exe -m pytest -q`.
  - pytest missing: tell the user and stop (do not install without asking).
  - Any failure or error: stop and show the failing tests.
- No tests: say so clearly and ask whether to release anyway. Never claim tests passed.
- `CLAUDE.md` safety checks (must pass):
  - Every `duckdb.connect(` outside `src/olist_db/build.py` and the temporary-database fixture in
    `tests/test_executor.py` uses `read_only=True`.
  - No hardcoded secrets (grep for `api_key\s*=\s*["']`, `sk-`, `token\s*=\s*["']`, `password\s*=\s*["']`).
  - `.env` and `.streamlit/secrets.toml` are in `.gitignore`.

## 3. Update VERSION and README (version-manager subagent)
Call the `version-manager` subagent (Agent tool, `subagent_type: "version-manager"`) with the bump type.
It validates the new version (SemVer, greater than the latest tag, tag not taken), updates `VERSION`,
and writes the `## Version history` entry from the commits since the last tag **and** the uncommitted changes.
It does not stage, commit or tag.

Check its result: `VERSION` holds the new version and README has the new entry at the top of the history.
If other docs (`README.md` usage/setup, `CLAUDE.md`, `data/README.md`) no longer match the code, fix them now
so they go in the same commit.

## 4. Stage everything
- `git add -A`
- Review what is staged: `git diff --cached --stat` and `git diff --cached --name-only`.
- Safety: if a staged file is a secret or local-only file (`.env`, `.streamlit/secrets.toml`, `.venv/`,
  `*.duckdb`, raw CSVs under `data/raw/`) or a diff contains a key/token, stop: unstage it
  (`git restore --staged <path>`) and ask the user (it probably belongs in `.gitignore`).

## 5. Single commit
The release is still **one commit**, but its message follows
[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) and describes what the
release actually changes, instead of always being `chore(release)`.

### Pick the type
Look at everything staged (commits since the last tag are already in history; describe the staged diff
and the README history bullets). Pick the **one** type that best describes the release; if several
apply, take the highest in this order:

| Type | Use when the release mainly... |
|---|---|
| `feat` | adds a user-visible capability (new CLI command/flag, new allowed query shape, new layer check) |
| `fix` | fixes a bug, incl. a closed guardrail bypass |
| `perf` | improves performance without changing behavior |
| `refactor` | restructures code without changing behavior |
| `test` | only adds or changes tests / red-team cases |
| `docs` | only changes docs (`README.md`, `CLAUDE.md`, `data/README.md`, skill/agent docs) |
| `build` | changes dependencies, packaging, the DB build (`src/olist_db/`) |
| `ci` | changes CI or pre-commit config |
| `style` | formatting only |
| `chore` | anything else (tooling, `.claude/` config); only when nothing above fits |

- **Scope** (optional): the main area touched, lowercase, e.g. `validator`, `executor`, `input-screen`,
  `cli`, `olist-db`, `skills`. Omit it when the release spans several areas.
- **Breaking change** (bump is `major`): add `!` after the type/scope and a `BREAKING CHANGE:` footer.
- Consistency with the bump: `feat` → at least `minor`; `!` → `major`. If the requested bump does not
  match (e.g. `patch` with a `feat`), tell the user and ask which to keep before committing.

### Message format
Follow the session's commit attribution rules (trailer lines go last, after the footers):
```
<type>[(<scope>)][!]: <summary in imperative mood, lowercase, no period, ≤ 72 chars>

- <same bullets as the README history entry>

[BREAKING CHANGE: <what breaks and how to migrate>]
Release: X.Y.Z
```
Examples: `feat(validator): allow window functions in SELECT`, `fix(executor)!: enforce row cap on CTEs`,
`docs: document the review CLI command`.

- Pass the multi-line message through a file, never inline:
  1. Write it with the Write tool to `commit_msg.txt` in the session scratchpad directory (not the repo,
     so `git status` stays clean).
  2. `git commit -F "<absolute path to commit_msg.txt>"`.
  - Do **not** use `git commit -F - @'...'@` in PowerShell: the here-string becomes a pathspec argument,
    not stdin, so git fails with `pathspec ... did not match any file(s)`. Avoid stacking multiple `-m`
    flags too (bullets with backticks/quotes break the quoting).
- The pre-commit hook (ruff lint + format) runs on this commit. If it fails or modifies files, the
  commit is not made: show the output, and if it only reformatted/auto-fixed files, re-stage them
  (`git add -A`) and commit again once. Never bypass it with `--no-verify`.
- After committing, `git status --porcelain` must be empty.

## 6. Tag
- Check `git show HEAD:VERSION` equals `X.Y.Z` and `git tag -l "vX.Y.Z"` is empty.
- `git tag -a vX.Y.Z -m "Release X.Y.Z"`.
- Verify `git rev-list -n1 vX.Y.Z` equals `git rev-parse HEAD`.
- Never move, delete or overwrite an existing tag unless explicitly asked.

## 7. Report
Checklist:
- [ ] Tests pass (or: no tests, user approved)
- [ ] All changes staged and committed in one commit, Conventional Commits message with the right type
- [ ] `VERSION` and README version history updated
- [ ] Tag `vX.Y.Z` created on `HEAD`

End with: old → new version, commit hash and subject line, tag, and the publish command (do not run it unless asked):
`git push origin main --follow-tags`
