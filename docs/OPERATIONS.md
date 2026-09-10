# Operations

Day-two notes for running this platform: onboarding people, sizing their grants,
and the two failure modes that actually take the service down.

## Onboarding a user

1. They register at `https://<host>/hub/signup` — username, email, password.
   The account is created but **inert**: `is_authorized = 0`.
2. Approval happens one of two ways:
   - **Manual** (default): an admin visits `/hub/authorize` and approves them.
   - **Self-service**: if `SELF_APPROVAL_DOMAINS` is configured and their email
     matches, they get an activation link by email and no admin is involved.
3. On first login they pick a profile, get a pod, and land in JupyterLab with
   `Welcome.ipynb` already in their home directory.

Usernames must match `^[a-z][a-z0-9-]{2,30}$`. This is not cosmetic: the username
becomes both the pod name and the PVC name, and a name that is not a valid DNS
label produces a server that cannot spawn — with the failure surfacing much later
than the signup that caused it.

### Admin URLs

| URL | Use |
|---|---|
| `/hub/authorize` | Approve pending signups |
| `/hub/admin` | List users, stop servers (frees a MIG slice), delete users |
| `/hub/change-password` | Anyone changing their own password |

## The admin account is load-bearing — and claimable

`admin_users: [admin]` means NativeAuthenticator treats a signup named `admin` as
**pre-authorized**: no approval step, instant access. `create_user()` gates only
on `user_exists()`, which consults NativeAuthenticator's own `users_info` table —
**not** JupyterHub's `users` table.

The consequence is easy to miss. JupyterHub auto-creates a `users` row for
everything in `admin_users` at startup, so `/hub/admin` shows an `admin` account
and everything looks claimed. But if there is no matching `users_info` row, the
account has no password, and **anyone on the internet can register `admin` and
receive instant administrator access.**

Check it directly:

```bash
kubectl -n jupyterhub exec deploy/hub -- python -c "
import sqlite3; c=sqlite3.connect('/srv/jupyterhub/jupyterhub.sqlite')
print([r[0] for r in c.execute('select username from users_info')])"
```

If `admin` is not in that list, the account is unclaimed. Claim it immediately,
over a port-forward rather than the public URL:

```bash
kubectl -n jupyterhub port-forward svc/proxy-public 8080:http
# from your laptop: ssh -L 8080:localhost:8080 root@<host>
# browse http://localhost:8080/hub/signup and register as: admin
```

Deleting the admin user from `/hub/admin` re-opens this. Don't.

## Sizing

The box has 235 GB RAM and 7 MIG slices, so the ceiling that matters is
`7 x mem_limit`, not the total.

| Profile | RAM | CPU | GPU | 7-user worst case |
|---|---|---|---|---|
| GPU — Standard | 12 G | 2 | 1 slice | 84 G |
| GPU — Large | 24 G | 4 | 1 slice | 168 G |
| CPU only | 8 G | 2 | none | — |

The CPU-only profile exists to protect the scarcest resource. There are seven
slices and no more; without that option every login consumes one even for work
that never touches a GPU, and the eighth user simply cannot spawn.

Changing a profile's limits affects **new** servers. Existing PVCs keep the size
they were created with — `capacity` is not editable after the fact.

## Storage

| Path | Backing | Shared | Quota |
|---|---|---|---|
| `~/` | per-user PVC on XFS | private | enforced, from the PVC request |
| `~/shared` | hostPath | everyone, read-only | none — admin-curated |
| `~/team` | hostPath | everyone, read-write | none — watch it |
| object storage | S3 | private prefix per user | provider-side |

### Quotas are only real because of scripts/07

k3s's `local-path` provisioner is hostPath underneath: it records a PVC's
requested size and **enforces nothing**. Before `scripts/07-user-storage.sh` runs,
a "10Gi" grant is advisory and one user can fill the root filesystem and take the
cluster down with it.

`07` builds an XFS filesystem in a loop-mounted image and points local-path at it,
because the root filesystem is ext4 without the `quota`/`project` features and
enabling them means `tune2fs` plus a remount of `/`. `scripts/08-quota-apply.sh`
then reconciles an XFS **project quota** onto each volume every two minutes,
sized from the PVC. Project quotas are inherited, so everything created under a
user's home counts against their cap, and hitting it produces an ordinary
`ENOSPC` they can recover from by deleting files.

Verify enforcement is live:

```bash
mountpoint /var/lib/k3s-user-storage && xfs_quota -x -c 'report -h' /var/lib/k3s-user-storage
systemctl status jupyter-quota.timer
```

`~/team` has **no quota** — it is a hostPath on the root filesystem, so anything
dumped there counts against `/`, not against a user. It is the most likely thing
to fill the disk. `08` alarms at 85% of the user-storage image, but that alarm
does not cover `~/team`.

## The two things that take this down

**A held MIG slice.** Idle culling (1 h) is what returns slices to the pool. It
authenticates as a service, using a role the chart appends to
`c.JupyterHub.load_roles`. Setting `load_roles` under `hub.config` *replaces* that
list, the culler starts getting `403` on every poll, culling silently stops, and
idle servers hold their slices forever. Add roles via `hub.loadRoles` instead,
which appends. Check with:

```bash
kubectl -n jupyterhub logs deploy/hub | grep -c "403: Forbidden"   # expect 0
```

**A full disk.** See above. Watch `/` as well as the user-storage image.

## Upgrading the singleuser image

Pin JupyterLab extension versions. An unpinned extension is free to resolve to a
build incompatible with the base image's JupyterLab on any future rebuild, and
the failure is not loud — `jupyter labextension list` marks it `X` and Lab loads
with the feature missing.

`jupyter-collaboration` is deliberately absent. Its extension requires a
`@jupyter/ydoc` older than JupyterLab 4.6 needs, and installing it *disables* the
core `filebrowser` and `cell-executor` extensions in order to replace them — so
users get a JupyterLab with no file browser. Server sharing is provided through
JupyterHub's own `shares!user` scope instead.

After rebuilding:

```bash
scripts/04-images.sh          # build + import into k3s containerd
kubectl -n jupyterhub delete pod -l component=singleuser-server   # optional
```

Running servers keep the old image until they restart.
