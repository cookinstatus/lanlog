"""Settings for lanlog, discovered automatically where possible.

The first version of this tool had my own network's addresses hardcoded
(192.168.1.151 for Pi-hole, 192.168.1.254 for the router, 192.168.1.0/24 for
the subnet). That works on exactly one network. Everything here is either
auto-detected or read from ~/.config/lanlog/config, so the tool works on
someone else's LAN unchanged.

Detection order for each setting:
  1. environment variable (LANLOG_PIHOLE, LANLOG_ROUTER, LANLOG_CIDR, ...)
  2. ~/.config/lanlog/config, written by `lanlog setup`
  3. auto-detection at run time
  4. a documented default
"""

import os
import re
import shutil
import socket
import subprocess

CONFIG_DIR = os.environ.get(
    "LANLOG_CONFIG_DIR", os.path.expanduser("~/.config/lanlog")
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config")
CACHE_FILE = os.path.expanduser("~/.cache/lanlog/names")

# Settings with their auto-detection and default.
#   key          env var            default if detection fails
DEFAULTS = {
    "pihole_ip":  ("LANLOG_PIHOLE",  ""),        # Pi-hole's LAN address
    "router":    ("LANLOG_ROUTER",  ""),        # gateway / DHCP server
    "cidr":      ("LANLOG_CIDR",    ""),        # subnet to sweep
    # No default on purpose. Defaulting this to "pihole" made detection
    # indistinguishable from a real find, so a machine with no local Pi-hole
    # reported one anyway. read_command() still falls back to "pihole" when
    # building the actual command.
    "container": ("LANLOG_CONTAINER", ""),
    "runtime":   ("LANLOG_RUNTIME", ""),        # podman | docker
    "pihole_log": ("LANLOG_PIHOLE_LOG", "/var/log/pihole/pihole.log"),
    # Where the Pi-hole container actually lives. Empty means "this machine",
    # which is the common case. Set it to another host's address when Pi-hole
    # runs elsewhere on the LAN and you want THIS machine's dashboard to show
    # the queries it sends.
    "pihole_host": ("LANLOG_PIHOLE_HOST", ""),
    # SSH user on pihole_host. Empty means the current username.
    "pihole_ssh_user": ("LANLOG_PIHOLE_SSH_USER", ""),
    # SSH port on pihole_host. Empty means 22. Useful when the Pi-hole host
    # runs sshd on a nonstandard port, which is common on locked-down boxes.
    "pihole_ssh_port": ("LANLOG_PIHOLE_SSH_PORT", ""),
    # Seconds into a Pi-hole query log to skip on startup, so a machine that
    # boots late does not re-ingest hours of history as if it were new.
    "log_tail":  ("LANLOG_LOG_TAIL", "200"),
}

_cache = None


# --------------------------------------------------------------------------
# file-backed settings
# --------------------------------------------------------------------------

def load_settings():
    """Read ~/.config/lanlog/config into a dict (no detection)."""
    global _cache
    if _cache is not None:
        return _cache
    out = {k: "" for k in DEFAULTS}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip().lower()
                    if k in out:
                        out[k] = v.strip().strip('"')
    except OSError:
        pass
    _cache = out
    return out


def save_settings(values):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        fh.write("# lanlog settings. Written by `lanlog setup`.\n")
        fh.write("# Edit by hand if you move Pi-hole to a different address.\n")
        for key in DEFAULTS:
            fh.write(f"{key} = {values.get(key, '')}\n")
    global _cache
    _cache = None
    return CONFIG_FILE


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

OS_RELEASE = "/etc/os-release"

# Package-manager command templates, one per family. Kept as a table so
# install.sh and the SSH guide cannot drift apart -- they had separate
# hardcoded copies, and the guide already supported arch while the installer
# did not.
#
# Placeholders: {p} is the package name.
PKG_INSTALL = {
    "debian": "sudo apt install {p}",
    "fedora": "sudo dnf install {p}",
    "arch":   "sudo pacman -S --needed {p}",
    "suse":   "sudo zypper install {p}",
    "alpine": "sudo apk add {p}",
}

# ip / ping / dig in one line, for the "some tools are missing" warning.
PKG_TOOLS = {
    "debian": "sudo apt install iproute2 iputils-ping dnsutils",
    "fedora": "sudo dnf install iproute iputils bind-utils",
    "arch":   "sudo pacman -S --needed iproute2 iputils dnsutils",
    "suse":   "sudo zypper install iproute2 iputils bind-utils",
    "alpine": "sudo apk add iproute2 iputils bind-tools",
}


def distro():
    """(id, id_like, pretty) for this machine, e.g. fedora / debian.

    Read from /etc/os-release rather than guessing from the hostname or a
    package list. Returns ("", "", "") if it cannot be read, which callers
    treat as "unknown, show everything".
    """
    vals = {"ID": "", "ID_LIKE": "", "PRETTY_NAME": ""}
    try:
        with open(OS_RELEASE, encoding="utf-8") as fh:
            for line in fh:
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k in vals:
                    vals[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return vals["ID"], vals["ID_LIKE"], vals["PRETTY_NAME"]


def distro_family():
    """Normalised family: fedora, debian, arch, suse, alpine, or ''.

    Fewer branches than listing every distribution. Derived from ID first and
    ID_LIKE second, so a derivative like Rocky or Manjaro resolves the same
    way as its parent without needing its own case.
    """
    did, like, _pretty = distro()
    hay = f"{did} {like}".lower()
    for family in ("fedora", "debian", "arch", "suse", "alpine"):
        if family in hay:
            return family
    return ""


def pkg_install_cmd(package):
    """The right install command for a package on this machine, or ''.

    Guessing a package manager wrong wastes the reader's time on a command
    that fails. When the family is unknown this returns '' so the caller can
    list the options rather than pick one.
    """
    tmpl = PKG_INSTALL.get(distro_family())
    return tmpl.format(p=package) if tmpl else ""


def pkg_tools_cmd():
    """One line that installs ip, ping and dig, or '' if unknown."""
    return PKG_TOOLS.get(distro_family(), "")


def firewall_tool():
    """'firewalld', 'ufw', or '' -- which firewall this machine actually runs.

    Detected by presence rather than by distro, because a Fedora box may use
    neither and a Debian box may have ufw installed but disabled.
    """
    if shutil.which("firewall-cmd"):
        return "firewalld"
    if shutil.which("ufw"):
        return "ufw"
    return ""


def _run(cmd):
    """Run a command, returning stdout. Returns "" if it fails.

    Empty output and a failed command are indistinguishable here, so anything
    that must tell them apart uses _try() below instead.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _try(cmd):
    """Run a command. Returns (ok, stdout, stderr).

    Separates "ran and printed nothing" from "could not run at all", which is
    the difference between a healthy empty container list and a permission
    error. Docker's socket denial is a non-zero exit, not an exception, so
    checking for exceptions alone is not enough.
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return False, "", str(exc)


def default_gateway():
    """The machine's default gateway, which is almost always the router."""
    out = _run(["ip", "-o", "route", "show", "default"])
    m = re.search(r"default via (\S+)", out)
    return m.group(1) if m else ""


def local_cidr():
    """The subnet of the interface holding the default route."""
    iface = _run(["ip", "-o", "route", "show", "default"]).split()
    dev = iface[iface.index("dev") + 1] if "dev" in iface else ""
    if not dev:
        return ""
    # `ip -o -4 addr show dev X` -> "3: eth0    inet 192.168.1.5/24 brd ..."
    out = _run(["ip", "-o", "-4", "addr", "show", "dev", dev])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", out)
    return m.group(1) if m else ""


def container_runtime():
    """podman if present, else docker. Empty if neither is installed."""
    if shutil.which("podman"):
        return "podman"
    if shutil.which("docker"):
        return "docker"
    return ""


def runtime_usable(rt=None):
    """(ok, message) -- can we actually talk to the container runtime?

    Distinct from the binary existing. Docker's socket is root-owned, so a
    fresh Docker install gives every non-root user a `docker` on PATH that
    then fails on use. Checking only for the binary is what let an install
    report success and then log nothing at all.
    """
    rt = rt or container_runtime()
    if not rt:
        return False, "neither podman nor docker found on PATH"
    ok, _out, err = _try([rt, "ps", "--format", "{{.Names}}"])
    if ok:
        return True, ""
    if "permission denied" in err.lower() or "docker.sock" in err.lower():
        return False, (f"{rt}: permission denied on its socket. Your user "
                       f"needs to be in the '{rt}' group.")
    return False, f"{rt} is installed but not usable: {err.splitlines()[0] if err else 'unknown error'}"


def pihole_container(runtime=None, name=None):
    """Find a running container that looks like Pi-hole.

    The name is configurable, but guessing beats failing: a user who named
    theirs "pi-hole" or "pihole_latest" should not have to read this code.

    Returns "" when the runtime is unusable (missing, or permission denied),
    not just when no container matches. An earlier version returned the
    default name even when `podman ps` had failed, so lanlog would confidently
    report a container that did not exist and then fail on every query.
    """
    rt = runtime or container_runtime()
    if not rt or not runtime_usable(rt)[0]:
        return ""
    if name:
        return name
    out = _run([rt, "ps", "--format", "{{.Names}}"])
    for line in out.splitlines():
        low = line.strip().lower().replace("-", "").replace("_", "")
        if "pihole" in low or "ftl" in low:
            return line.strip()
    return ""


def pihole_address(rt=None, container=None):
    """Pi-hole's address on the LAN.

    Order: configured value, then a host published by the container, then a
    guess. It is NOT just the container's internal IP: with host networking
    that is the host's own address, and with pasta/slirp the container reaches
    the LAN through the host, so the usable address is the host's LAN IP.
    """
    s = load_settings()
    if s.get("pihole_ip"):
        return s["pihole_ip"]

    runtime = rt or s.get("runtime") or container_runtime()
    cont = container or s.get("container") or pihole_container(runtime)
    if not runtime or not cont:
        return ""
    # A published port tells us the host side of the mapping, but not the
    # address, so this only helps confirm the container exists.
    _run([runtime, "inspect", cont])
    # With pasta/slirp the container is reachable at the host's LAN address on
    # port 53, which is what most guides configure.
    return local_cidr().split("/")[0] or ""


def verify_dns(ip, name="example.com"):
    """True if something answers DNS at ip."""
    if not ip:
        return False
    out = subprocess.run(
        ["dig", "+short", "+time=3", "+tries=1", f"@{ip}", name],
        capture_output=True, text=True, timeout=6,
    ).stdout.strip()
    return bool(out)


# --------------------------------------------------------------------------
# resolved settings used across the app
# --------------------------------------------------------------------------

def settings():
    """Fully-resolved settings: config file, then detection, then default."""
    s = load_settings()
    out = {}
    for key, (env, dflt) in DEFAULTS.items():
        out[key] = os.environ.get(env) or s.get(key) or dflt
    if not out["router"]:
        out["router"] = default_gateway()
    if not out["cidr"]:
        out["cidr"] = local_cidr()

    # Local container detection only makes sense when Pi-hole is on THIS
    # machine. With pihole_host set, the container is over there: probing our
    # own runtime would either find nothing (and overwrite a perfectly good
    # config with blanks) or, worse, find an unrelated local container.
    if not out["pihole_host"]:
        if not out["runtime"]:
            out["runtime"] = container_runtime()
        if not out["container"]:
            out["container"] = pihole_container(out["runtime"])
        if not out["pihole_ip"]:
            out["pihole_ip"] = pihole_address(out["runtime"], out["container"])
    else:
        # Remote: keep the configured values, and default the container name
        # rather than overwriting it with a local probe that found nothing.
        out["runtime"] = out["runtime"] or "podman"
        out["container"] = out["container"] or "pihole"
    return out


def get(key):
    return settings().get(key, "")


def pi_container():
    """(runtime, container) for reading the Pi-hole log."""
    s = settings()
    return s["runtime"] or "podman", s["container"] or "pihole"
