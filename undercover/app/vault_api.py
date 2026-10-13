"""Vaulting's routes (the backups are core/vault.py; SafeHouse shows them):

    GET  /api/vault                      on/off, where, size choice, the last run, stored vs raw bytes, a run under way
    GET  /api/vault/check?folder=        would this folder do? {ok, why, warn, free, need} (nothing is switched)
    POST /api/vault/settings             {folder, size}: switch on (a checked folder), change the size, or off (folder "")
    POST /api/vault/run                  vault now, in the background
    GET  /api/vault/contents?snapshot=   what a snapshot holds, by kind and item, and whether each is here / missing / different
    POST /api/vault/restore              {keys, on_conflict: ask|both|replace|skip, snapshot}: bit-identical, never a silent overwrite;
                                         replace runs in the background ({"started": true}; the outcome in GET's "restore")
"""
from urllib.parse import parse_qs, urlparse

from ..core import vault
from ..core.voices import ModelError


def handle(h, method: str, p: str) -> bool:
    path = p
    if not path.startswith("/api/vault"):
        return False
    q = parse_qs(urlparse(h.path).query)
    one = lambda k: (q.get(k) or [""])[0]
    try:
        if method == "GET" and path == "/api/vault":
            h._json(vault.status())
        elif method == "GET" and path == "/api/vault/check":
            h._json(vault.check_folder(one("folder"), one("size") or None))
        elif method == "POST" and path == "/api/vault/settings":
            b = h._body()
            h._json(vault.set_folder(str(b.get("folder") or ""), b.get("size") or None))
        elif method == "POST" and path == "/api/vault/run":
            h._body()
            if not vault.settings()["folder"]:
                raise ModelError("The vault is off: pick a drive for it first.")
            h._json({"started": vault.run_now("asked"), **vault.status()})
        elif method == "GET" and path == "/api/vault/contents":
            h._json(vault.contents(stamp=one("snapshot") or None))
        elif method == "POST" and path == "/api/vault/restore":
            b = h._body()
            keys = b.get("keys")
            if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
                raise ModelError("Pick what to restore.")
            how = str(b.get("on_conflict") or "ask")
            if how == "replace":   # it vaults everything first: minutes, not a request's worth
                if not vault.restore_now(keys, how, b.get("snapshot") or None):
                    raise ModelError("A restore is already running.")
                h._json({"started": True, **vault.status()})
            else:
                h._json(vault.restore(keys, how, stamp=b.get("snapshot") or None))
        else:
            return False
    except ModelError as e:
        h._error(str(e))
    return True
