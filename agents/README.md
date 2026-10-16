# Cloud agents

Each user gets their own caimex agent server, reached by the caimex mobile app at:

    https://notebooks.futurezim.com/agents/<user>

```
phone ──HTTPS──> Cloudflare Tunnel ──> JupyterHub proxy (proxy-public)
                                          │  /agents/  (c.Proxy.extra_routes, charts/jupyterhub-values.yaml)
                                          ▼
                                agents-router (nginx, namespace agents)
                                          │  strips /agents/<user>
                                          ▼
                                agent-<user>:4096 (caimex serve, own pod + volume)
```

- **Image:** `local/caimex-agent:futurezim-cloud`. It's built from `digiland/caimex-code`
  branch `futurezim-cloud`, which keeps path prefixes in pairing links, accepts Bearer
  tokens, and keeps the password out of the agent's tools.
- **Auth:** each agent checks its own password (secret `agent-<user>`) or a pairing token.
  The router does no auth.
- **Isolation:** each agent runs as non-root with all capabilities dropped, no service
  account token, and CPU/memory limits. Its only environment is its password and its
  Caimex key. Network rules: only the router can reach an agent, and agents can reach the
  internet and DNS but nothing else inside the cluster, the node or the metadata service.
- **Model access:** every agent uses one shared Caimex gateway key (secret
  `caimex-shared` in namespace `agents`). Set or rotate it with `./agents/set-api-key.sh`,
  which also restarts the agents. For a few seconds after an agent starts, it lists no
  models while it loads its providers.

## First-time setup

```bash
cd ~/curly-garbanzo
./agents/build-image.sh                     # build caimex + image, load into k3s
kubectl apply -f agents/base.yaml           # namespace, router, network policies
./agents/set-api-key.sh                     # shared Caimex key (prompts for it)
./scripts/05-jupyterhub.sh                  # helm upgrade: adds the /agents/ route
```

## Users

```bash
./agents/new-agent.sh alice                    # create/update; prints a pairing link
./agents/pair-agent.sh alice                   # new pairing link (single use, 5 min)
kubectl -n agents delete deploy,svc,pvc -l agent-user=alice && \
  kubectl -n agents delete secret agent-alice  # remove (deletes their files)
```

The user opens the pairing link on their phone, or scans its QR code, in the caimex app.
The app contract (API, auth, events) is in the caimex-code repo.

## Checks

```bash
curl -s https://notebooks.futurezim.com/agents/alice/api/info   # 401 = reachable, needs login
kubectl -n agents get pods
kubectl -n agents logs deploy/agent-alice
```
