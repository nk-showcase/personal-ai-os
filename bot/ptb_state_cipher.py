"""bot/ptb_state_cipher.py — at-rest encryption for the Telegram framework's own state file.

The framework (python-telegram-bot) persists conversation state — `chat_data`, `user_data`,
`bot_data`, callback data and ConversationHandler states — by pickling it to disk after every
update, so an auto-restart does not drop an in-flight dialogue. That file is written by the
framework, NOT by the storage layer, so it bypassed the at-rest cipher and held personal
message text in cleartext (protected only by directory permissions).

:class:`EncryptedPicklePersistence` closes that gap WITHOUT re-implementing any cryptography:
it keeps the framework's own pickling (same picklers, same protocol) and encrypts/decrypts the
resulting BYTES through the same single-encryptor chokepoint every other store uses
(``context_cipher.encrypt_for_storage`` -> scheme-3 AEAD envelope).

Three-state contract, identical to the rest of the system (no fourth state):
    flag OFF          -> plain pickle, byte-compatible with the stock class (demo/dev path)
    flag ON + key     -> encrypted envelope on disk
    flag ON + no key  -> KekUnavailable, and NOTHING is written (never a silent cleartext
                         downgrade; no partial or temp file is left behind either)

Legacy migration is IMMEDIATE and VALIDATED: a cleartext file left by an earlier deployment is
read, unpickled (so a corrupt file is never replaced by an encrypted corrupt file), and only
then re-written encrypted — inside the same load call, before any further update.

Deliberate divergence from the stock class: only a genuinely ABSENT file yields empty state.
The stock class treats every ``OSError`` as "no file", which would turn a failed migration or
an unreadable state file into a silent, empty start — exactly the failure an encryption
boundary must not have. Here anything other than "file not found" propagates.

AAD binding: domain ``ptb_state``, row_id = the file's LEXICAL ABSOLUTE PATH (expanduser +
abspath, symlinks NOT resolved — ``os.replace`` replaces a symlink itself, so a resolved
target would stop matching after the first write; symlinked state paths are rejected outright).
Ciphertext therefore cannot be decrypted from another path, including a same-named file in
another directory.

KNOWN LIMIT (documented, not silently accepted): the envelope carries no monotonic version, so
someone able to write into the state directory can restore an OLDER envelope of the SAME path
and it will authenticate. That is rollback of the operator's own dialogue state inside a 0700
owner-only directory; it exposes no new plaintext and is out of scope for this boundary.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import pickle
from pathlib import Path

from telegram.ext import PicklePersistence
from telegram.ext._picklepersistence import _BotPickler, _BotUnpickler

from .context_cipher import decrypt_for_storage, encrypt_for_storage
from .context_key import encryption_enabled

log = logging.getLogger(__name__)

# Marks a file written by this class. Plain-pickle files never start with it.
MAGIC = b"AIOS-PTB-ENC1\n"
_DOMAIN = "ptb_state"
_FIELD = "pickle"
_MODE = 0o600


class StateMigrationError(RuntimeError):
    """A legacy cleartext state file could not be re-written encrypted. Never swallowed:
    starting with empty state here would hide a failed encryption migration."""


class EncryptedPicklePersistence(PicklePersistence):
    """PicklePersistence whose on-disk payload is encrypted at rest (see module docstring)."""

    # ---------- path identity ----------

    @staticmethod
    def _row_id(path: Path) -> str:
        """AAD identity: the LEXICAL absolute path (symlinks not resolved — see docstring)."""
        return os.path.abspath(os.path.expanduser(str(path)))

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise ValueError(
                f"state path {path.name} is a symlink; point the persistence at a real file "
                "(os.replace would swap the link itself and break the AAD binding)"
            )

    # ---------- byte-level I/O ----------

    def _write_bytes(self, path: Path, payload: bytes) -> None:
        """Encrypt (when flag+key are on) and write atomically. Encryption happens BEFORE any
        file is opened, so a fail-closed raise leaves neither a target nor a temp file. Mode
        0600 is enforced at creation AND verified — a filesystem that cannot honour it is an
        error, not a shrug."""
        self._reject_symlink(path)
        envelope = encrypt_for_storage(
            _DOMAIN, self._row_id(path), _FIELD, base64.b64encode(payload).decode("ascii")
        )
        blob = payload if envelope is None else MAGIC + envelope
        tmp = path.with_name(path.name + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _MODE)
            try:
                # write-all loop: a single os.write may legally write fewer bytes.
                view = memoryview(blob)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
                if (os.fstat(fd).st_mode & 0o777) != _MODE:
                    os.fchmod(fd, _MODE)
                    if (os.fstat(fd).st_mode & 0o777) != _MODE:
                        raise OSError(f"cannot enforce mode {oct(_MODE)} on the state file")
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            # Never leave a half-written temp file (encrypted or not) behind.
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        # Directory fsync is MANDATORY: the rename must be durable, otherwise a crash right
        # after a legacy migration could resurrect the cleartext file the migration replaced.
        # A failure here is noisy by design — the file on disk is already the new encrypted
        # one, so the raise reports a durability problem, not a data problem.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _load_object(self, path: Path):
        """Read one state file and return the unpickled object.

        Raises FileNotFoundError when the file does not exist (the only "empty state" case).
        Decryption failures, key problems and migration failures all propagate.
        """
        self._reject_symlink(path)
        with path.open("rb") as file:
            blob = file.read()

        if blob.startswith(MAGIC):
            b64 = decrypt_for_storage(
                blob[len(MAGIC):], domain=_DOMAIN, row_id=self._row_id(path), field=_FIELD
            )
            return self._unpickle(base64.b64decode(b64), path.name)

        # Legacy cleartext file: validate BEFORE replacing it, so a corrupt file is never
        # turned into an encrypted corrupt file (and stays available for recovery).
        obj = self._unpickle(blob, path.name)
        if encryption_enabled():
            log.warning(
                "ptb state file %s was cleartext (legacy); re-writing it encrypted now",
                path.name,
            )
            try:
                self._write_bytes(path, blob)
            except OSError as exc:
                raise StateMigrationError(
                    f"could not re-write {path.name} encrypted: {type(exc).__name__}"
                ) from exc
        return obj

    # ---------- pickling (framework contract preserved) ----------

    def _pickle(self, data: object) -> bytes:
        buf = io.BytesIO()
        _BotPickler(self.bot, buf, protocol=pickle.HIGHEST_PROTOCOL).dump(data)
        return buf.getvalue()

    def _unpickle(self, payload: bytes, filename: str) -> object:
        """Mirror the stock class's error contract: unpickling problems surface as TypeError
        with the same wording. Crypto failures are NOT swallowed — they stay fail-closed."""
        try:
            return _BotUnpickler(self.bot, io.BytesIO(payload)).load()
        except pickle.UnpicklingError as exc:
            raise TypeError(f"File {filename} does not contain valid pickle data") from exc
        except Exception as exc:
            raise TypeError(f"Something went wrong unpickling {filename}") from exc

    # ---------- overrides of the framework's four file-I/O methods ----------

    def _dump_singlefile(self) -> None:
        data = {
            "conversations": self.conversations,
            "user_data": self.user_data,
            "chat_data": self.chat_data,
            "bot_data": self.bot_data,
            "callback_data": self.callback_data,
        }
        self._write_bytes(self.filepath, self._pickle(data))

    def _load_singlefile(self) -> None:
        try:
            data = self._load_object(self.filepath)
        except FileNotFoundError:
            self.conversations = {}
            self.user_data = {}
            self.chat_data = {}
            self.bot_data = self.context_types.bot_data()
            self.callback_data = None
            return
        self.user_data = data["user_data"]
        self.chat_data = data["chat_data"]
        # Backwards compatibility with files written before bot_data/callback_data existed.
        self.bot_data = data.get("bot_data", self.context_types.bot_data())
        self.callback_data = data.get("callback_data", {})
        self.conversations = data["conversations"]

    def _dump_file(self, filepath: Path, data: object) -> None:
        self._write_bytes(Path(filepath), self._pickle(data))

    def _load_file(self, filepath: Path):
        try:
            return self._load_object(Path(filepath))
        except FileNotFoundError:
            return None
