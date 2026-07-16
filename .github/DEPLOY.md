# CI/CD — GitHub Actions → production VM

Two workflows:

| File | Role |
|------|------|
| `.github/workflows/ci.yml` | Lint / type / test both apps + a Docker-build sanity pass. Runs on PRs and feature pushes; also called by the deploy workflow. |
| `.github/workflows/deploy.yml` | On push to `main`: runs CI, then SSHes to the VM, fast-forwards it to `main`, and rebuilds the compose stack. |

## How the deploy works

The production stack is Docker Compose + Caddy on the Azure VM (`cortexmail.dev`) —
see `docs/RUNBOOK.md`. A deploy is just the runbook's manual flow, automated:

1. CI passes on the pushed commit.
2. The workflow SSHes in as `$VM_USER` and runs, in `~/AI-Mailbox`:
   ```
   git fetch --prune origin && git checkout main && git reset --hard origin/main
   bash deploy/vm-deploy.sh
   ```
3. `deploy/vm-deploy.sh` runs `docker compose … up -d --build` (the `dc` command
   from the runbook) and prunes dangling images.
4. Compose's one-shot `migrate` service runs `alembic upgrade head` before `api`
   and `worker` start, so **migrations are automatic** — no separate step.
5. The workflow polls `https://cortexmail.dev/api/v1/health` until it returns `ok`.

`git reset --hard origin/main` makes the box match `main` exactly. `deploy/.env`,
`models/`, and `data/` are gitignored, so it never touches your secrets or the
trained model. Treat the VM as a pure deploy target — don't hand-edit tracked
files there; a deploy would discard them.

## One-time setup — GitHub secrets

Set these under **Settings → Secrets and variables → Actions** (repo or a
`production` environment):

| Secret | Value | How to get it |
|--------|-------|---------------|
| `VM_HOST` | `52.233.95.168` (or `cortexmail.dev`) | from the runbook |
| `VM_USER` | `azureuser` | the VM login |
| `VM_SSH_KEY` | the **private** key contents | `cat ~/.ssh/cortexmail-vps_key.pem` — paste the whole PEM, including the BEGIN/END lines |
| `VM_SSH_KNOWN_HOSTS` | the VM's host public key | `ssh-keyscan -t ed25519,rsa cortexmail.dev` — paste the output (pins the host key so the runner can't be MITM'd) |

The deploy user (`azureuser`) is in the `docker` group per the runbook, so
`docker compose` runs without `sudo`, and the VM already has git access to pull
from GitHub. Nothing else to configure.

> **Least privilege (optional but recommended):** rather than reusing your personal
> `cortexmail-vps_key.pem`, generate a dedicated deploy keypair, add its public key
> to `~azureuser/.ssh/authorized_keys` on the VM, and put the private key in
> `VM_SSH_KEY`. Then you can revoke CI access without rotating your own key.

## Rollback

Deploys are just git checkouts, so rolling back is one too. On the VM:

```bash
cd ~/AI-Mailbox
git reset --hard <previous-good-sha>
bash deploy/vm-deploy.sh
```

Find the SHA in the Actions run log or `git log --oneline`.

## Note on build-on-box

The VM rebuilds images itself (`--build`) on every deploy, matching the runbook.
It's simple and needs no registry, but it's CPU/RAM-heavy on a 2 vCPU / 3.8 GB box
(especially with `INSTALL_LOCAL_CLASSIFIER=true` pulling torch). If deploys get
slow or memory-tight, the upgrade is to build images in CI, push to **GHCR**, and
have compose `pull` prebuilt images instead of building — a larger change to
`docker-compose.yml` (swap `build:` for `image:`), not done here.
