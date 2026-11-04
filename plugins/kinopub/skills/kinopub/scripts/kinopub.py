# /// script
# requires-python = ">=3.10"
# dependencies = ["keyring>=24,<26"]
# ///
"""Кинопаб (kino.watch, kino.pub) CLI: закладки, поиск, «Буду смотреть» через API v1.

Вход - OAuth device flow (как ТВ-устройство), токены в системном хранилище
секретов через keyring (macOS Keychain, Secret Service, Windows Credential Manager).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
from pathlib import Path
from urllib import error, parse, request

__version__ = "0.1.0"

API = os.environ.get("KINOPUB_API", "https://api.srvkp.com")
SITE = "https://kino.watch"
# Публичный client Kodi-плагина quarckster/kodi.kino.pub, на который ссылается kino.watch.
CLIENT_ID = "xbmc"
CLIENT_SECRET = "cgg3gtifu46urtfp2zp1nqtba0k2ezxh"
UA = f"Mozilla/5.0 kinopub-skill/{__version__}"
TIMEOUT = 30

KEYRING_SERVICE = os.environ.get("KINOPUB_KEYRING_SERVICE", "kinopub-skill")
KC_ACCESS = "access_token"
KC_REFRESH = "refresh_token"
KC_EXPIRES = "expires_at"
_cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
STATE_DIR = Path(os.environ.get("KINOPUB_STATE_DIR") or _cache / "kinopub")
DEVICE_FILE = STATE_DIR / "device.json"
DEFAULT_TITLE = f"kinopub-skill ({platform.node().split('.')[0] or platform.system()})"


class KPError(Exception):
    def __init__(self, message, code=1, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


# ---------- хранилище токенов ----------

class Keychain:
    """Секреты в системном хранилище: сервис KEYRING_SERVICE, имя записи - KC_*."""

    def __init__(self, service=KEYRING_SERVICE):
        import keyring

        self.kr = keyring
        self.service = service

    def get(self, name):
        return self.kr.get_password(self.service, name)

    def set(self, name, value):
        self.kr.set_password(self.service, name, value)

    def clear(self):
        import keyring.errors

        for name in (KC_ACCESS, KC_REFRESH, KC_EXPIRES):
            try:
                self.kr.delete_password(self.service, name)
            except keyring.errors.PasswordDeleteError:
                pass


# ---------- HTTP ----------

def http(method, url, data=None, token=None):
    body = parse.urlencode(data).encode() if data is not None and method == "POST" else None
    if data is not None and method == "GET":
        url = f"{url}?{parse.urlencode(data)}"
    req = request.Request(url, data=body, method=method, headers={"User-Agent": UA, "Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw[:300].decode(errors="replace")}
        return e.code, payload
    except error.URLError as e:
        raise KPError(f"сеть: {e.reason}", 1) from e


class Client:
    def __init__(self, store=None, transport=http, now=time.time):
        self.store = store
        self.transport = transport
        self.now = now

    # --- токены ---
    def _save_tokens(self, resp):
        self.store.set(KC_ACCESS, resp["access_token"])
        self.store.set(KC_REFRESH, resp["refresh_token"])
        self.store.set(KC_EXPIRES, str(int(self.now()) + int(resp.get("expires_in", 86400))))

    def refresh(self):
        rt = self.store.get(KC_REFRESH)
        if not rt:
            raise KPError("нет токена: выполните `run login start`", 3)
        st, resp = self.transport("POST", f"{API}/oauth2/token", {
            "grant_type": "refresh_token", "refresh_token": rt,
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        if st != 200 or "access_token" not in resp:
            raise KPError("refresh не удался, нужен новый вход: `run login start`", 3,
                          status=st, error=resp.get("error"))
        self._save_tokens(resp)
        return resp["access_token"]

    def token(self):
        tok = self.store.get(KC_ACCESS)
        exp = int(self.store.get(KC_EXPIRES) or 0)
        if not tok or exp - self.now() < 120:
            return self.refresh()
        return tok

    def call(self, method, path, data=None):
        url = f"{API}/v1/{path}"
        st, resp = self.transport(method, url, data, self.token())
        if st == 401:
            st, resp = self.transport(method, url, data, self.refresh())
        if st == 401 or st == 403:
            raise KPError("доступ запрещён: проверьте подписку или выполните вход заново", 3, status=st)
        if st == 404:
            raise KPError("не найдено", 4, path=path)
        if st != 200 or (isinstance(resp, dict) and resp.get("status", 200) != 200):
            raise KPError(f"API вернул {st}", 1, path=path, response=resp)
        return resp

    # --- device flow ---
    def login_start(self):
        st, resp = self.transport("POST", f"{API}/oauth2/device", {
            "grant_type": "device_code", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        if st != 200 or "code" not in resp:
            raise KPError("не удалось получить код устройства", 1, status=st, error=resp.get("error"))
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state = {"code": resp["code"], "interval": int(resp.get("interval", 5)),
                 "expires_at": int(self.now()) + int(resp.get("expires_in", 300))}
        DEVICE_FILE.write_text(json.dumps(state))
        DEVICE_FILE.chmod(0o600)
        return {"user_code": resp["user_code"], "verification_uri": resp.get("verification_uri", f"{SITE}/device"),
                "expires_in": resp.get("expires_in", 300),
                "next": "пользователь вводит user_code на verification_uri, затем `run login wait`"}

    def login_wait(self, title, sleep=time.sleep):
        if not DEVICE_FILE.exists():
            raise KPError("нет ожидающего входа: сначала `run login start`", 2)
        state = json.loads(DEVICE_FILE.read_text())
        while self.now() < state["expires_at"]:
            st, resp = self.transport("POST", f"{API}/oauth2/device", {
                "grant_type": "device_token", "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET, "code": state["code"]})
            if st == 200 and "access_token" in resp:
                self._save_tokens(resp)
                DEVICE_FILE.unlink(missing_ok=True)
                self.call("POST", "device/notify", {"title": title, "hardware": platform.machine() or "unknown",
                                                    "software": f"kinopub-skill {__version__}"})
                return {"ok": True, "user": self.whoami()}
            err = resp.get("error")
            if st != 429 and err not in (None, "authorization_pending"):
                DEVICE_FILE.unlink(missing_ok=True)
                raise KPError(f"вход отклонён: {err}", 3, status=st)
            sleep(state["interval"])
        DEVICE_FILE.unlink(missing_ok=True)
        raise KPError("код истёк, начните заново: `run login start`", 3)

    # --- данные ---
    def whoami(self):
        u = self.call("GET", "user").get("user", {})
        sub = u.get("subscription", {})
        return {"username": u.get("username"), "subscription_active": sub.get("active"),
                "subscription_days": sub.get("days"),
                "subscription_end": time.strftime("%Y-%m-%d", time.localtime(sub["end_time"]))
                if sub.get("end_time") else None}

    def folders(self):
        return [fold(f) for f in self.call("GET", "bookmarks").get("items", [])]

    def resolve_folder(self, ref):
        ref = str(ref).strip()
        m = _FOLDER_RE.search(ref)
        if m:
            ref = m.group(1)
        all_ = self.folders()
        if ref.isdigit():
            for f in all_:
                if f["id"] == int(ref):
                    return f
            raise KPError(f"папки с id {ref} нет", 4, folders=all_)
        low = ref.casefold()
        exact = [f for f in all_ if f["title"].casefold() == low]
        if len(exact) == 1:
            return exact[0]
        part = [f for f in all_ if low in f["title"].casefold()]
        if len(part) == 1:
            return part[0]
        raise KPError(f"папка «{ref}» не найдена или неоднозначна", 4 if not part else 2,
                      candidates=part or all_)

    def show(self, folder, page=1, perpage=50):
        f = self.resolve_folder(folder)
        resp = self.call("GET", f"bookmarks/{f['id']}", {"page": page, "perpage": perpage})
        return {"folder": f, "items": [brief(i) for i in resp.get("items", [])],
                "pagination": resp.get("pagination")}

    def search(self, q, type_=None, limit=10):
        params = {"q": q, "perpage": limit}
        if type_:
            params["type"] = type_
        return [brief(i) for i in self.call("GET", "items/search", params).get("items", [])][:limit]

    def item(self, ref):
        iid = item_id(ref)
        it = self.call("GET", f"items/{iid}").get("item", {})
        out = brief(it)
        out.update({"plot": (it.get("plot") or "")[:500] or None,
                    "genres": [g.get("title") for g in it.get("genres", [])],
                    "countries": [c.get("title") for c in it.get("countries", [])],
                    "in_watchlist": it.get("in_watchlist"),
                    "folders": self.item_folders(iid)})
        return out

    def item_folders(self, iid):
        return [fold(f) for f in self.call("GET", "bookmarks/get-item-folders", {"item": iid}).get("folders", [])]

    def add(self, ref, folder):
        iid, f = item_id(ref), self.resolve_folder(folder)
        self.call("POST", "bookmarks/add", {"item": iid, "folder": f["id"]})
        return {"ok": True, "item": iid, "added_to": f, "now_in": self.item_folders(iid)}

    def remove(self, ref, folder=None, all_folders=False):
        iid = item_id(ref)
        data = {"item": iid}
        f = None
        if not all_folders:
            if folder is None:
                raise KPError("укажите папку или --all", 2)
            f = self.resolve_folder(folder)
            data["folder"] = f["id"]
        self.call("POST", "bookmarks/remove-item", data)
        return {"ok": True, "item": iid, "removed_from": f or "все папки", "now_in": self.item_folders(iid)}

    def move(self, ref, src, dst):
        iid, s, d = item_id(ref), self.resolve_folder(src), self.resolve_folder(dst)
        self.call("POST", "bookmarks/add", {"item": iid, "folder": d["id"]})
        self.call("POST", "bookmarks/remove-item", {"item": iid, "folder": s["id"]})
        return {"ok": True, "item": iid, "from": s, "to": d, "now_in": self.item_folders(iid)}

    def mkdir(self, title):
        return {"ok": True, "folder": fold(self.call("POST", "bookmarks/create", {"title": title}).get("folder", {}))}

    def rmdir(self, folder, yes=False):
        f = self.resolve_folder(folder)
        if not yes:
            raise KPError("удаление папки необратимо: покажите папку пользователю и повторите с --yes", 2, folder=f)
        self.call("POST", "bookmarks/remove-folder", {"folder": f["id"]})
        return {"ok": True, "removed": f}

    def watchlist(self):
        items = self.call("GET", "watching/serials", {"subscribed": 1}).get("items", [])
        return [{"id": i.get("id"), "title": i.get("title"), "total": i.get("total"),
                 "watched": i.get("watched"), "new": i.get("new"), "url": f"{SITE}/item/view/{i.get('id')}"}
                for i in items]

    def watchlist_toggle(self, ref):
        iid = item_id(ref)
        resp = self.call("GET", "watching/togglewatchlist", {"id": iid})
        return {"ok": True, "item": iid, "in_watchlist": resp.get("watching")}


# ---------- форматирование ----------

def fold(f):
    return {"id": f.get("id"), "title": f.get("title"), "count": f.get("count")}


def brief(i):
    return {k: v for k, v in {
        "id": i.get("id"), "title": i.get("title"), "year": i.get("year"), "type": i.get("type"),
        "kinopoisk": i.get("kinopoisk_rating"), "imdb": i.get("imdb_rating"),
        "url": f"{SITE}/item/view/{i.get('id')}"}.items() if v not in (None, "")}


_ITEM_RE = re.compile(r"/item/view/(\d+)")
_FOLDER_RE = re.compile(r"/(?:favorites|bookmarks)/view\?(?:.*&)?id=(\d+)")


def item_id(ref):
    ref = str(ref).strip()
    if ref.isdigit():
        return int(ref)
    m = _ITEM_RE.search(ref)
    if m:
        return int(m.group(1))
    raise KPError(f"не понимаю id фильма «{ref}»: нужен число или ссылка …/item/view/<id>", 2)


# ---------- CLI ----------

def build_parser():
    p = argparse.ArgumentParser(prog="kinopub", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="вход как ТВ-устройство")
    lg.add_argument("step", choices=["start", "wait"])
    lg.add_argument("--title", default=DEFAULT_TITLE, help="имя устройства в аккаунте")
    sub.add_parser("logout", help="удалить токены из хранилища (устройство на сайте не отвязывается)")
    sub.add_parser("whoami")
    sub.add_parser("folders")
    s = sub.add_parser("show", help="фильмы в папке")
    s.add_argument("folder")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--perpage", type=int, default=50)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--type", choices=["movie", "serial", "tvshow", "3d", "concert", "documovie", "docuserial"])
    s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("item", help="карточка и папки фильма")
    s.add_argument("item")
    s = sub.add_parser("add", help="добавить в папку")
    s.add_argument("item")
    s.add_argument("folder")
    s = sub.add_parser("remove", help="убрать из папки; --all - из всех")
    s.add_argument("item")
    s.add_argument("folder", nargs="?")
    s.add_argument("--all", action="store_true", dest="all_folders")
    s = sub.add_parser("move")
    s.add_argument("item")
    s.add_argument("src")
    s.add_argument("dst")
    s = sub.add_parser("mkdir")
    s.add_argument("title")
    s = sub.add_parser("rmdir")
    s.add_argument("folder")
    s.add_argument("--yes", action="store_true")
    w = sub.add_parser("watchlist", help="«Буду смотреть»")
    w.add_argument("action", nargs="?", choices=["list", "toggle"], default="list")
    w.add_argument("item", nargs="?")
    return p


def run(args, client):
    c = args.cmd
    if c == "logout":
        client.store.clear()
        return {"ok": True, "note": "токены удалены; устройство отвяжите на сайте в списке устройств"}
    if c == "login":
        return client.login_start() if args.step == "start" else client.login_wait(args.title)
    if c == "watchlist":
        if args.action == "toggle":
            if not args.item:
                raise KPError("watchlist toggle <id|ссылка>", 2)
            return client.watchlist_toggle(args.item)
        return client.watchlist()
    return {
        "whoami": lambda: client.whoami(),
        "folders": lambda: client.folders(),
        "show": lambda: client.show(args.folder, args.page, args.perpage),
        "search": lambda: client.search(args.query, args.type, args.limit),
        "item": lambda: client.item(args.item),
        "add": lambda: client.add(args.item, args.folder),
        "remove": lambda: client.remove(args.item, args.folder, args.all_folders),
        "move": lambda: client.move(args.item, args.src, args.dst),
        "mkdir": lambda: client.mkdir(args.title),
        "rmdir": lambda: client.rmdir(args.folder, args.yes),
    }[c]()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        out = run(args, Client(Keychain()))
    except KPError as e:
        print(json.dumps({"error": str(e), **e.extra}, ensure_ascii=False), file=sys.stderr)
        return e.code
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
