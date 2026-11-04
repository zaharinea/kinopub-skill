import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("kinopub", Path(__file__).parents[1] / "plugins/kinopub/skills/kinopub/scripts/kinopub.py")
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)

FOLDERS = {"status": 200, "items": [
    {"id": 101, "title": "Закладки Ани", "count": 5},
    {"id": 202, "title": "Золотая коллекция", "count": 3},
    {"id": 303, "title": "Закладки Бори", "count": 2}]}


class MemStore:
    def __init__(self, **kw):
        self.d = dict(kw)

    def get(self, name):
        return self.d.get(name)

    def set(self, name, value):
        self.d[name] = value

    def clear(self):
        self.d.clear()


class FakeTransport:
    """Отвечает по (method, path) заготовками; записывает вызовы."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, data=None, token=None):
        path = url.split(k.API, 1)[1]
        self.calls.append((method, path, data, token))
        r = self.routes.get((method, path))
        if callable(r):
            return r(data, token)
        if isinstance(r, list):
            return r.pop(0)
        return r if r is not None else (200, {"status": 200})


def client(routes, now=1000.0, **store):
    store.setdefault(k.KC_ACCESS, "acc")
    store.setdefault(k.KC_REFRESH, "ref")
    store.setdefault(k.KC_EXPIRES, str(int(now) + 3600))
    t = FakeTransport(routes)
    return k.Client(MemStore(**store), t, now=lambda: now), t


class TestParsing(unittest.TestCase):
    def test_item_id(self):
        self.assertEqual(k.item_id("74959"), 74959)
        self.assertEqual(k.item_id("https://kino.watch/item/view/74959/s1e1"), 74959)
        with self.assertRaises(k.KPError):
            k.item_id("дюна")

    def test_folder_by_url_title_id(self):
        c, _ = client({("GET", "/v1/bookmarks"): (200, FOLDERS)})
        self.assertEqual(c.resolve_folder("https://kino.watch/favorites/view?id=303")["id"], 303)
        self.assertEqual(c.resolve_folder("золотая")["id"], 202)
        self.assertEqual(c.resolve_folder("101")["id"], 101)

    def test_ambiguous_folder(self):
        c, _ = client({("GET", "/v1/bookmarks"): (200, FOLDERS)})
        with self.assertRaises(k.KPError) as e:
            c.resolve_folder("закладки")
        self.assertEqual(e.exception.code, 2)


class TestTokens(unittest.TestCase):
    def test_refresh_when_expiring(self):
        c, t = client({("POST", "/oauth2/token"): (200, {"access_token": "new", "refresh_token": "r2", "expires_in": 86400}),
                       ("GET", "/v1/bookmarks"): (200, FOLDERS)}, **{k.KC_EXPIRES: "1010"})
        c.folders()
        self.assertEqual(t.calls[0][1], "/oauth2/token")
        self.assertEqual(t.calls[1][3], "new")
        self.assertEqual(c.store.get(k.KC_REFRESH), "r2")

    def test_retry_on_401(self):
        c, t = client({("GET", "/v1/bookmarks"): [(401, {}), (200, FOLDERS)],
                       ("POST", "/oauth2/token"): (200, {"access_token": "new", "refresh_token": "r2", "expires_in": 60})})
        self.assertEqual(len(c.folders()), 3)
        self.assertEqual(t.calls[-1][3], "new")

    def test_refresh_failure_code3(self):
        c, _ = client({("POST", "/oauth2/token"): (400, {"error": "invalid_refresh_token"})}, **{k.KC_EXPIRES: "0"})
        with self.assertRaises(k.KPError) as e:
            c.folders()
        self.assertEqual(e.exception.code, 3)


class TestBookmarks(unittest.TestCase):
    def routes(self):
        return {("GET", "/v1/bookmarks"): (200, FOLDERS),
                ("GET", "/v1/bookmarks/get-item-folders"): (200, {"status": 200, "folders": []})}

    def test_add(self):
        c, t = client(self.routes())
        c.add("https://kino.watch/item/view/5", "101")
        self.assertIn(("POST", "/v1/bookmarks/add", {"item": 5, "folder": 101}, "acc"), t.calls)

    def test_remove_folder_required_or_all(self):
        c, t = client(self.routes())
        with self.assertRaises(k.KPError):
            c.remove(5)
        c.remove(5, "Ани")
        self.assertIn(("POST", "/v1/bookmarks/remove-item", {"item": 5, "folder": 101}, "acc"), t.calls)
        c.remove(5, all_folders=True)
        self.assertIn(("POST", "/v1/bookmarks/remove-item", {"item": 5}, "acc"), t.calls)

    def test_move_adds_then_removes(self):
        c, t = client(self.routes())
        c.move(5, "Ани", "Бори")
        writes = [x[1:3] for x in t.calls if x[0] == "POST"]
        self.assertEqual(writes, [("/v1/bookmarks/add", {"item": 5, "folder": 303}),
                                  ("/v1/bookmarks/remove-item", {"item": 5, "folder": 101})])

    def test_rmdir_requires_yes(self):
        c, t = client(self.routes())
        with self.assertRaises(k.KPError):
            c.rmdir("Бори")
        self.assertFalse([x for x in t.calls if x[1] == "/v1/bookmarks/remove-folder"])
        c.rmdir("Бори", yes=True)
        self.assertIn(("POST", "/v1/bookmarks/remove-folder", {"folder": 303}, "acc"), t.calls)

    def test_api_error_status(self):
        c, _ = client({("POST", "/v1/bookmarks/create"): (200, {"status": 500})})
        with self.assertRaises(k.KPError):
            c.mkdir("x")


class TestLogin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        k.STATE_DIR = Path(self.tmp.name)
        k.DEVICE_FILE = k.STATE_DIR / "device.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_device_flow(self):
        c, t = client({
            ("POST", "/oauth2/device"): [
                (200, {"code": "DC", "user_code": "AB12C", "verification_uri": "https://kino.watch/device",
                       "interval": 5, "expires_in": 300}),
                (400, {"error": "authorization_pending"}),
                (200, {"access_token": "A", "refresh_token": "R", "expires_in": 86400})],
            ("GET", "/v1/user"): (200, {"status": 200, "user": {"username": "u", "subscription": {}}}),
        }, **{k.KC_ACCESS: None, k.KC_REFRESH: None, k.KC_EXPIRES: None})
        start = c.login_start()
        self.assertEqual(start["user_code"], "AB12C")
        self.assertNotIn("DC", str(start))
        self.assertEqual(oct(os.stat(k.DEVICE_FILE).st_mode & 0o777), "0o600")
        res = c.login_wait("t", sleep=lambda s: None)
        self.assertEqual(res["user"]["username"], "u")
        self.assertEqual(c.store.get(k.KC_ACCESS), "A")
        self.assertFalse(k.DEVICE_FILE.exists())
        self.assertIn(("POST", "/v1/device/notify"), [(m, p) for m, p, *_ in t.calls])

    def test_logout_clears_tokens(self):
        c, t = client({})
        res = k.run(k.build_parser().parse_args(["logout"]), c)
        self.assertTrue(res["ok"])
        self.assertIsNone(c.store.get(k.KC_ACCESS))
        self.assertEqual(t.calls, [])

    def test_wait_without_start(self):
        c, _ = client({})
        with self.assertRaises(k.KPError) as e:
            c.login_wait("t")
        self.assertEqual(e.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
