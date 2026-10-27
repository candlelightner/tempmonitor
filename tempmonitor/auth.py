import hashlib
import hmac
import os
import secrets
import time

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError

from .models import Credentials, PasswordChange
from .store import Store

COOKIE = "tm_session"
SESSION_SECONDS = 7 * 86400


class Auth:
    def __init__(self, store: Store):
        self.store = store
        self.hasher = PasswordHasher(type=Type.ID)
        self.dummy = self.hasher.hash(secrets.token_urlsafe(24))
        self.setup_file = store.path.parent / "setup-token"
        if not self.has_admin() and not self.setup_file.exists():
            with open(self.setup_file, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
                f.write(secrets.token_urlsafe(32))

    def has_admin(self):
        with self.store.transaction() as db:
            return db.execute("SELECT 1 FROM admin").fetchone() is not None

    def setup(self, credentials: Credentials, token: str):
        with self.store.transaction(immediate=True) as db:
            if db.execute("SELECT 1 FROM admin").fetchone():
                raise ValueError("Administrator already configured")
            if not hmac.compare_digest(token, self.setup_file.read_text().strip()):
                raise PermissionError("Invalid setup token")
            db.execute("INSERT INTO admin VALUES(1,?,?)",
                       (credentials.username, self.hasher.hash(credentials.password.get_secret_value())))
        self.setup_file.unlink(missing_ok=True)

    def login(self, credentials: Credentials, peer: str) -> str:
        now = time.time()
        with self.store.transaction(immediate=True) as db:
            db.execute("DELETE FROM attempts WHERE at<?", (now - 300,))
            count = db.execute("SELECT count(*) FROM attempts WHERE peer=?", (peer,)).fetchone()[0]
            total = db.execute("SELECT count(*) FROM attempts").fetchone()[0]
            if count >= 5 or total >= 30:
                raise TimeoutError("Too many attempts; retry in five minutes")
            db.execute("INSERT INTO attempts VALUES (?,?)", (peer, now))
            admin = db.execute("SELECT * FROM admin").fetchone()
        try:
            self.hasher.verify(admin["password"] if admin else self.dummy,
                               credentials.password.get_secret_value())
            valid = admin is not None and hmac.compare_digest(admin["username"], credentials.username)
        except VerificationError:
            valid = False
        if not valid:
            raise PermissionError("Invalid username or password")
        token = secrets.token_urlsafe(32)
        with self.store.transaction() as db:
            # Recheck the password version so a concurrent password change cannot create an old-password session.
            current = db.execute("SELECT password FROM admin").fetchone()
            if not current or current["password"] != admin["password"]:
                raise PermissionError("Password changed; sign in again")
            if self.hasher.check_needs_rehash(admin["password"]):
                db.execute("UPDATE admin SET password=?", (self.hasher.hash(credentials.password.get_secret_value()),))
            db.execute("DELETE FROM attempts WHERE peer=?", (peer,))
            db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            db.execute("INSERT INTO sessions VALUES(?,?,?)",
                       (self.digest(token), secrets.token_urlsafe(32), now + SESSION_SECONDS))
        return token

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token: str) -> dict | None:
        if not token or len(token) > 128:
            return None
        with self.store.transaction() as db:
            row = db.execute("SELECT csrf,expires,username FROM sessions CROSS JOIN admin "
                             "WHERE token=? AND expires>?", (self.digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token: str):
        with self.store.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (self.digest(token),))

    def change_password(self, token: str, change: PasswordChange):
        if not self.session(token):
            raise PermissionError("Sign in again")
        with self.store.transaction(immediate=True) as db:
            row = db.execute("SELECT password FROM admin").fetchone()
            try:
                self.hasher.verify(row["password"], change.current_password.get_secret_value())
            except VerificationError as exc:
                raise PermissionError("Current password is incorrect") from exc
            db.execute("UPDATE admin SET password=?", (self.hasher.hash(change.new_password.get_secret_value()),))
            db.execute("DELETE FROM sessions")
