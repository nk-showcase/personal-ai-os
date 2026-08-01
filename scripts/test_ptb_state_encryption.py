#!/usr/bin/env python3
"""Plain-python tests (no pytest) for bot/ptb_state_cipher.EncryptedPicklePersistence.

Proves the Telegram framework's own conversation-state file is encrypted at rest, that the
flag-OFF path stays byte-compatible with the stock class, that a missing KEK fails closed
without writing anything, that a legacy cleartext file is migrated at LOAD time, and that the
stock class's pickling protocol and error contract are preserved.

Dependency handling: with python-telegram-bot + cryptography absent the script SKIPS (exit 0)
so a laptop without the runtime can still run the suite - EXCEPT when AIOS_TEST_REQUIRE_DEPS
is truthy (CI sets it), where a missing dependency is a FAILURE, not a silent green.

Run: PYTHONPATH=<repo> python3 scripts/test_ptb_state_encryption.py
"""
from __future__ import annotations

import asyncio
import os
import pickle
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_REQUIRE = (os.getenv("AIOS_TEST_REQUIRE_DEPS") or "").strip().lower() in {"1", "true", "yes", "on"}

try:
    from telegram.ext import PicklePersistence
    from telegram.ext._picklepersistence import _BotPickler
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: F401
except Exception as exc:  # pragma: no cover - environment-dependent
    if _REQUIRE:
        print(f"FAIL: required dependency missing ({type(exc).__name__}: {exc})")
        sys.exit(1)
    print(f"SKIP: dependency not installed ({type(exc).__name__}); set AIOS_TEST_REQUIRE_DEPS=1 to make this fatal")
    sys.exit(0)

TEST_KEK_HEX = "11" * 32
SECRET_TEXT = "personal dialogue text that must never sit in cleartext"

failed: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("PASS" if cond else "FAIL") + ": " + name)
    if not cond:
        failed.append(name)


def encryption_on() -> None:
    os.environ["AIOS_CONTEXT_ENCRYPTION"] = "1"
    os.environ["AIOS_CONTEXT_ALLOW_TEST_KEK"] = "1"
    os.environ["AIOS_CONTEXT_TEST_KEK"] = TEST_KEK_HEX
    os.environ.pop("AIOS_CONTEXT_IDENTITY_FILE", None)


def encryption_off() -> None:
    os.environ["AIOS_CONTEXT_ENCRYPTION"] = "0"
    os.environ.pop("AIOS_CONTEXT_ALLOW_TEST_KEK", None)
    os.environ.pop("AIOS_CONTEXT_TEST_KEK", None)


def encryption_on_without_key() -> None:
    os.environ["AIOS_CONTEXT_ENCRYPTION"] = "1"
    for var in ("AIOS_CONTEXT_ALLOW_TEST_KEK", "AIOS_CONTEXT_TEST_KEK",
                "AIOS_CONTEXT_IDENTITY_FILE", "AIOS_CONTEXT_KEK_FILE"):
        os.environ.pop(var, None)


async def write_state(persistence, chat_id: int, text: str) -> None:
    await persistence.update_chat_data(chat_id, {"draft": text})
    await persistence.flush()


def raises(fn, exc_types) -> bool:
    """True only when fn() raises an exception whose EXACT type is one of exc_types.
    A subclass does not count - that is what makes this an exact-type assertion."""
    expected = exc_types if isinstance(exc_types, tuple) else (exc_types,)
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - the type check below is the assertion
        if type(exc) in expected:
            return True
        print(f"       (unexpected {type(exc).__name__}: {exc})")
        return False
    return False


def main() -> int:
    from bot.context_cipher import DecryptQuarantine
    from bot.context_key import KekUnavailable
    from bot.ptb_state_cipher import MAGIC, EncryptedPicklePersistence

    tmp = Path(tempfile.mkdtemp(prefix="ptb-state-test-"))
    try:
        # ---- 1. flag ON: the file is an envelope and holds no cleartext ----
        encryption_on()
        p1 = tmp / "on.pkl"
        pers = EncryptedPicklePersistence(filepath=str(p1), on_flush=False)
        asyncio.run(write_state(pers, 42, SECRET_TEXT))
        blob = p1.read_bytes()
        check("flag ON: state file starts with the encrypted-file marker", blob.startswith(MAGIC))
        check("flag ON: the personal text is NOT in the file", SECRET_TEXT.encode() not in blob)
        check("flag ON: the file is not a readable pickle", _not_a_pickle(blob))
        check("flag ON: file mode is 0600", (p1.stat().st_mode & 0o777) == 0o600)
        check("flag ON: no temp file left behind", not (tmp / "on.pkl.tmp").exists())

        # ---- 2. round-trip through a FRESH instance (restart simulation) ----
        pers2 = EncryptedPicklePersistence(filepath=str(p1), on_flush=False)
        check("flag ON: a fresh instance decrypts the state back",
              asyncio.run(pers2.get_chat_data()).get(42, {}).get("draft") == SECRET_TEXT)

        # ---- 3. on_flush=True (write happens at flush, not per update) ----
        p_fl = tmp / "flush.pkl"
        pers_fl = EncryptedPicklePersistence(filepath=str(p_fl), on_flush=True)
        asyncio.run(pers_fl.update_chat_data(5, {"draft": SECRET_TEXT}))
        check("on_flush=True: nothing on disk before flush", not p_fl.exists())
        asyncio.run(pers_fl.flush())
        check("on_flush=True: encrypted file written at flush",
              p_fl.exists() and p_fl.read_bytes().startswith(MAGIC))

        # ---- 4. conversations + bot_data + user_data survive the round-trip ----
        p_all = tmp / "all.pkl"
        pers_all = EncryptedPicklePersistence(filepath=str(p_all), on_flush=False)
        asyncio.run(pers_all.update_bot_data({"b": SECRET_TEXT}))
        asyncio.run(pers_all.update_user_data(77, {"u": SECRET_TEXT}))
        asyncio.run(pers_all.update_conversation("conv", (1, 2), "state-x"))
        asyncio.run(pers_all.flush())
        pers_all2 = EncryptedPicklePersistence(filepath=str(p_all), on_flush=False)
        check("all categories round-trip (bot_data)",
              asyncio.run(pers_all2.get_bot_data()).get("b") == SECRET_TEXT)
        check("all categories round-trip (user_data)",
              asyncio.run(pers_all2.get_user_data()).get(77, {}).get("u") == SECRET_TEXT)
        check("all categories round-trip (conversations)",
              asyncio.run(pers_all2.get_conversations("conv")).get((1, 2)) == "state-x")
        check("no category leaks cleartext", SECRET_TEXT.encode() not in p_all.read_bytes())

        # ---- 5. flag OFF: plain pickle, readable by the stock class ----
        encryption_off()
        p2 = tmp / "off.pkl"
        pers3 = EncryptedPicklePersistence(filepath=str(p2), on_flush=False)
        asyncio.run(write_state(pers3, 7, "plain"))
        check("flag OFF: no encryption marker", not p2.read_bytes().startswith(MAGIC))
        stock = PicklePersistence(filepath=str(p2), on_flush=False)
        check("flag OFF: the stock class reads the same file",
              asyncio.run(stock.get_chat_data()).get(7, {}).get("draft") == "plain")

        # ---- 6. pickling protocol matches the stock class ----
        check("pickle protocol matches the stock class (HIGHEST_PROTOCOL)",
              p2.read_bytes()[:2] == _stock_protocol_prefix())

        # ---- 7. flag ON without a KEK: fail-closed, nothing written ----
        encryption_on_without_key()
        p3 = tmp / "nokey.pkl"
        pers4 = EncryptedPicklePersistence(filepath=str(p3), on_flush=False)
        check("flag ON without a KEK raises KekUnavailable",
              raises(lambda: asyncio.run(write_state(pers4, 1, SECRET_TEXT)), KekUnavailable))
        check("flag ON without a KEK writes NO file at all", not p3.exists())
        check("flag ON without a KEK leaves no temp file", not (tmp / "nokey.pkl.tmp").exists())

        # ---- 8. legacy cleartext file is migrated at LOAD time ----
        encryption_off()
        p4 = tmp / "legacy.pkl"
        legacy = PicklePersistence(filepath=str(p4), on_flush=False)
        asyncio.run(write_state(legacy, 9, "legacy draft"))
        check("legacy file starts out cleartext", b"legacy draft" in p4.read_bytes())
        encryption_on()
        pers5 = EncryptedPicklePersistence(filepath=str(p4), on_flush=False)
        check("legacy cleartext file is still readable after upgrade",
              asyncio.run(pers5.get_chat_data()).get(9, {}).get("draft") == "legacy draft")
        migrated = p4.read_bytes()
        check("legacy file is re-encrypted at LOAD, before any further write",
              migrated.startswith(MAGIC))
        check("legacy cleartext is gone from disk", b"legacy draft" not in migrated)

        # ---- 9. legacy file + flag ON + no key: fail-closed, file untouched ----
        encryption_off()
        p4b = tmp / "legacy2.pkl"
        legacy2 = PicklePersistence(filepath=str(p4b), on_flush=False)
        asyncio.run(write_state(legacy2, 8, "legacy draft 2"))
        before = p4b.read_bytes()
        encryption_on_without_key()
        pers5b = EncryptedPicklePersistence(filepath=str(p4b), on_flush=False)
        check("legacy migration without a KEK raises instead of running half-protected",
              raises(lambda: asyncio.run(pers5b.get_chat_data()), KekUnavailable))
        check("failed legacy migration leaves the original file byte-identical",
              p4b.read_bytes() == before)

        # ---- 10. tampering fails closed with the EXACT crypto error ----
        encryption_on()
        p5 = tmp / "tamper.pkl"
        pers6 = EncryptedPicklePersistence(filepath=str(p5), on_flush=False)
        asyncio.run(write_state(pers6, 3, SECRET_TEXT))
        raw = bytearray(p5.read_bytes())
        raw[-1] ^= 0xFF
        p5.write_bytes(bytes(raw))
        pers7 = EncryptedPicklePersistence(filepath=str(p5), on_flush=False)
        check("a tampered state file raises DecryptQuarantine (no cleartext fallback)",
              raises(lambda: asyncio.run(pers7.get_chat_data()), DecryptQuarantine))

        # ---- 11. ciphertext is bound to its absolute path (no relocation, same name) ----
        moved_dir = tmp / "elsewhere"
        moved_dir.mkdir()
        shutil.copyfile(p1, moved_dir / "on.pkl")  # SAME basename, different directory
        pers8 = EncryptedPicklePersistence(filepath=str(moved_dir / "on.pkl"), on_flush=False)
        check("ciphertext copied to another directory (same name) refuses to decrypt",
              raises(lambda: asyncio.run(pers8.get_chat_data()), DecryptQuarantine))

        # ---- 12. corrupt PLAINTEXT pickle keeps the stock TypeError contract ----
        encryption_off()
        p_bad = tmp / "bad.pkl"
        p_bad.write_bytes(b"not a pickle at all")
        pers_bad = EncryptedPicklePersistence(filepath=str(p_bad), on_flush=False)
        check("corrupt plain file raises TypeError, like the stock class",
              raises(lambda: asyncio.run(pers_bad.get_chat_data()), TypeError))

        # ---- 13. multi-file mode: every category file is encrypted and round-trips ----
        encryption_on()
        base = tmp / "multi"
        pers9 = EncryptedPicklePersistence(filepath=str(base), single_file=False, on_flush=False)
        asyncio.run(pers9.update_chat_data(11, {"draft": SECRET_TEXT}))
        asyncio.run(pers9.update_user_data(12, {"draft": SECRET_TEXT}))
        asyncio.run(pers9.update_bot_data({"draft": SECRET_TEXT}))
        asyncio.run(pers9.update_conversation("conv", (3, 4), "s"))
        asyncio.run(pers9.flush())
        produced = sorted(p.name for p in tmp.glob("multi_*"))
        check("multi-file mode: category files exist", len(produced) >= 3)
        check("multi-file mode: EVERY produced file is encrypted",
              all((tmp / n).read_bytes().startswith(MAGIC) for n in produced))
        check("multi-file mode: no file leaks the personal text",
              all(SECRET_TEXT.encode() not in (tmp / n).read_bytes() for n in produced))
        pers10 = EncryptedPicklePersistence(filepath=str(base), single_file=False, on_flush=False)
        check("multi-file mode: chat_data decrypts back",
              asyncio.run(pers10.get_chat_data()).get(11, {}).get("draft") == SECRET_TEXT)
        check("multi-file mode: conversations decrypt back",
              asyncio.run(pers10.get_conversations("conv")).get((3, 4)) == "s")

        # ---- 14. fault injection: a failing write leaves no temp file and no partial state ----
        p_fault = tmp / "fault.pkl"
        pers11 = EncryptedPicklePersistence(filepath=str(p_fault), on_flush=False)
        asyncio.run(write_state(pers11, 4, SECRET_TEXT))
        good = p_fault.read_bytes()
        real_replace = os.replace

        def boom(src, dst):  # noqa: ANN001 - test double
            raise OSError("injected failure")

        os.replace = boom
        try:
            check("an injected write failure propagates",
                  raises(lambda: asyncio.run(write_state(pers11, 4, "second value")), OSError))
        finally:
            os.replace = real_replace
        check("after a failed write the previous file is intact", p_fault.read_bytes() == good)
        check("after a failed write no temp file remains", not (tmp / "fault.pkl.tmp").exists())

        # ---- 15. a FAILED legacy migration must not degrade into empty state ----
        from bot.ptb_state_cipher import StateMigrationError

        encryption_off()
        p_mig = tmp / "migfail.pkl"
        legacy3 = PicklePersistence(filepath=str(p_mig), on_flush=False)
        asyncio.run(write_state(legacy3, 21, "legacy that must not vanish"))
        encryption_on()
        pers_mig = EncryptedPicklePersistence(filepath=str(p_mig), on_flush=False)
        os.replace = boom
        try:
            check("a failed legacy migration raises instead of returning empty state",
                  raises(lambda: asyncio.run(pers_mig.get_chat_data()), StateMigrationError))
        finally:
            os.replace = real_replace
        check("a failed legacy migration leaves the original file readable",
              b"legacy that must not vanish" in p_mig.read_bytes())

        # ---- 16. same, in multi-file mode ----
        encryption_off()
        base_m = tmp / "migmulti"
        legacy4 = PicklePersistence(filepath=str(base_m), single_file=False, on_flush=False)
        asyncio.run(write_state(legacy4, 22, "multi legacy"))
        encryption_on()
        pers_mig2 = EncryptedPicklePersistence(
            filepath=str(base_m), single_file=False, on_flush=False
        )
        os.replace = boom
        try:
            check("multi-file: a failed legacy migration raises, not silent empty state",
                  raises(lambda: asyncio.run(pers_mig2.get_chat_data()), StateMigrationError))
        finally:
            os.replace = real_replace

        # ---- 17. an unreadable (not absent) state file is NOT treated as empty ----
        encryption_on()
        p_perm = tmp / "perm.pkl"
        pers_perm = EncryptedPicklePersistence(filepath=str(p_perm), on_flush=False)
        asyncio.run(write_state(pers_perm, 31, SECRET_TEXT))
        os.chmod(p_perm, 0o000)
        pers_perm2 = EncryptedPicklePersistence(filepath=str(p_perm), on_flush=False)
        unreadable_ok = os.geteuid() == 0 or raises(
            lambda: asyncio.run(pers_perm2.get_chat_data()), PermissionError
        )
        check("an unreadable state file raises instead of starting empty", unreadable_ok)
        os.chmod(p_perm, 0o600)

        # ---- 18. a symlinked state path is rejected outright ----
        p_link = tmp / "link.pkl"
        p_link.symlink_to(p1)
        pers_link = EncryptedPicklePersistence(filepath=str(p_link), on_flush=False)
        check("a symlinked state path is rejected",
              raises(lambda: asyncio.run(pers_link.get_chat_data()), ValueError))

        # ---- 19. Bot objects and custom context_types survive the round-trip ----
        from telegram import Bot
        from telegram.ext import ContextTypes, PersistenceInput

        class MyChatData(dict):
            pass

        # A plain Bot (not ExtBot) cannot own callback_data, so that category is switched
        # off here; the point of this case is Bot substitution + custom context_types.
        bot = Bot("123456:dummy-token-for-pickling-only")
        p_bot = tmp / "bot.pkl"
        pers_bot = EncryptedPicklePersistence(
            filepath=str(p_bot), on_flush=False,
            store_data=PersistenceInput(callback_data=False),
            context_types=ContextTypes(chat_data=MyChatData),
        )
        pers_bot.set_bot(bot)
        # A Telegram object carrying a bot reference: this is what exercises the picklers'
        # Bot substitution (a bare Bot cannot even be deepcopied by the framework).
        import datetime as _dt

        from telegram import Chat, Message

        msg = Message(
            message_id=1,
            date=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
            chat=Chat(id=50, type="private"),
        )
        msg.set_bot(bot)
        asyncio.run(pers_bot.update_chat_data(50, {"msg": msg, "draft": SECRET_TEXT}))
        asyncio.run(pers_bot.flush())
        pers_bot2 = EncryptedPicklePersistence(
            filepath=str(p_bot), on_flush=False,
            store_data=PersistenceInput(callback_data=False),
            context_types=ContextTypes(chat_data=MyChatData),
        )
        pers_bot2.set_bot(bot)
        restored_bot = asyncio.run(pers_bot2.get_chat_data()).get(50, {})
        check("a stored Telegram object is re-bound to the live bot instance",
              restored_bot.get("msg") is not None and restored_bot["msg"].get_bot() is bot)
        # NOTE: like the stock class, persistence stores plain dicts; context_types wrapping
        # is the Application's job. The assertion is round-trip parity, not re-wrapping.
        check("custom context_types construction round-trips like the stock class",
              restored_bot.get("draft") == SECRET_TEXT)

        # ---- 20. callback_data round-trips too ----
        p_cb = tmp / "cb.pkl"
        pers_cb = EncryptedPicklePersistence(filepath=str(p_cb), on_flush=False)
        asyncio.run(pers_cb.update_callback_data(([("id-1", 1234.5, {"k": SECRET_TEXT})], {"x": "y"})))
        asyncio.run(pers_cb.flush())
        pers_cb2 = EncryptedPicklePersistence(filepath=str(p_cb), on_flush=False)
        cb = asyncio.run(pers_cb2.get_callback_data())
        check("callback_data round-trips", cb is not None and cb[1] == {"x": "y"})
        check("callback_data is not on disk in cleartext",
              SECRET_TEXT.encode() not in p_cb.read_bytes())

        # ---- 21. short writes are completed, not silently truncated ----
        encryption_on()
        p_short = tmp / "short.pkl"
        pers_short = EncryptedPicklePersistence(filepath=str(p_short), on_flush=False)
        real_write = os.write

        def dribble(fd, data):  # noqa: ANN001 - test double: at most 7 bytes per call
            return real_write(fd, bytes(data)[:7])

        os.write = dribble
        try:
            asyncio.run(write_state(pers_short, 61, SECRET_TEXT))
        finally:
            os.write = real_write
        pers_short2 = EncryptedPicklePersistence(filepath=str(p_short), on_flush=False)
        check("a dribbling os.write still produces a complete, decryptable file",
              asyncio.run(pers_short2.get_chat_data()).get(61, {}).get("draft") == SECRET_TEXT)

        # ---- 22. a directory-fsync failure propagates (durability is not optional) ----
        import stat as _stat

        p_dirf = tmp / "dirf.pkl"
        pers_dirf = EncryptedPicklePersistence(filepath=str(p_dirf), on_flush=False)
        real_fsync = os.fsync

        def dir_fsync_fails(fd):  # noqa: ANN001 - test double
            if _stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory fsync failure")
            return real_fsync(fd)

        os.fsync = dir_fsync_fails
        try:
            check("a failed directory fsync propagates",
                  raises(lambda: asyncio.run(write_state(pers_dirf, 62, SECRET_TEXT)), OSError))
        finally:
            os.fsync = real_fsync
        check("the file itself was still installed encrypted before the fsync raise",
              p_dirf.exists() and p_dirf.read_bytes().startswith(MAGIC))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


def _stock_protocol_prefix() -> bytes:
    """First two bytes a stock _BotPickler dump produces (pickle protocol opcode)."""
    import io as _io

    buf = _io.BytesIO()
    _BotPickler(None, buf, protocol=pickle.HIGHEST_PROTOCOL).dump({"x": 1})
    return buf.getvalue()[:2]


def _not_a_pickle(blob: bytes) -> bool:
    try:
        pickle.loads(blob)
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
