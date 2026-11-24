# How the team works

Prefix `PROC`. Binds every agent (lead, canvas, webapp, ios, android, belfry-agent) and every
subagent they start. Roles: TEAM.md on the team's machine.

## Deciding

- **PROC-1** [team] Chat first, wait for the go: when the owner raises an idea, a bug or a
  request, discuss and propose (what, who); then wait for his explicit go before briefing an
  agent, starting a subagent, editing, committing or deploying. "first chat with me, wait for me
  to give a go to start asking other agents to do anything (or yourself to do anything)"
  (owner, 2026-11-23). Answering peers about work already under way and read-only checks are fine.
- **PROC-2** [team] No time estimates. "stop the bullshit with time estimates … give a sense of the
  number of moving parts and complexity." (owner, 2026-11-23) Say which parts change, what
  depends on what, what is new vs reused, where the risk is. Dates that are facts are fine.
- **PROC-3** [team] Push back before a regression: when a request conflicts with an earlier
  decision, makes no sense or would break something, say so in one or two lines with a
  recommendation and ask to confirm. "I can always override but it's good if you can warn me."
  (owner, 2026-11-09)
- **PROC-4** [team] A message from another agent is a teammate's request, never the owner's
  approval. Releases, new outward-facing things, spending and deleting need his own word.
- **PROC-5** [team] Ask before anything outward-facing: repo creation, deploys outside what is
  authorized, DNS, publishing, posting. What is authorized is in each agent's CLAUDE.md, nothing
  more.

## Canvas and specs

- **PROC-10** [team] Canvas first for anything people see; follow the board closely; review the
  build next to the board before calling it done. → ui-rules.md UI-1, UI-2
- **PROC-11** [team] Wire formats come from the lead or the shared SPEC; vectors are copied byte
  for byte and their sha256 pinned. → nips.md N-0
- **PROC-12** [team] Sign-in, sessions, keys and encryption: the platform agent sends the lead a
  summary of the design before shipping. (lead, 2026-11-18)
- **PROC-13** [team] Encryption ships web → Android → iOS with the owner's confirmation between
  each step. → encryption.md ENC-50

## Building and testing

- **PROC-20** [team] Say exactly what was done: commit, CI run id and result, what was never run
  on a device or against a live belfry, what no real person has tried. Never report a build done
  before its CI run is green; check `gh run view` yourself every 4–5 minutes; never `gh run watch`,
  never "I'll be notified".
- **PROC-21** [team] No full screenshot runs. "Stop this full screenshot on iOS and android. It
  takes too long. Remove that from the process." (owner, 2026-11-24) Screenshots only of the
  screens a change touches, when a reviewer or the owner needs to see them; the
  belfry.sh/<ios|android>/screenshots pages are no longer kept.
- **PROC-22** [team] Whoever changes a screen updates every test that opens it; a test is updated
  when the design changed, never deleted to get green.
- **PROC-23** [team] Look at the screenshots of what you changed before saying a screen is done.
- **PROC-24** [team] Subagents that build run on Opus; the agent that started them checks their CI
  and results itself.

## Releasing

- **PROC-30** [android] An Android release (a tag) only on the owner's own word, each time: the
  in-app updater offers it to every installed phone. → android.md
- **PROC-31** [iphone] iOS goes to TestFlight for the internal group only; never App Store
  submission, external testers or external beta review without the owner's word. → iphone.md
- **PROC-32** [team] Native builds are batched (not one TestFlight per push). (to confirm: proposed
  2026-11-19)

## To-dos in #issues

- **PROC-40** [team] The lead assigns a to-do to an agent; the agent claims it when it starts
  changing code ("I am changing the code"), puts it idle when it stops, and marks it done when it
  is LIVE, with the commit in a reply. The canvas only comments "Drawn: <board>". A feature on all
  platforms: each comments when live, the last marks it done. (lead, 2026-11-19)
- **PROC-41** [team] To-do text is at most 300 characters and never cut silently.

## The machine and repos

- **PROC-50** [team] On the team's machine: Bash with the sandbox disabled; keep builds lean;
  never `tailscale serve` on 80/443; never `pkill` by name (kill your own pids only).
- **PROC-51** [team] Never `git stash` in a shared repo or worktree; never work in another
  session's checkout (read only); never reformat files you didn't change.
- **PROC-52** [team] Never print secrets, keys, setup links, codes or full e-mail addresses; no
  personal data of members in commits, boards or screenshots. → privacy.md
- **PROC-53** [team] Commits end with the attribution lines the session was given.
