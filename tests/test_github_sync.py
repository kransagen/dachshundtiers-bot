"""Testy GitHub synchronizace github_sync.py (bez discord.py, mocknuté HTTP)."""

import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import github_sync


def _content(players):
    raw = json.dumps(players, ensure_ascii=False)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _get_ok(players, sha="sha123"):
    return SimpleNamespace(status_code=200, json=lambda: {"content": _content(players), "sha": sha})


def _get_404():
    return SimpleNamespace(status_code=404, json=lambda: {})


def _put(code):
    return SimpleNamespace(status_code=code, json=lambda: {})


def _merge(players, ign="adurytak", mode="AnchorPvP", tier="HT5"):
    """Idempotentní build_fn: aplikuje tier na (libovolný) seznam hráčů."""
    out = [dict(p) for p in (players or [])]
    player = next((p for p in out if p.get("username") == ign), None)
    if player is None:
        player = {"username": ign, "modes": {}, "history": {}}
        out.append(player)
    player["modes"][mode] = tier
    return out


class GithubSyncTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(github_sync, "GITHUB_TOKEN", "test-token")
        patch.start()
        self.addCleanup(patch.stop)

    def test_push_success(self):
        async def main():
            with (
                mock.patch.object(github_sync.requests, "get", return_value=_get_ok([], "sha1")),
                mock.patch.object(github_sync.requests, "put", return_value=_put(200)) as p,
            ):
                ok, msg, built = await github_sync.push_players(
                    "msg", _merge, success_message="OK!"
                )
                self.assertTrue(ok)
                self.assertEqual(msg, "OK!")
                self.assertEqual([x["username"] for x in built], ["adurytak"])
                # PUT nesl sha z GET
                _, kwargs = p.call_args
                self.assertEqual(kwargs["json"]["sha"], "sha1")

        asyncio.run(main())

    def test_push_conflict_then_retry(self):
        """409 → merge se znovu aplikuje na čerstvá data a PUT se zopakuje."""
        calls = {"n": 0}

        def put_side_effect(*a, **k):
            calls["n"] += 1
            return _put(200 if calls["n"] > 1 else 409)

        async def main():
            with (
                mock.patch.object(
                    github_sync.requests, "get",
                    side_effect=[_get_ok([], "sha1"), _get_ok([], "sha2")],
                ) as g,
                mock.patch.object(github_sync.requests, "put", side_effect=put_side_effect) as p,
            ):
                ok, msg, built = await github_sync.push_players("msg", _merge)
                self.assertTrue(ok)
                self.assertEqual(g.call_count, 2)
                self.assertEqual(p.call_count, 2)
                # druhý PUT nesl čerstvé sha z opětovného GET
                shas = [c.kwargs["json"]["sha"] for c in p.call_args_list]
                self.assertEqual(shas, ["sha1", "sha2"])

        asyncio.run(main())

    def test_push_persistent_conflict_fails(self):
        async def main():
            with (
                mock.patch.object(github_sync.requests, "get", return_value=_get_ok([], "sha1")),
                mock.patch.object(github_sync.requests, "put", return_value=_put(409)) as p,
            ):
                ok, msg, built = await github_sync.push_players("msg", _merge)
                self.assertFalse(ok)
                self.assertIn("409", msg)
                self.assertEqual(p.call_count, github_sync.MAX_PUSH_ATTEMPTS)
                # i při selhání víme, co by na web bylo zapsáno (poslední build)
                self.assertIsNotNone(built)

        asyncio.run(main())

    def test_push_no_token(self):
        with mock.patch.object(github_sync, "GITHUB_TOKEN", ""):

            async def main():
                ok, msg, built = await github_sync.push_players("msg", _merge)
                self.assertFalse(ok)
                self.assertIn("GITHUB_TOKEN", msg)
                self.assertIsNone(built)

            asyncio.run(main())

    def test_push_file_missing_creates_new(self):
        async def main():
            with (
                mock.patch.object(github_sync.requests, "get", return_value=_get_404()),
                mock.patch.object(github_sync.requests, "put", return_value=_put(201)) as p,
            ):
                ok, msg, built = await github_sync.push_players("msg", _merge)
                self.assertTrue(ok)
                # soubor neexistoval → PUT bez sha
                self.assertNotIn("sha", p.call_args.kwargs["json"])

        asyncio.run(main())

    def test_fetch_players_no_token_returns_none(self):
        with mock.patch.object(github_sync, "GITHUB_TOKEN", ""):

            async def main():
                self.assertEqual(await github_sync.fetch_players(), (None, None, None))

            asyncio.run(main())

    def test_fetch_players_ok_and_404(self):
        async def main():
            with mock.patch.object(github_sync.requests, "get", return_value=_get_ok([{"a": 1}], "s1")):
                players, sha, err = await github_sync.fetch_players()
                self.assertEqual(players, [{"a": 1}])
                self.assertEqual(sha, "s1")
                self.assertIsNone(err)
            with mock.patch.object(github_sync.requests, "get", return_value=_get_404()):
                players, sha, err = await github_sync.fetch_players()
                self.assertEqual(players, [])
                self.assertIsNone(sha)
                self.assertIsNone(err)

        asyncio.run(main())

    def test_push_applies_merge_to_fresh_data(self):
        """build_fn běží nad čerstvě staženými daty (ne nad starou lokální kopií)."""

        async def main():
            # GET vrací víc hráčů, než zná build_fn → ti nesmí zmizet
            remote = [{"username": "other", "modes": {}}]
            with (
                mock.patch.object(github_sync.requests, "get", return_value=_get_ok(remote, "sha1")),
                mock.patch.object(github_sync.requests, "put", return_value=_put(200)) as p,
            ):
                ok, msg, built = await github_sync.push_players("msg", _merge)
                self.assertTrue(ok)
                self.assertEqual(sorted(x["username"] for x in built), ["adurytak", "other"])
                sent = json.loads(base64.b64decode(p.call_args.kwargs["json"]["content"]))
                self.assertEqual(sorted(x["username"] for x in sent), ["adurytak", "other"])

        asyncio.run(main())

    def test_merge_fn_idempotent(self):
        """Aplikace build_fn dvakrát na stejný základ nezdvojí hráče."""
        once = _merge([], "adurytak", "AnchorPvP", "HT5")
        twice = _merge(once, "adurytak", "AnchorPvP", "HT5")
        matches = [p for p in twice if p["username"] == "adurytak"]
        self.assertEqual(len(matches), 1)


if __name__ == "__main__":
    unittest.main()