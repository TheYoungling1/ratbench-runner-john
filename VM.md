# VM access — Hetzner `Graph2Env` (167.233.64.96)

How to reach the benchmark VM from this Mac, and how to re-enable access after the box is
rebuilt. Written 2026-09-03 after the previous VM (`Research-1`) was replaced.

## Connect

```bash
ssh -p 443 root@167.233.64.96
```

**Always port 443, not 22.** User is `root`.

## Why 443

Port 22 is unreachable **from inside Claude Code**. Its sandbox permits outbound TCP on 443 only:

```
github.com:443        succeeded      ← 443 egress allowed
github.com:22         blocked        ← proves the block is local, not the VM's firewall
167.233.64.96:22      timed out
167.233.64.96:443     connected
```

The `!` prefix in Claude Code runs in that **same** sandbox, so `! ssh 167.233.64.96` fails too.
A terminal outside Claude Code (Terminal.app, iTerm) is a different matter — port 22 may work
there; it has not been tested since the rebuild. If it does not, use 443 from there as well.

Diagnostic tell: **`Connection refused` means the host is alive** and answered with a TCP reset —
the packet crossed every firewall and found nothing listening. **`Operation timed out` means
something dropped it.** Unroutable control addresses (`192.0.2.1`) time out, so a refusal is
positive evidence the machine is up.

## Enabling 443 on a rebuilt VM

The VM only listens on 22 out of the box. To add 443, use the **Hetzner Cloud Console** → the
server → the `>_` button. That is a browser VNC session needing no network from your side, so it
works even when SSH does not. It usually **cannot paste**, so keep commands short.

Log in as `root`, then:

```
echo Port 443 >> /etc/ssh/sshd_config
systemctl disable --now ssh.socket
systemctl enable --now ssh
systemctl restart ssh
ss -lnt | grep 443
```

The last line must show `:443`. Port 22 keeps working — the original `Port 22` directive is
untouched.

### The two traps

**`ssh.socket` overrides `sshd_config`.** On Ubuntu 22.10+/Debian 12+, ssh is socket-activated:
systemd owns the listening ports and **every `Port` line in `sshd_config` is ignored**. Symptom:
`sshd -T | grep port` shows 443, `sshd -t` passes, the restart succeeds — and `ss` still shows only
`:22`. `systemctl is-active ssh.socket` returning `active` confirms it. Disabling the socket and
enabling `ssh.service` is what fixes it.

**`enable --now` does not restart a running service.** If `ssh.service` is already up, `--now` is a
no-op and the daemon keeps its old config. `systemctl restart ssh` is the step that makes sshd
re-read `sshd_config`. This is what blocked us the first time round.

### Firewalls

Nothing needed in the Hetzner console as of 2026-09-03 — 443 already reaches the host (proved by
the refusal, above). If a Cloud Firewall is later attached with an allowlist, add inbound TCP 443.
`ufw` on the host is separate; check with `ufw status` and `ufw allow 443/tcp` only if active.

## Verify

```bash
ssh -p 443 root@167.233.64.96 'hostname; uname -m; nproc; free -g | head -2; df -h /'
```

## State as of 2026-09-03

```
Graph2Env · Ubuntu 26.04.1 LTS · x86_64
8 cores · 15 GB RAM · 287 GB free on /
python3.14 only (no 3.10, no 3.11) · git · curl
Docker: NOT INSTALLED
/root and /opt: empty
```

**This is a bare box.** It is not the old `Research-1` VM — notes in earlier sessions referring to
`/opt/manifest_builder` on this IP are void. Nothing is provisioned; a benchmark run needs Docker,
a Python 3.11+ venv for SWE-agent, the repo, and `.env`.

## Gotchas worth remembering

- **`known_hosts` records the port.** A non-default port appears as `[167.233.64.96]:443`. An
  unbracketed entry means the connection was on 22. Useful for recovering which port a host
  actually used.
- The hostname changed from `Research-1` to `Graph2Env` on the same IP — check `hostname` before
  trusting anything an old session recorded about this address.
- macOS screenshot filenames contain a narrow no-break space (U+202F) before `am`/`pm`, so a
  copy-pasted path fails in the shell. Glob it instead: `ls Screenshot*8.42*`.
