# CLAUDE.md

## End-of-session log (do this every session, without being asked)

At the end of every working session, update `project_log.md` in the root of this repository. Create it if it doesn't exist.

- **Append** a new dated entry at the bottom. Never rewrite or delete earlier entries. If an earlier entry turned out to be wrong, say so in the new entry.
- Head each entry with the date and a few words on the topic, e.g. `## 2026-10-07 — 1077A/1077B rental pages`.
- Each entry covers:
  - **Worked on:** what was done, with the file paths and branch involved.
  - **Decisions and why:** each key decision and the reason for it.
  - **Status:** what is finished, what is live, and what isn't.
  - **Blockers and open questions:** anything waiting on the owner or on access, and anything still undecided.
  - **Next steps:** the concrete next actions, in order.
- `project_log.md` is the single source of truth for this project. Write it so that someone with no prior context can get fully caught up, including the owner reading it on a phone away from this machine:
  - Use short paragraphs and bullet lists, plain words and no unexplained jargon.
  - Give full links and file paths, and no wide tables.
- Commit and push `project_log.md` along with the session's other work, so it can be read on GitHub.
