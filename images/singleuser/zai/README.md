# Reaching this server from outside

Everything below goes through `https://<host>/user/{{USER}}/` and needs a
JupyterHub API token. Make one at **Account → API tokens** (`/hub/token`).

## Command line

    curl -fsSLo zai https://<host>/hub/zai/cli && chmod +x zai
    ./zai login https://<host>
    ./zai status

## SSH, scp, rsync, VS Code Remote-SSH

1. Put your public key in `~/.ssh/authorized_keys` on this server.
2. On your machine run `./zai ssh-config >> ~/.ssh/config`.
3. `ssh zai` (or pick host `zai` in VS Code Remote-SSH).

SSH only works while this server is running. Port forwarding works:
`ssh -L 8000:localhost:8000 zai`.

## Web apps on a port

Anything listening on a port inside this server is reachable at

    https://<host>/user/{{USER}}/proxy/<port>/

In a browser, your login is enough. From code, send your token:

    curl -H "Authorization: Bearer $TOKEN" https://<host>/user/{{USER}}/proxy/8000/v1/models

OpenAI-compatible clients work as is: use the token as the API key and
`https://<host>/user/{{USER}}/proxy/8000/v1` as the base URL.

Nobody without a token for your account can reach these ports.
