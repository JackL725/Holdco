"""Credential vault (Phase 1, docs/phases/phase-1-connect.md section 1).

Secrets leave the plain-text settings tables. Each one is stored under a dotted name:

    settings.<key>            global secrets (config.SECRET_SETTINGS)
    company.<id>.<key>        company secrets (config.SECRET_COMPANY_SETTINGS)
    connector.<id>.<field>    secret fields of a connector instance
    oauth.<connector id>      an OAuth token set (a JSON string)

The settings tables keep a "vault:<name>" marker in place of the value; db.get_settings and db.company_settings
swap the marker for the value when they read it.

Three backends sit behind one interface. The first use picks one and keeps it for the life of the process:

    dpapi           Windows encrypts each secret for the signed-in user (CryptProtectData), kept in data/vault.json
    secret-service  the Linux desktop keyring, through the secret-tool program
    file            data/vault.json, encrypted with a random key in data/vault.key, or with a key derived from
                    HOLDCO_VAULT_PASSPHRASE when that is set

HOLDCO_VAULT_BACKEND (auto, dpapi, secret-service or file) forces one; the tests use file so they never touch a
real keyring. data/vault.json records which backend stored each secret, so a backup restored on another machine
fails loudly ("enter it again in Settings") instead of quietly losing keys.

redact() is the backstop that keeps secrets out of events, transcripts, the audit log and records/: every known
secret value of 8 characters or more becomes [redacted]. Nothing in this module prints or logs a secret value.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets as _random
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config

MARKER = "vault:"           # what the settings tables keep instead of a secret: "vault:<name>"
MASK = "••••••••"           # what the dashboard shows (and sends back) for a secret that is set
REDACTED = "[redacted]"
BACKENDS = ("dpapi", "secret-service", "file")
MIN_REDACT = 8              # shorter values are too likely to be ordinary words to replace everywhere
PBKDF2_ROUNDS = 200_000
SECRET_TOOL_TIMEOUT = 15    # seconds; a locked keyring must not hang Holdco
CRYPTPROTECT_UI_FORBIDDEN = 0x1

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
# Inside a JSON secret (an OAuth token set), the fields whose values are secrets of their own. token_endpoint and
# token_type do not match: they end differently.
_SECRET_FIELD_RE = re.compile(r"(token|secret|password|passwd|api_?key)$", re.I)


class VaultError(Exception):
    """A secret could not be stored or read. The message is plain language and never contains the value."""


_lock = threading.RLock()
_active: str | None = None          # the backend in use, chosen at first use
_secret_tool_ok: bool | None = None  # result of the secret-tool probe
_values: dict[str, str] = {}        # secrets read or written so far: the fast path for get() and redact()
_needles: list[str] | None = None   # the strings redact() replaces, longest first (None: rebuild)
_swept = False                      # redact() has loaded every secret once
_stamp: tuple | None = None         # vault.json (inode, mtime, size) the caches belong to
_masters: dict = {}                 # passphrase-derived keys (PBKDF2 is slow on purpose, so it runs once)
_dll: dict = {}                     # Windows: crypt32 and kernel32 with their argument types set


def _reset() -> None:
    """Forget the chosen backend and every cached value and key (tests, or after changing the environment)."""
    global _active, _secret_tool_ok, _needles, _swept, _stamp
    with _lock:
        _active = None
        _secret_tool_ok = None
        _values.clear()
        _masters.clear()
        _needles = None
        _swept = False
        _stamp = None


# ---------------------------------------------------------------- the vault file

def _path() -> Path:
    return config.DATA_DIR / "vault.json"


def _key_path() -> Path:
    return config.DATA_DIR / "vault.key"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s) -> bytes:
    return base64.b64decode(s, validate=True)


def _file_stamp() -> tuple | None:
    try:
        st = _path().stat()
    except FileNotFoundError:
        return None
    except OSError:
        return ("unreadable",)
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _sync() -> None:
    """Drop the caches when vault.json changed outside this process (a restored backup, a hand edit)."""
    global _stamp, _swept, _needles
    now = _file_stamp()
    if now != _stamp:
        _values.clear()
        _swept = False
        _needles = None
        _stamp = now


def _load() -> dict:
    """vault.json as {"version", "id"?, "salt"?, "backend"?, "secrets": {name: entry}}. A missing file is an
    empty vault; a damaged one raises VaultError."""
    p = _path()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "secrets": {}}
    except (OSError, UnicodeDecodeError) as e:
        raise VaultError(f"The vault file {p} cannot be read ({getattr(e, 'strerror', None) or e})") from None
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise VaultError(f"The vault file {p} is damaged (it is not valid JSON)")
    found = data.get("secrets")
    if not isinstance(found, dict):
        # The design's dpapi example keeps entries at the top level: read that layout too.
        found = {k: v for k, v in data.items() if isinstance(v, dict) and "backend" in v}
        data = {k: v for k, v in data.items() if k not in found}
    data["secrets"] = {str(k): v for k, v in found.items() if isinstance(v, dict)}
    data.setdefault("version", 1)
    return data


def _save(data: dict) -> None:
    """Write vault.json through a temporary file and a rename, so a crash never leaves half a vault."""
    global _stamp
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".vault.json.{os.getpid()}.{threading.get_ident()}.tmp")
    body = json.dumps(data, indent=1, sort_keys=True).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:   # Windows: a backup or a virus scanner has the file open for a moment
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise VaultError(f"The vault file {p} could not be written ({e.strerror or e})") from None
    _stamp = _file_stamp()


def _check_name(name) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise VaultError(f"Not a valid secret name: {str(name)[:60]!r} (use letters, digits and dots, such as "
                         "connector.3.api_key)")
    return name


# ---------------------------------------------------------------- choosing a backend

def backend() -> str:
    """The backend in use: "dpapi", "secret-service" or "file". Chosen at first use, then kept."""
    global _active
    with _lock:
        if _active is None:
            _active = _choose()
        return _active


def _choose() -> str:
    want = (os.environ.get("HOLDCO_VAULT_BACKEND") or "auto").strip().lower()
    if want in BACKENDS:
        return want
    if want != "auto":
        print(f"Vault: HOLDCO_VAULT_BACKEND={want!r} is not auto, dpapi, secret-service or file; choosing "
              "automatically")
    # Keep the backend the stored secrets already use while it works here, so installing a keyring later (or
    # forcing a backend once) never strands them.
    try:
        data = _load()
        used = {e.get("backend") for e in data["secrets"].values()}
        if data.get("backend") in used and data.get("backend") in BACKENDS and _available(data["backend"]):
            return data["backend"]
    except VaultError:
        pass
    if os.name == "nt":
        return "dpapi"
    if _available("secret-service"):
        return "secret-service"
    return "file"


def _available(name: str) -> bool:
    if name == "file":
        return True
    if name == "dpapi":
        return os.name == "nt"
    if name == "secret-service":
        return _secret_tool_works()
    return False


# ---------------------------------------------------------------- Windows DPAPI

def _crypt_dlls():
    """crypt32 and kernel32 with argument types set (private instances, so nothing else in the process is
    affected)."""
    if not _dll:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_p = ctypes.POINTER(Blob)
        crypt32.CryptProtectData.argtypes = [blob_p, wintypes.LPCWSTR, blob_p, ctypes.c_void_p, ctypes.c_void_p,
                                             wintypes.DWORD, blob_p]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p, ctypes.c_void_p,
                                               wintypes.DWORD, blob_p]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        _dll.update(ctypes=ctypes, Blob=Blob, crypt32=crypt32, kernel32=kernel32)
    return _dll


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Encrypt (protect=True) or decrypt bytes with DPAPI for the current Windows user. Never shows a prompt."""
    if os.name != "nt":
        raise VaultError("DPAPI exists only on Windows")
    try:
        d = _crypt_dlls()
        ctypes, Blob = d["ctypes"], d["Blob"]
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = Blob()
        if protect:
            ok = d["crypt32"].CryptProtectData(ctypes.byref(blob_in), "Holdco", None, None, None,
                                               CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        else:
            ok = d["crypt32"].CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                                 CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    except (OSError, AttributeError, TypeError, ValueError) as e:   # the DLL or the call itself failed
        raise VaultError(f"DPAPI could not be called ({e})") from None
    if not ok:
        raise VaultError(f"Windows error {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        d["kernel32"].LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


# ---------------------------------------------------------------- Secret Service (secret-tool)

def _secret_tool(args: list[str], value: str | None = None) -> subprocess.CompletedProcess:
    """Run secret-tool. A value goes on stdin, never on the command line where other programs could see it."""
    exe = shutil.which("secret-tool")
    if not exe:
        raise VaultError("secret-tool is not installed (on Debian or Ubuntu: sudo apt install libsecret-tools)")
    try:
        return subprocess.run([exe, *args], input=(value or "").encode("utf-8"), capture_output=True,
                              timeout=SECRET_TOOL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise VaultError(f"secret-tool did not answer within {SECRET_TOOL_TIMEOUT} seconds (is the keyring "
                         "locked?)") from None
    except OSError as e:
        raise VaultError(f"secret-tool could not run ({e.strerror or e})") from None


def _tool_error(r: subprocess.CompletedProcess) -> str:
    text = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return text[-1][:200] if text else f"exit code {r.returncode}"


def _strip_newline(s: str) -> str:
    return s[:-1] if s.endswith("\n") else s


def _secret_tool_works() -> bool:
    """secret-tool exists and a store, lookup and clear of a throwaway probe secret all work (cached)."""
    global _secret_tool_ok
    if _secret_tool_ok is None:
        _secret_tool_ok = _probe()
    return _secret_tool_ok


def _probe() -> bool:
    if not shutil.which("secret-tool"):
        return False
    token = _random.token_hex(16)
    attrs = ["app", "holdco", "name", f"probe.{token[:8]}"]
    try:
        if _secret_tool(["store", "--label", "Holdco probe", *attrs], token).returncode != 0:
            return False
        r = _secret_tool(["lookup", *attrs])
        return r.returncode == 0 and _strip_newline(r.stdout.decode("utf-8", "replace")) == token
    except VaultError:
        return False
    finally:
        try:
            _secret_tool(["clear", *attrs])
        except VaultError:
            pass


def _attrs(data: dict, name: str) -> list[str]:
    """The keyring attributes of a secret. The vault id keeps two Holdco data folders on one PC (or a test run)
    from overwriting each other's keys; `secret-tool lookup app holdco name <name>` still finds it."""
    vid = data.get("id")
    return ["app", "holdco", *(["vault", str(vid)] if vid else []), "name", name]


def _forget_keyring(data: dict, name: str) -> None:
    if shutil.which("secret-tool"):
        try:
            _secret_tool(["clear", *_attrs(data, name)])
        except VaultError:
            pass


# ---------------------------------------------------------------- file backend

def _master(data: dict, create: bool) -> tuple[bytes, str]:
    """(master key, where it comes from: "passphrase" or "keyfile"). create=True may add a salt to `data` or
    write data/vault.key."""
    passphrase = os.environ.get("HOLDCO_VAULT_PASSPHRASE", "")
    if passphrase:
        salt = data.get("salt")
        if not salt:
            if not create:
                raise VaultError("The vault file has no salt, so the passphrase cannot open it")
            salt = data["salt"] = _b64(os.urandom(16))
        cache_key = (hashlib.sha256(passphrase.encode("utf-8")).hexdigest(), salt)
        if cache_key not in _masters:
            try:
                raw_salt = _unb64(salt)
            except (ValueError, TypeError):
                raise VaultError("The vault file's salt is damaged") from None
            _masters[cache_key] = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), raw_salt, PBKDF2_ROUNDS)
        return _masters[cache_key], "passphrase"
    kp = _key_path()
    try:
        key = kp.read_bytes()
    except FileNotFoundError:
        if not create:
            raise VaultError(f"The vault key {kp} is missing, so the secrets in vault.json cannot be opened. Restore "
                             "it from a backup, or enter the keys again in Settings") from None
        key = _create_key(kp)
    except OSError as e:
        raise VaultError(f"The vault key {kp} cannot be read ({e.strerror or e})") from None
    if len(key) != 32:
        raise VaultError(f"The vault key {kp} is damaged (it should hold 32 bytes)")
    return key, "keyfile"


def _create_key(kp: Path) -> bytes:
    """32 random bytes in data/vault.key, readable by this user only (created with mode 0600, never widened)."""
    kp.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    try:
        fd = os.open(str(kp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        return kp.read_bytes()   # another thread or process made it first: use theirs
    with os.fdopen(fd, "wb") as f:
        f.write(key)
        f.flush()
        os.fsync(f.fileno())
    return key


def _keys(master: bytes) -> tuple[bytes, bytes]:
    return (hmac.new(master, b"enc", hashlib.sha256).digest(), hmac.new(master, b"mac", hashlib.sha256).digest())


def _xor_stream(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    """XOR with the keystream: block i is HMAC-SHA256(enc_key, nonce + i as 8 big-endian bytes)."""
    out = bytearray()
    for i in range(0, len(data), 32):
        block = hmac.new(enc_key, nonce + (i // 32).to_bytes(8, "big"), hashlib.sha256).digest()
        out += bytes(a ^ b for a, b in zip(data[i:i + 32], block))
    return bytes(out)


def _file_seal(data: dict, value: str) -> dict:
    data.setdefault("salt", _b64(os.urandom(16)))   # the file layout always carries one (used with a passphrase)
    master, source = _master(data, create=True)
    enc_key, mac_key = _keys(master)
    nonce = os.urandom(16)
    ct = _xor_stream(enc_key, nonce, value.encode("utf-8"))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return {"backend": "file", "nonce": _b64(nonce), "ct": _b64(ct), "tag": _b64(tag), "key": source}


def _file_open(data: dict, name: str, entry: dict) -> str:
    try:
        nonce, ct, tag = (_unb64(entry[k]) for k in ("nonce", "ct", "tag"))
    except (KeyError, ValueError, TypeError):
        raise VaultError(f"Secret {name} is damaged in the vault file; enter it again in Settings") from None
    master, source = _master(data, create=False)
    enc_key, mac_key = _keys(master)
    # The tag is checked before anything is decrypted: a wrong key or a changed byte stops here.
    if not hmac.compare_digest(hmac.new(mac_key, nonce + ct, hashlib.sha256).digest(), tag):
        stored = entry.get("key")
        if stored == "passphrase" and source == "keyfile":
            raise VaultError(f"Secret {name} was locked with HOLDCO_VAULT_PASSPHRASE, which is not set now; set it "
                             "again, or enter the secret again in Settings")
        if stored == "keyfile" and source == "passphrase":
            raise VaultError(f"Secret {name} was locked with data/vault.key, but HOLDCO_VAULT_PASSPHRASE is set now; "
                             "remove it, or enter the secret again in Settings")
        raise VaultError(f"Secret {name} failed its integrity check: the passphrase or data/vault.key differs from "
                         "the one it was stored with, or the vault file was changed. Enter it again in Settings")
    try:
        return _xor_stream(enc_key, nonce, ct).decode("utf-8")
    except UnicodeDecodeError:
        raise VaultError(f"Secret {name} is damaged in the vault file; enter it again in Settings") from None


# ---------------------------------------------------------------- the interface

def put(name: str, value: str) -> None:
    """Store a secret (replacing any earlier value). An empty value deletes it."""
    _check_name(name)
    if value is None or value == "":
        delete(name)
        return
    if not isinstance(value, str):
        raise VaultError(f"Secret {name} must be text")
    with _lock:
        _sync()
        data = _load()
        active = backend()
        data.setdefault("id", _random.token_hex(8))
        old = data["secrets"].get(name)
        if active == "file":
            entry = _file_seal(data, value)
        elif active == "dpapi":
            try:
                entry = {"backend": "dpapi", "blob": _b64(_dpapi(value.encode("utf-8"), protect=True))}
            except VaultError as e:
                raise VaultError(f"Windows could not protect secret {name} ({e})") from None
        else:
            r = _secret_tool(["store", "--label", f"Holdco {name}", *_attrs(data, name)], value)
            if r.returncode != 0:
                raise VaultError(f"The keyring did not store secret {name} ({_tool_error(r)})")
            entry = {"backend": "secret-service"}
        if old and old.get("backend") == "secret-service" and active != "secret-service":
            _forget_keyring(data, name)
        data["secrets"][name] = entry
        # The recorded backend is what the next start tries first. While other secrets still live with the recorded
        # one (the keyring, after a brief secret-tool outage at start sent this one to the file), keep it: switching
        # would strand them. Each entry records its own backend, so both kinds read.
        recorded = data.get("backend")
        if not (recorded and recorded != active
                and any(e.get("backend") == recorded for n, e in data["secrets"].items() if n != name)):
            data["backend"] = active
        _save(data)
        _remember(name, value)


def get(name: str) -> str | None:
    """The secret's value, or None when there is no such secret. Raises VaultError when it exists but cannot be
    read here (stored on another machine, wrong passphrase, damaged)."""
    _check_name(name)
    with _lock:
        _sync()
        if name in _values:
            return _values[name]
        data = _load()
        entry = data["secrets"].get(name)
        if entry is None:
            return None
        value = _open(data, name, entry)
        _remember(name, value)
        return value


def _keyring_down(data: dict, stored_with) -> bool:
    """A secret kept in the keyring this vault records as its own, while secret-tool does not answer: an outage
    (a locked or not yet started keyring), not a secret from another machine."""
    return stored_with == "secret-service" and data.get("backend") == "secret-service" and not _available(stored_with)


def _open(data: dict, name: str, entry: dict) -> str:
    """Read one entry with the backend it was stored with. That is usually the active one; it can differ after a
    keyring outage (new secrets went to the file) or a backend change, and either still reads while it works here."""
    stored_with, active = entry.get("backend"), backend()
    if stored_with != active and not (stored_with in BACKENDS and _available(stored_with)):
        if _keyring_down(data, stored_with):
            raise VaultError(f"Secret {name} is kept in the desktop keyring, which is unavailable right now; unlock "
                             "the keyring and restart Holdco, or enter it again in Settings")
        raise VaultError(f"Secret {name} was stored with {stored_with or 'an unknown backend'} on another machine; "
                         "enter it again in Settings")
    if stored_with == "file":
        return _file_open(data, name, entry)
    if stored_with == "dpapi":
        try:
            blob = _unb64(entry.get("blob"))
        except (ValueError, TypeError):
            raise VaultError(f"Secret {name} is damaged in the vault file; enter it again in Settings") from None
        try:
            return _dpapi(blob, protect=False).decode("utf-8")
        except (VaultError, UnicodeDecodeError) as e:
            raise VaultError(f"Windows could not unlock secret {name} ({e}); it was probably stored by another Windows "
                             "user or on another machine. Enter it again in Settings") from None
    r = _secret_tool(["lookup", *_attrs(data, name)])
    if r.returncode != 0 or not r.stdout:
        raise VaultError(f"Secret {name} is not in this computer's keyring (it was stored with secret-service, "
                         "perhaps on another machine); enter it again in Settings")
    return _strip_newline(r.stdout.decode("utf-8", "replace"))


def delete(name: str) -> None:
    """Remove a secret. Removing one that does not exist is fine."""
    _check_name(name)
    with _lock:
        _sync()
        data = _load()
        entry = data["secrets"].pop(name, None)
        if entry is not None:
            if entry.get("backend") == "secret-service":
                _forget_keyring(data, name)
            _save(data)
        _forget(name)


def names() -> list[str]:
    """Every stored secret's name, sorted (nothing is decrypted)."""
    with _lock:
        return sorted(_load()["secrets"])


def _remember(name: str, value: str) -> None:
    global _needles
    _values[name] = value
    _needles = None


def _forget(name: str) -> None:
    global _needles
    _values.pop(name, None)
    _needles = None


# ---------------------------------------------------------------- redaction

def _variants(value: str) -> set[str]:
    """The value as written, and as it appears inside JSON text. For a JSON secret (an OAuth token set), also the
    tokens and secrets inside it, which are what would actually leak."""
    out = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
    if value[:1] == "{":
        try:
            inner = json.loads(value)
        except ValueError:
            inner = None
        if isinstance(inner, dict):
            for k, v in inner.items():
                if isinstance(v, str) and _SECRET_FIELD_RE.search(str(k)):
                    out |= {v, json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1]}
    return out


def _known() -> list[str]:
    """The strings redact() replaces. The first call reads every secret once; later calls reuse the list until a
    put or delete changes it, so redact() does no decryption per call."""
    global _needles, _swept
    with _lock:
        _sync()
        if not _swept:
            _swept = True
            try:
                stored = names()
            except VaultError:
                stored = []
            for n in stored:
                if n not in _values:
                    try:
                        get(n)
                    except Exception:   # unreadable here: the Doctor's vault row says so
                        pass
        if _needles is None:
            found: set[str] = set()
            for v in _values.values():
                found |= _variants(v)
            _needles = sorted((s for s in found if len(s) >= MIN_REDACT), key=len, reverse=True)
        return _needles


def redact(text: str) -> str:
    """Replace every known secret value (8+ characters) with [redacted]."""
    if not text or not isinstance(text, str):
        return text
    try:
        for needle in _known():
            if needle in text:
                text = text.replace(needle, REDACTED)
    except Exception:   # redaction must never stop an event, a transcript or an audit row from being written
        pass
    return text


# ---------------------------------------------------------------- migration and the Doctor

def migrate() -> int:
    """Move plain-text secrets from the settings tables into the vault and leave "vault:<name>" markers.
    Runs at every start (db.init); a no-op once done. Returns how many secrets moved."""
    from . import db   # imported here: db imports this module lazily too
    moved = 0
    with _lock:
        for key in config.SECRET_SETTINGS:
            row = db.one("SELECT value FROM settings WHERE key=?", (key,))
            if not row:
                continue
            try:
                value = json.loads(row["value"])
            except (ValueError, TypeError):
                value = row["value"]
            if isinstance(value, str) and value and not value.startswith(MARKER):
                name = f"settings.{key}"
                put(name, value)
                db.run("UPDATE settings SET value=? WHERE key=?", (json.dumps(MARKER + name), key))
                moved += 1
        for c in db.q("SELECT id, settings FROM companies"):
            stored = db.loads(c["settings"], {})
            if not isinstance(stored, dict):
                continue
            markers = {}
            for key in config.SECRET_COMPANY_SETTINGS:
                value = stored.get(key)
                if isinstance(value, str) and value and not value.startswith(MARKER):
                    name = f"company.{c['id']}.{key}"
                    put(name, value)
                    markers[key] = MARKER + name
            if markers:
                with db._lock:   # read again under the lock, so a settings change made meanwhile is kept
                    now = db.loads(db.scalar("SELECT settings FROM companies WHERE id=?", (c["id"],)), {})
                    now = now if isinstance(now, dict) else {}
                    now.update(markers)
                    db.run("UPDATE companies SET settings=? WHERE id=?", (json.dumps(now), c["id"]))
                moved += len(markers)
    if moved:
        _scrub_database(db)
        print(f"Vault: {backend()}, moved {moved} secret{'' if moved == 1 else 's'} out of the settings tables")
    return moved


def _scrub_database(db) -> None:
    """The old plain-text values can linger in SQLite's free pages and write-ahead log after the update. Rebuild
    the file once so they are really gone. Best effort: a busy database is left as it is."""
    try:
        db.run("VACUUM")
        db.run("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        print(f"Vault: could not compact the database after moving secrets ({e})")


def doctor_rows() -> list[dict]:
    """The Doctor's 'vault' row: ok with the backend name; bad when the vault file cannot be read or a secret
    cannot be opened here."""
    from . import doctor   # imported here: doctor imports most of Holdco
    label = "Secrets vault"
    try:
        active = backend()
        with _lock:
            _sync()
            data = _load()
    except VaultError as e:
        return [doctor._row("vault", label, "bad", str(e), doctor._text(
            "Restore data/vault.json from a backup, or move it aside and enter your keys again in Settings."))]
    if not _available(active):
        why = {"dpapi": "DPAPI exists only on Windows",
               "secret-service": "secret-tool is missing or the desktop keyring did not answer"}.get(active, "")
        return [doctor._row("vault", label, "bad", f"HOLDCO_VAULT_BACKEND asks for {active}, but {why}.", doctor._text(
            "Set HOLDCO_VAULT_BACKEND to auto or file and restart Holdco."))]
    entries = data["secrets"]
    # Secrets whose backend does not work here. Those in the keyring this vault records as its own, while
    # secret-tool does not answer, are an outage (a warning); any other kind came from another machine.
    unreadable = {n for n, e in entries.items() if e.get("backend") != active
                  and not (e.get("backend") in BACKENDS and _available(e.get("backend")))}
    waiting = sorted(n for n in unreadable if _keyring_down(data, entries[n].get("backend")))
    foreign = sorted(unreadable - set(waiting))
    if foreign:
        others = sorted({str(entries[n].get("backend") or "an unknown backend") for n in foreign})
        shown = ", ".join(foreign[:6]) + (f" and {len(foreign) - 6} more" if len(foreign) > 6 else "")
        return [doctor._row("vault", label, "bad",
                            f"{len(foreign)} secret{'' if len(foreign) == 1 else 's'} stored with {' and '.join(others)} "
                            f"on another machine cannot be read here: {shown}.", doctor._text(
                                f"Enter these keys again in Settings (and on the connector cards); Holdco then keeps "
                                f"them with {active} on this PC."))]
    broken = []
    for n in sorted(set(entries) - set(waiting)):
        try:
            get(n)
        except VaultError as e:
            broken.append((n, str(e)))
    if broken:
        return [doctor._row("vault", label, "bad", f"{len(broken)} secret{'' if len(broken) == 1 else 's'} cannot be "
                            f"read: {broken[0][1]}", doctor._text(
                                "Enter the named keys again in Settings, or restore the passphrase or data/vault.key "
                                "they were stored with."))]
    if waiting:
        shown = ", ".join(waiting[:6]) + (f" and {len(waiting) - 6} more" if len(waiting) > 6 else "")
        return [doctor._row("vault", label, "warn",
                            f"keyring unavailable; new secrets are stored in the file vault. {len(waiting)} "
                            f"secret{'' if len(waiting) == 1 else 's'} in the keyring cannot be read until it answers "
                            f"again: {shown}.", doctor._text(
                                "Unlock the desktop keyring (or start it), then restart Holdco. Nothing needs to be "
                                "entered again."))]
    n = len(entries)
    if not n:
        return [doctor._row("vault", label, "ok", f"{active}: ready, no secrets stored yet.")]
    how = {"dpapi": "encrypted by Windows for this user", "secret-service": "kept in the desktop keyring",
           "file": ("locked with HOLDCO_VAULT_PASSPHRASE" if os.environ.get("HOLDCO_VAULT_PASSPHRASE")
                    else "locked with data/vault.key")}[active]
    return [doctor._row("vault", label, "ok", f"{active}: {n} secret{'' if n == 1 else 's'} stored, {how}.")]
