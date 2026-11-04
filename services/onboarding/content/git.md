# Git server

**{{git_host}}** is where participants and their agents keep code together: repositories, pull
requests, patches and issues, on Nostr. It is private to Open Source Village: only someone with an
active pass can read or write. Without a pass you see this page and nothing else.

## What you need

- An active pass, and your Nostr key: the one Belfry keeps for you (sign in to
  [your account]({{account}})) or your own.
- **ngit 3 or later**, the git plugin for Nostr: [ngit.dev/install](https://ngit.dev/install).
  It signs you in to the server with your key and adds the credential every git request needs.

## Getting started

1. Sign in with your key, once: `ngit account login`. With the key Belfry keeps for you, add
   `--bunker-url 'bunker://…'` from your account page (**Connect to Nostr App**).
2. Put a repository here: `ngit init --name "My project" --grasp-server {{git_host}} --private`
3. Send your work: `git push -u origin main`
4. Get someone's repository: `git clone nostr://<their npub>/<repository>`
5. Propose a change: `git push -u origin pr/my-change -o 'title=My change'`
6. Open an issue: `ngit issue create --title "…" --body "…"`

Use `--private` and name only **{{git_host}}** as the server, so the repository and the talk about
it stay in the village. Only a repository's maintainers push its branches; everyone else proposes
changes as `pr/…` branches.

## Agents

An agent can work here for you with your key and your rights, nothing more: give it a bunker URL
from your account page. The [agent guide]({{skill_url}}) (section 6, "Git") has the details.

| What | Where |
| --- | --- |
| Relay | `{{git_relay}}` |
| Repositories | `{{git_url}}/<npub>/<repository>.git` |
| Server | ngit-grasp in private mode (GRASP-08), run by Belfry |
