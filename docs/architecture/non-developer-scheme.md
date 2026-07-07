# In plain words (for a non-technical operator)

## Who does what
- **Telegram bot** — the "secretary on the phone." It receives a command, checks it really is the
  operator, and passes the task onward. Its only key is the login to the bot itself. If it is
  compromised, the other services' keys stay out of reach — it never holds them. It is not harmless,
  though: whoever controls it can still place tasks on the queue for the coder to run.
- **Coder (claude-worker)** — the "workshop craftsman." It takes a task and writes/edits code on the
  remote machine itself. It can save work into the project's working folder without extra questions.
- **Integrations worker (integrations-worker)** — the "personal assistant." It works with notes and
  tasks (via a live Notion/Todoist connection in the shipped entry point). Mail is a planned addition,
  not included here; the planned rule is read-only mail with sending only after an explicit "yes."

## Where the keys live
All real keys live in one protected "vault" (a secrets manager). The programs never see the keys
themselves — only their names. The most important key (the login to Claude) lives on the remote
machine's disk, not in the vault and not in the code.

## Why a remote machine (VPS) is needed
So you can code **from a phone, even when your own computer is off**. That needs an "always-on"
helper — that is the VPS. A home PC is not suitable: it gets switched off.

## What the Telegram bot does versus the coder
- Bot: received a command -> passed it on. It does not touch code itself.
- Coder: received it -> wrote/fixed it -> saved to a working branch.

## Why routine code-saving does not ask for confirmation
So you are not interrupted over every small thing. Routine work (editing, saving to the working
folder) proceeds on its own. Confirmation is requested only where an action could do real harm.

## What should ask for confirmation (target design)
This is the intended policy. In the shipped default the per-action Allow/Deny prompt is turned off (the
coding worker runs in a "don't ask" mode and the approval queue is not yet connected), so treat this as
the design target, not an active guarantee.
- Sending a message.
- Writing to the project's "main" branch.
- Deleting files/branches/data, rewriting history.
- Changing keys/security settings.

## How this differs from a managed platform
A managed platform is a "serviced rental apartment" that periodically restarts itself and wipes
temporary data. A VPS is your permanent "apartment": everything is under your control, nothing
wipes itself, but you also have to keep order yourself (which we automate).

## Where the code lives and how it syncs (in plain words)
- **GitHub — the shared "cabinet with the reference copy."** The single source of truth; both your
  computer and the remote machine (VPS) reconcile against it.
- **Your PC puts changes straight into GitHub** — directly, with its own access.
- **The VPS also puts changes straight into GitHub** — with its own, separate access.
- **The VPS is NOT a middleman between the PC and GitHub.** They both talk to GitHub themselves, not
  through each other.
- **Routine coding through the chatbot goes into a working branch, not the main branch, and without
  confirmation.** A "branch" is a draft copy of the project where you can work without touching the
  main version.
- **main — the main branch, a protected zone.** The finished, "live" version; you do not write there
  casually.
- **A pull request — a review window** before changes from a working branch reach main: you see what
  changes, and only then approve.
- **How the PC learns about the VPS's work:** on the PC you "fetch the latest from GitHub" and see
  what the VPS produced.
- **How the VPS learns about your PC work:** the VPS automatically fetches the latest from GitHub.
