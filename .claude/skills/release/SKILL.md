---
name: release
description: Orchestrate a full release - run tests, bump the version (major/minor/patch), update docs, commit and create the git release tag. Use when the user asks to release, cut a version, or make a release commit.
argument-hint: "[major|minor|patch]"
disable-model-invocation: true
---

# Release workflow

Bump type requested: `$ARGUMENTS`

Run the steps **in order**. If a step fails, **stop**, report what failed, and do not continue
(no commit, no tag). Never push without explicit approval.

## 0. Parse the argument
- Allowed values: `major`, `minor`, `patch` (case-insensitive).
- Empty: ask the version-manager subagent to propose a bump type from the changes since the last tag,
  then confirm the proposal with the user before going on.
- Anything else: stop and tell the user the allowed values.

## 1. Pre-flight checks
- Current branch is `main` (`git branch --show-current`). If not, ask before continuing.
- Pending changes: if `git status --porcelain` is not empty, include them in the release by committing them now:
  1. Review every changed and untracked file (`git status --porcelain`, `git diff`, and read new files).
  2. Safety: never stage secrets or local-only files (`.env`, `.streamlit/secrets.toml`, `.venv/`, `*.duckdb`,
     raw data under `data/`, anything containing API keys or tokens). If such a file shows up as untracked,
     stop and ask the user (it probably belongs in `.gitignore`).
  3. Stage the reviewed files by explicit path (`git add <path> ...`), not `git add -A` / `git add .`.
  4. Commit with a Conventional Commits message that says what the changes are, e.g.
     `feat: add release skill and tag validation to version-manager`, with a short body listing each
     change when there are several. Use `feat:`, `fix:`, `docs:`, `refactor:`, `test:` or `chore:` to match the change
     (follow the commit attribution rules of the session). If the changes are unrelated, make one commit per group.
  5. Show the user the resulting commit(s) (`git log -n <count> --stat`).
- There are commits since the last tag: `git log <last-tag>..HEAD --oneline` (if there are no tags, all commits).
  If nothing changed, stop: there is nothing to release.

## 2. Run tests
- Use the project venv: `.\.venv\Scripts\python.exe` (Windows).
- If a `tests/` folder or `test_*.py` / `*_test.py` files exist, run:
  `.\.venv\Scripts\python.exe -m pytest -q`
  - If pytest is not installed, tell the user and stop (do not install packages without asking).
  - Any failure or error: stop and show the failing tests.
- If no tests exist: tell the user clearly and ask whether to release anyway. Do not claim tests passed.
- Safety checks from `CLAUDE.md` (must pass):
  - Every `duckdb.connect(` call outside `build_db.py` uses `read_only=True`.
  - No hardcoded secrets in tracked files (grep for patterns like `api_key\s*=\s*["']`, `sk-`, `token\s*=\s*["']`).
  - `.env` and `.streamlit/secrets.toml` are listed in `.gitignore`.

## 3. Bump the version (version-manager subagent)
Call the `version-manager` subagent (Agent tool, `subagent_type: "version-manager"`) with:
- the bump type,
- the commit list since the last tag (`git log <last-tag>..HEAD --oneline`),
- the instruction: bump `VERSION`, sync every other version reference, and add the new entry to
  `## Version history` in `README.md`. **Do not commit and do not tag yet.**

Then check the result yourself: `VERSION` holds the new version and
`grep -rn "<old-version>"` (excluding `.git`, `.venv`, `data/`, and past history entries) shows no stale reference.

## 4. Update documentation
Using the diff since the last tag (`git diff <last-tag>..HEAD`), update the docs so they match the code:
- `README.md`: setup, usage, features, file descriptions (not only the version history).
- `CLAUDE.md`: only if project rules or structure changed.
- `data/data_source.md`: only if data or tables changed.
Keep edits minimal and factual. If nothing besides the version history needs changing, say so.

## 5. Commit
- Show the user `git diff --stat` before committing.
- Stage only the release files (e.g. `git add VERSION README.md <other docs changed>`), not `git add -A`.
- Commit message: `chore: bump version to X.Y.Z` (follow the commit attribution rules of the session).

## 6. Tag (version-manager subagent)
Call the `version-manager` subagent again to create the tag `vX.Y.Z` on `HEAD`.
It must run all its tag validation checks (SemVer format, matches `VERSION`, no duplicate local/remote tag,
version greater than the latest tag, clean tree) and stop if any fails.

## 7. Verify and report
Confirm each success criterion and report it as a checklist:
- [ ] Version updated in all relevant files (`VERSION`, README version history, other references)
- [ ] All tests pass (or: no tests exist and the user approved releasing anyway)
- [ ] Documentation updated
- [ ] Release tag created: `git tag -l "vX.Y.Z"` lists it and `git rev-list -n1 vX.Y.Z` == `git rev-parse HEAD`

End with: old version → new version, commit hash, tag name, and the command to publish if the user wants:
`git push origin main --follow-tags` (do not run it unless asked).
