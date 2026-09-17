#!/usr/bin/env python3
import os
import shlex
import sys

APW = "/opt/apple-watcher/bin/apw"


def allowed(original):
    try:
        command = shlex.split(original)
    except ValueError:
        return None
    if command == [APW, "--version"]:
        return command
    if command[:7] != [APW, "watch", "--targets", "-", "--interval", "60", "--timeout"]:
        return None
    if len(command) != 8 or not command[7].isdigit() or not 1 <= int(command[7]) <= 1200:
        return None
    return command


def self_test():
    assert allowed(f"{APW} --version")
    assert allowed(f"{APW} watch --targets - --interval 60 --timeout 1200")
    assert not allowed(f"{APW} watch --targets /etc/passwd --interval 60 --timeout 1200")
    assert not allowed(f"{APW} watch --targets - --interval 30 --timeout 1200")
    assert not allowed(f"{APW} watch --targets - --interval 60 --timeout 1201")
    assert not allowed("sh -c id")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        raise SystemExit(0)
    command = allowed(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if not command:
        print("This key is restricted to the Apple watcher worker.", file=sys.stderr)
        raise SystemExit(126)
    os.environ["APW_BLOCK_STATE_FILE"] = "/var/lib/apple-watcher/cooldowns.json"
    os.execv(APW, command)
