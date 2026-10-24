# Maintaining the personal branch

`dev` follows the official `odysseus-dev/odysseus` development branch.
`personal` contains recovered, reviewed local changes. Keep runtime data,
credentials, configuration and backups outside this repository's history.

The initial `origin` is a local bare repository because this session could not
create a GitHub fork. It is a recovery destination, not a GitHub fork.
`upstream` fetches the official repository and has an intentionally disabled
push URL. Do not replace the active installation with this checkout.

## Review an upstream update

Start with a clean working directory. Use a fresh integration branch and keep
the validated personal branch as the recovery point:

```powershell
git status --short
git fetch upstream
git switch dev
git merge --ff-only upstream/dev
git switch personal
git branch backup/personal-before-update
git switch -c integration/upstream-update
git merge dev
```

Choose unused names for subsequent runs. Resolve conflicts on the integration
branch. Inspect security-sensitive changes in both branches, run targeted and
regression tests, and compare failures with an untouched upstream checkout.
Abort an unfinished merge with `git merge --abort`. If validation fails, leave
the integration branch for investigation and return to `personal` after saving
any uncommitted work. Never force-push or reset the official branches.

When the integration is validated, fast-forward `personal` to it and push only
to the personal origin. A Git update is not a deployment. Deployment requires
a fresh data backup and a separate decision about the active installation.

## Completing the GitHub fork later

Create a fork of `odysseus-dev/odysseus` in the authenticated account, retaining
the `dev` branch. Verify GitHub reports `fork: true` and the expected parent.
Keep the local bare origin as an additional recovery destination before
changing remotes:

```powershell
git remote rename origin local-backup
git remote add origin https://github.com/YOUR_LOGIN/odysseus.git
git fetch origin
git push --set-upstream origin dev personal
```

Replace `YOUR_LOGIN` only after confirming the account and fork. Scan the
outgoing commits for secrets again before publishing. Disable Actions and
Pages until their workflows and publishing credentials have been reviewed.

## Development and validation

Use a dedicated environment and temporary application data. Never copy the
production `.env`, databases or authentication files into this checkout to
make a test pass. Reserve a separate, non-listening Chroma port when running
tests so an HTTP client cannot adopt the production vector store. Leave
`APP_PORT` unset for unit tests; it opts into live smoke tests.

The native `scripts/odysseus-dev` launcher supports isolated data and ports,
but its process-identification tests have a known Windows failure in the
audited upstream revision. A complete Windows server boot and stop cycle was
not validated in this recovery. The recovered `Start-Odysseus.ps1`,
`Stop-Odysseus.ps1` and `Open-Odysseus.ps1` scripts target the installed desktop
application; do not run them as development launchers.

The optional Journal-IA and Automatisations-PC adapters expect separately
installed libraries under the current user's `Documents` directory. Those
libraries, their virtual environments, configurations and personal data are
not bundled or tested end to end here. Existing update notifications still
check official `main`; use the Git workflow above to track `dev`.

## Restore and rollback

Restore data into a new, private directory first. Validate every checksum and
run SQLite `PRAGMA integrity_check` on the restored databases. Keep the
application encryption key with the matching encrypted data. Do not replay
old SQLite WAL/SHM files over a logical database snapshot.

Do not restore over a running application. A real rollback needs a planned
stop, a fresh capture of the current state, and preservation of the replaced
files. This recovery did not stop or deploy the production application.

The private audit report and backup manifests are stored separately from this
repository. The local backups are on the same device as the installation;
arrange an encrypted off-device copy to cover device loss.
