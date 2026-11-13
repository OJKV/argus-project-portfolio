"""systemd hardening that cannot break the services (review 2026-09-18 P-5, still OPEN).

Every unit was `User` + `WorkingDirectory` + `EnvironmentFile=.env` + `Nice` and nothing else, and
the whole `.env` — live OKX keys, LLM keys, Telegram token — was exported into the environment of
the dashboard, the backup shell and the weekly jobs, none of which need it in their environment.

Deliberately NOT applied, because each would break a service on this box:
- ProtectHome: the venv's interpreter is uv's Python under /home/argus.
- MemoryDenyWriteExecute: numba (via pandas-ta) JIT-compiles.
- CapabilityBoundingSet / AmbientCapabilities: the root-owned installer refuses both keys (P-N1);
  `NoNewPrivileges` with a non-root user already leaves the process no capabilities.
"""

from __future__ import annotations

import pytest

from .systemd_units import SYSTEMD, unit_values

HARDENING = {
    "NoNewPrivileges": "true",
    "PrivateTmp": "true",
    "ProtectSystem": "full",
    "ProtectKernelTunables": "true",
    "ProtectKernelModules": "true",
    "ProtectControlGroups": "true",
    "RestrictSUIDSGID": "true",
    "RestrictRealtime": "true",
    "LockPersonality": "true",
    "UMask": "0027",
}

SERVICES = sorted(p.name for p in SYSTEMD.glob("*.service"))

# The three runs that read HEALTHCHECKS_URL in a shell line (watchdog) or have been proven under
# systemd with the exported env and place orders (manager, review) keep it for now.
KEEPS_ENV_FILE = {"argus-manager.service", "argus-review.service", "argus-watchdog.service"}


# systemd.exec(5), NoNewPrivileges=: "Defaults to false, but certain settings override this and
# ignore the value of this setting. This is the case when SystemCallFilter=,
# SystemCallArchitectures=, RestrictAddressFamilies=, RestrictNamespaces=, PrivateDevices=,
# ProtectKernelTunables=, ProtectKernelModules=, ProtectKernelLogs=, ProtectClock=,
# MemoryDenyWriteExecute=, RestrictRealtime=, RestrictSUIDSGID=, DynamicUser= or LockPersonality=
# are specified." A service that sets any of them runs with no_new_privs on whatever its own
# NoNewPrivileges= says, and the kernel then ignores the setuid bit on every execve — sudo included
# (`sudo: The "no new privileges" flag is set, which prevents sudo from running as root`).
IMPLIES_NO_NEW_PRIVILEGES = {
    "SystemCallFilter",
    "SystemCallArchitectures",
    "RestrictAddressFamilies",
    "RestrictNamespaces",
    "PrivateDevices",
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "ProtectKernelLogs",
    "ProtectClock",
    "MemoryDenyWriteExecute",
    "RestrictRealtime",
    "RestrictSUIDSGID",
    "DynamicUser",
    "LockPersonality",
    # Not in the man page's sentence above, but in systemd's own test for it
    # (context_has_no_new_privileges): ProtectHostname=yes installs a seccomp filter on
    # sethostname/setdomainname besides the UTS namespace, so an unprivileged service gets
    # no_new_privs from it too.
    "ProtectHostname",
}

# Checked against the same logic and the man pages, and NOT in the list above:
#   ProtectProc=, ProcSubset=  procfs mount options in the unit's mount namespace; no seccomp.
#   PrivateIPC=                an IPC namespace; no seccomp.
#   RestrictFileSystems=       a BPF-LSM program, not a seccomp filter; the man page's override
#                              list does not name it.
# PrivateUsers= is different: it does not set no_new_privs, but the service then runs in a user
# namespace in which root is not the real root, so what sudo is for (writing /etc, restarting a
# system unit) cannot be done from inside it. It stops sudo by another route.
BREAKS_SUDO_OTHERWISE = {"PrivateUsers"}

# Units that call sudo: deploy/autodeploy.sh restarts the dashboard and runs the root-owned unit
# installer through it. Every key above, PrivateUsers, and NoNewPrivileges itself, is absent.
NEEDS_SUDO = {"argus-autodeploy.service"}

# What each sudo unit gives up from HARDENING. Besides the keys that imply NoNewPrivileges:
# ProtectSystem=full mounts /etc read-only for the whole process tree, sudo's children included,
# and the installer writes /etc/systemd/system.
EXEMPT = {
    service: {"NoNewPrivileges", "ProtectSystem"} | (IMPLIES_NO_NEW_PRIVILEGES & set(HARDENING))
    for service in NEEDS_SUDO
}


def keys_that_stop_sudo(unit_text: str) -> set[str]:
    """Every key in the unit (comments aside) that would stop `sudo` from working under it."""
    found = set()
    for raw in unit_text.splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        found.add(line.split("=", 1)[0].strip())
    return found & (IMPLIES_NO_NEW_PRIVILEGES | BREAKS_SUDO_OTHERWISE | {"NoNewPrivileges"})


@pytest.mark.parametrize("service", sorted(NEEDS_SUDO))
def test_a_unit_that_needs_sudo_sets_nothing_that_stops_sudo(service: str) -> None:
    offending = keys_that_stop_sudo((SYSTEMD / service).read_text())
    assert not offending, f"{service}: {sorted(offending)} stop sudo from working under this unit"


@pytest.mark.parametrize(
    "key", sorted(IMPLIES_NO_NEW_PRIVILEGES | BREAKS_SUDO_OTHERWISE | {"NoNewPrivileges"})
)
def test_the_rule_catches_each_key_it_lists(key: str) -> None:
    assert keys_that_stop_sudo(f"[Service]\nUser=argus\n{key}=true\n") == {key}


@pytest.mark.parametrize("key", ["ProtectProc", "ProcSubset", "PrivateIPC", "RestrictFileSystems"])
def test_keys_that_do_not_stop_sudo_are_not_forbidden(key: str) -> None:
    """The ones checked and left out (see the comments above): procfs mount options, an IPC
    namespace and a BPF-LSM filter. None installs a seccomp filter, so none sets no_new_privs."""
    assert keys_that_stop_sudo(f"[Service]\n{key}=yes\n") == set()


def test_the_rest_of_the_hardening_is_kept_on_the_sudo_unit() -> None:
    """Only what is incompatible is dropped: PrivateTmp, ProtectControlGroups and UMask stay."""
    for service in NEEDS_SUDO:
        for key in ("PrivateTmp", "ProtectControlGroups", "UMask"):
            assert unit_values(service, key) == [HARDENING[key]], f"{service}: {key}"


@pytest.mark.parametrize("service", SERVICES)
def test_every_service_is_hardened(service: str) -> None:
    exempt = EXEMPT.get(service, set())
    for key, value in HARDENING.items():
        expected = [] if key in exempt else [value]
        assert unit_values(service, key) == expected, f"{service}: {key}"


def test_the_exemptions_are_explained_where_they_are_granted() -> None:
    """Dropping a hardening key is a decision; the unit must say which, and why."""
    for service, keys in EXEMPT.items():
        comments = "\n".join(
            ln for ln in (SYSTEMD / service).read_text().splitlines() if ln.startswith("#")
        )
        for key in keys:
            assert key in comments, f"{service} drops {key} without saying so"
        assert "sudo" in comments, service


@pytest.mark.parametrize("service", sorted(set(SERVICES) - KEEPS_ENV_FILE))
def test_services_that_need_no_secrets_in_their_environment_do_not_get_them(service: str) -> None:
    """argus reads .env itself (Settings, from WorkingDirectory); exporting it only widens who
    sees it — /proc/<pid>/environ, every child process, a crash dump."""
    assert unit_values(service, "EnvironmentFile") == [], service


def test_the_backup_gets_only_the_one_variable_it_reads() -> None:
    assert unit_values("argus-backup.service", "Environment") == ["ARGUS_HOME=/var/lib/argus"]


@pytest.mark.parametrize("key", ["ProtectHostname", "PrivateUsers"])
def test_the_two_keys_the_man_page_sentence_misses_are_named_explicitly(key: str) -> None:
    """Spelled out, not derived from the sets: dropping either from them must fail a test, not
    silently drop its own test case."""
    assert keys_that_stop_sudo(f"[Service]\n{key}=yes\n") == {key}
