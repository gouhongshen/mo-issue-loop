"""Configure a tunnel-only SSH identity on this disposable VM, never on macOS."""
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess


def public_key(value):
    match = re.fullmatch(r"(ssh-ed25519 [A-Za-z0-9+/=]+)(?: [^\r\n]+)?", value.strip())
    if not match:
        raise ValueError("expected an Ed25519 public key")
    return match.group(1)


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("bootstrap is restricted to GitHub Actions")
    public = public_key(os.environ["SSH_PUBLIC_KEY"])
    instance = os.environ["INSTANCE"]
    if not re.fullmatch(r"[a-f0-9]{32}", instance):
        raise ValueError("invalid instance identity")
    private = Path(os.environ["RUNNER_TEMP"]) / "mo-private"
    private.mkdir(mode=0o750)
    public_file = private / "key.pub"
    public_file.write_text(public + "\n")
    subprocess.run(["ssh-keygen", "-lf", str(public_file)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["sudo", "useradd", "--create-home", "--shell", "/bin/sh", "--password", "*", "challenger-link"], check=True)
    token = private / "admin-token"
    token.write_text(secrets.token_hex(32))
    token.chmod(0o640)
    command = private / "auth-command"
    ssh_root = Path("/etc/mo-challenger")
    command.write_text('#!/bin/sh\n[ "$SSH_ORIGINAL_COMMAND" = auth ] || exit 1\nexec cat /etc/mo-challenger/admin-token\n')
    command.chmod(0o750)
    key_file = private / "authorized_keys"
    key_file.write_text('restrict,port-forwarding,command="/etc/mo-challenger/auth-command",permitopen="127.0.0.1:8765",'
                       'permitopen="127.0.0.1:26001",permitopen="127.0.0.1:26002" ' + public + '\n')
    host_key = private / "host_key"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["sudo", "chgrp", "-R", "challenger-link", str(private)], check=True)
    # StrictModes rejects authorized_keys beneath another user's home tree.
    subprocess.run(["sudo", "mkdir", "-p", str(ssh_root)], check=True)
    for source_file, mode in ((key_file, "644"), (command, "755")):
        subprocess.run(["sudo", "install", "-o", "root", "-g", "root", "-m", mode,
                        str(source_file), str(ssh_root / source_file.name)], check=True)
    subprocess.run(["sudo", "install", "-o", "root", "-g", "challenger-link", "-m", "640",
                    str(token), str(ssh_root / "admin-token")], check=True)
    address = subprocess.check_output(["tailscale", "ip", "-4"], text=True).strip()
    if ipaddress.ip_address(address) not in ipaddress.ip_network("100.64.0.0/10"):
        raise ValueError("unexpected Tailscale IP")
    config = private / "sshd_config"
    config.write_text('\n'.join([
        "Port 2222", "ListenAddress " + address, "HostKey " + str(host_key),
        "PidFile " + str(private / "sshd.pid"), "AuthorizedKeysFile /etc/mo-challenger/authorized_keys",
        "AllowUsers challenger-link", "PasswordAuthentication no", "KbdInteractiveAuthentication no",
        "PermitRootLogin no", "UsePAM no", "AllowTcpForwarding local", "GatewayPorts no",
        "PermitTTY no", "X11Forwarding no", "AllowAgentForwarding no", "PermitTunnel no",
        "StrictModes yes", "LogLevel ERROR", "ClientAliveInterval 30", "ClientAliveCountMax 3", ""]))
    subprocess.run(["sudo", "mkdir", "-p", "/run/sshd"], check=True)
    subprocess.run(["sudo", "/usr/sbin/sshd", "-f", str(config)], check=True)
    manifest = Path("runner-info")
    manifest.mkdir()
    (manifest / "runner.json").write_text(json.dumps({
        "instance": instance, "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "host": address, "ssh_port": 2222, "host_key": host_key.with_suffix(".pub").read_text().strip(),
        "started_at": int(os.environ["JOB_STARTED_AT"]), "image": os.environ["RUNNER_IMAGE"]}))


if __name__ == "__main__":
    main()
