---
name: release
description: Cut a release in a single commit - run all tests, stage all changes, update VERSION and the README version history, commit once and create the annotated vX.Y.Z tag. Use when the user asks to release or cut a version.
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
  - Every `duckdb.connect(` outside `agentic_query/build_db.py` uses `read_only=True`.
  - No hardcoded secrets (grep for `api_key\s*=\s*["']`, `sk-`, `token\s*=\s*["']`, `password\s*=\s*["']`).
  - `.env` and `.streamlit/secrets.toml` are in `.gitignore`.

## 3. Update VERSION and README (version-manager subagent)
Call the `version-manager` subagent (Agent tool, `subagent_type: "version-manager"`) with the bump type.
It validates the new version (SemVer, greater than the latest tag, tag not taken), updates `VERSION`,
and writes the `## Version history` entry from the commits since the last tag **and** the uncommitted changes.
It does not stage, commit or tag.

Check its result: `VERSION` holds the new version and README has the new entry at the top of the history.
If other docs (`README.md` usage/setup, `CLAUDE.md`, `data/data_source.md`) no longer match the code, fix them now
so they go in the same commit.

## 4. Stage everything
- `git add -A`
- Review what is staged: `git diff --cached --stat` and `git diff --cached --name-only`.
- Safety: if a staged file is a secret or local-only file (`.env`, `.streamlit/secrets.toml`, `.venv/`,
  `*.duckdb`, raw CSVs under `data/`) or a diff contains a key/token, stop: unstage it
  (`git restore --staged <path>`) and ask the user (it probably belongs in `.gitignore`).

## 5. Single commit
- Message (Conventional Commits, follow the session's commit attribution rules):
  ```
  chore(release): X.Y.Z

  - <same bullets as the README history entry>
  ```
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
- [ ] All changes staged and committed in one commit
- [ ] `VERSION` and README version history updated
- [ ] Tag `vX.Y.Z` created on `HEAD`

End with: old → new version, commit hash, tag, and the publish command (do not run it unless asked):
`git push origin main --follow-tags`
