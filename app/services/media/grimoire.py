import logging
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import or_

from app.extensions import db
from app.models import Invitation, User
from app.services.invites import is_invite_valid

from .client_base import EMAIL_RE, RestApiMixin, register_media_client
from .utils import (
    DateHelper,
    LibraryAccessHelper,
    StandardizedPermissions,
    create_standardized_user_details,
)

if TYPE_CHECKING:
    from app.services.media.user_details import MediaUserDetails


@register_media_client("grimoire")
class GrimoireClient(RestApiMixin):
    """Wrapper around the Grimoire REST API for TTRPG library management.

    This client integrates with Grimoire using its official REST API.
    Authentication is handled via API keys sent as an ``X-API-Key`` header.

    API Documentation: https://docs.grimoirecodex.org/api.html

    Key Endpoints Used:
        Authentication:
            - GET  /api/auth/status - Server reachability and setup status (no auth)
            - POST /api/auth/login  - Exchange username/password for JWT token

        User Management:
            - GET  /api/users       - List all users (admin)
            - POST /api/users       - Create a user (admin)
            - PATCH /api/users/:id  - Update a user (admin)
            - DELETE /api/users/:id - Delete a user (admin)

        Library:
            - GET /api/systems   - List all game systems (treated as libraries)
            - GET /api/stats     - Library counts and totals

        Server Info:
            - GET /api/auth/me   - Current user details

    Important Notes:
        - Grimoire authenticates API keys via the ``X-API-Key`` header.
        - API keys are personal and act as the user that created them; the Wizarr
          admin's key must have ``Users: Write`` (and ``Statistics: Read`` for
          stats) permission to manage users and read counts.
        - Grimoire does not support enabling/disabling users.
        - User IDs are UUIDs.
    """

    @classmethod
    def check_connection(cls, url: str, token: str) -> tuple[bool, str]:
        from app.services.servers import check_grimoire

        return check_grimoire(url, token)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("url_key", "server_url")
        kwargs.setdefault("token_key", "api_key")  # noqa: S105  # Parameter name, not actual password
        super().__init__(*args, **kwargs)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-API-Key"] = self.token
        return headers

    def libraries(self) -> dict[str, str]:
        """Get all game systems from Grimoire, treated as libraries.

        API Endpoint: GET /api/systems

        Returns:
            dict[str, str]: Mapping of system ID to system name
        """
        try:
            response = self.get("/api/systems")
            response.raise_for_status()
            systems = response.json()
            if not isinstance(systems, list):
                return {}
            return {str(system.get("id")): system.get("name", "Unknown") for system in systems}
        except Exception as e:
            logging.error(f"Failed to get Grimoire systems: {e}")
            return {}

    def scan_libraries(
        self, url: str | None = None, token: str | None = None
    ) -> dict[str, str]:
        """Scan available game systems on this Grimoire server.

        Args:
            url: Optional server URL override
            token: Optional API token override

        Returns:
            dict: System name -> system ID mapping
        """
        import requests

        try:
            if url and token:
                headers = {"X-API-Key": token, "Accept": "application/json"}
                response = requests.get(
                    f"{url.rstrip('/')}/api/systems", headers=headers, timeout=10
                )
                response.raise_for_status()
                systems = response.json()
            else:
                response = self.get("/api/systems")
                systems = response.json()

            if not isinstance(systems, list):
                return {}
            return {system.get("name", "Unknown"): str(system.get("id")) for system in systems}
        except Exception as exc:
            logging.warning("Grimoire: failed to scan systems – %s", exc)
            return {}

    def create_user(
        self,
        username: str,
        password: str,
        email: str = "",
        role: str = "player",
    ) -> str:
        """Create a new user in Grimoire and return the user ID.

        API Endpoint: POST /api/users (admin)

        Args:
            username: Username for the new user
            password: Password for the new user
            email: Email address for the new user
            role: Role to assign (admin, gm, or player)

        Returns:
            str: The new user's ID (UUID)
        """
        payload = {
            "username": username,
            "password": password,
            "email": email,
            "role": role,
        }
        response = self.post("/api/users", json=payload)
        response.raise_for_status()
        data = response.json()
        user_id = data.get("id") or data.get("user_id")
        if not user_id:
            raise ValueError("No user ID returned from Grimoire")
        return str(user_id)

    def update_user(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update a Grimoire user.

        API Endpoint: PATCH /api/users/:id (admin)
        """
        response = self.patch(f"/api/users/{user_id}", json=updates)
        return response.json()

    def enable_user(self, user_id: str) -> bool:  # noqa: ARG002
        """Enable a user account on Grimoire.

        Grimoire does not support enabling/disabling users directly.
        """
        structlog.get_logger().warning(
            "Grimoire does not support enabling users."
        )
        return False

    def disable_user(self, user_id: str) -> bool:  # noqa: ARG002
        """Disable a user account on Grimoire.

        Grimoire does not support enabling/disabling users directly.
        """
        structlog.get_logger().warning(
            "Grimoire does not support disabling users, only deletion."
        )
        return False

    def delete_user(self, user_id: str) -> None:
        """Delete a user from Grimoire.

        API Endpoint: DELETE /api/users/:id (admin)
        """
        self.delete(f"/api/users/{user_id}")
        logging.info("Deleted Grimoire user '%s'", user_id)

    def get_user(self, user_id: str) -> dict[str, Any]:
        """Get user info in legacy format for backward compatibility."""
        details = self.get_user_details(user_id)
        return {
            "id": details.user_id,
            "username": details.username,
            "email": details.email,
            "isAdmin": details.is_admin,
            "created": details.created_at.isoformat() + "Z" if details.created_at else None,
            "lastActive": details.last_active.isoformat() + "Z" if details.last_active else None,
        }

    def get_user_details(self, user_identifier: str | int) -> "MediaUserDetails":
        """Get detailed user information in standardized format."""
        user_id = str(user_identifier)
        from app.services.media.utils import (
            DateHelper,
            LibraryAccessHelper,
            create_standardized_user_details,
        )

        # List all users and find the matching one
        response = self.get("/api/users")
        users = response.json()
        if not isinstance(users, list):
            raise ValueError("Unexpected response format from Grimoire /api/users")

        raw_user = None
        for u in users:
            if str(u.get("id")) == user_id:
                raw_user = u
                break

        if not raw_user:
            raise ValueError(f"Grimoire user not found: {user_id}")

        permissions = StandardizedPermissions.for_basic_server(
            "grimoire",
            is_admin=raw_user.get("role") == "admin",
            allow_downloads=True,
        )

        library_access = LibraryAccessHelper.create_full_access()

        created_at = DateHelper.parse_iso_date(raw_user.get("created_at"))
        last_active = DateHelper.parse_iso_date(raw_user.get("last_active"))

        return create_standardized_user_details(
            user_id=str(raw_user.get("id", user_id)),
            username=raw_user.get("username", user_id),
            email=raw_user.get("email"),
            permissions=permissions,
            library_access=library_access,
            created_at=created_at,
            last_active=last_active,
            is_enabled=True,
        )

    def list_users(self) -> list[User]:
        """Sync users from Grimoire into the local DB and return the list of User records."""
        try:
            response = self.get("/api/users")
            grimoire_users = response.json()
            if not isinstance(grimoire_users, list):
                logging.warning("Unexpected response format from Grimoire /api/users – expected list")
                return []
        except Exception as exc:
            logging.error(f"Failed to list Grimoire users: {exc}")
            return []

        users_dict = {str(u.get("id")): u for u in grimoire_users if u.get("id")}
        server_id = getattr(self, "server_id", None)

        known_users = User.query.filter(User.server_id == server_id).all()
        if self._skip_prune_on_empty_remote(not users_dict, known_users):
            return known_users

        users: list[User] = []
        for dbu in known_users:
            if dbu.token in users_dict:
                users.append(dbu)
            else:
                db.session.delete(dbu)

        known_tokens = {user.token for user in users}
        for token, grimoire_user in users_dict.items():
            if token not in known_tokens:
                new = User(
                    token=token,
                    username=grimoire_user.get("username", "grimoire-user"),
                    email=grimoire_user.get("email", "empty"),
                    code="empty",
                    server_id=server_id,
                )
                db.session.add(new)
                users.append(new)

        for user in users:
            permissions = StandardizedPermissions.for_basic_server(
                "grimoire",
                is_admin=False,
                allow_downloads=True,
            )
            user.allow_downloads = permissions.allow_downloads
            user.allow_live_tv = permissions.allow_live_tv
            user.is_admin = permissions.is_admin

        try:
            db.session.commit()
        except Exception as e:
            logging.error("Grimoire: failed to update user metadata – %s", e)
            db.session.rollback()
            return []

        return users

    def _password_for_db(self, password: str) -> str:
        """Return the password value to store in the local DB."""
        return password

    def _do_join(
        self,
        username: str,
        password: str,
        confirm: str,
        email: str,
        code: str,
    ) -> tuple[bool, str]:
        """Handle public sign-up via invite for Grimoire servers."""
        if email and not EMAIL_RE.fullmatch(email):
            return False, "Invalid e-mail address."
        if not 8 <= len(password) <= 128:
            return False, "Password must be 8–128 characters."
        if password != confirm:
            return False, "Passwords do not match."

        ok, msg = is_invite_valid(code)
        if not ok:
            return False, msg

        existing = User.query.filter(
            or_(User.username == username, User.email == email),
            User.server_id == getattr(self, "server_id", None),
        ).first()
        if existing:
            return False, "User or e-mail already exists."

        try:
            inv = Invitation.query.filter_by(code=code).first()
            current_server_id = getattr(self, "server_id", None)

            # Grimoire roles: admin, gm, player. Invited users default to player.
            role = "player"
            if inv and getattr(inv, "allow_downloads", False):
                role = "gm"

            user_id = self.create_user(username, password, email, role=role)

            from app.services.expiry import calculate_user_expiry

            expires = calculate_user_expiry(inv, current_server_id) if inv else None

            self._create_user_with_identity_linking(
                {
                    "username": username,
                    "email": email or "empty",
                    "token": user_id,
                    "code": code,
                    "expires": expires,
                    "server_id": current_server_id,
                }
            )
            db.session.commit()

            return True, ""

        except Exception:
            logging.error("Grimoire join error", exc_info=True)
            db.session.rollback()
            return False, "An unexpected error occurred."

    def now_playing(self) -> list[dict[str, Any]]:
        """Return a list of currently playing sessions from Grimoire.

        Grimoire is a TTRPG library manager and does not provide session
        tracking or "now playing" functionality. This method always returns
        an empty list.
        """
        return []

    def statistics(self) -> dict[str, Any]:
        """Return essential Grimoire server statistics for the dashboard.

        API Endpoint: GET /api/stats (Statistics: Read via API key)

        Returns:
            dict: Server statistics
        """
        try:
            stats = {
                "library_stats": {},
                "user_stats": {},
                "server_stats": {},
                "content_stats": {},
            }

            # User statistics
            try:
                users_response = self.get("/api/users")
                users = users_response.json() if users_response.text.strip() else []
                user_count = len(users) if isinstance(users, list) else 0
                stats["user_stats"] = {
                    "total_users": user_count,
                    "active_sessions": 0,
                }
            except Exception as e:
                logging.error(f"Failed to get Grimoire user stats: {e}")
                stats["user_stats"] = {"total_users": 0, "active_sessions": 0}

            # Server statistics - no version endpoint available, use stats
            try:
                stats_response = self.get("/api/stats")
                stats_response.raise_for_status()
                data = stats_response.json()
                stats["library_stats"] = {
                    "game_systems": data.get("game_systems", 0),
                    "books": data.get("books", 0),
                    "maps": data.get("maps", 0),
                    "tokens": data.get("tokens", 0),
                    "audio": data.get("audio", 0),
                    "models": data.get("models", 0),
                }
                stats["content_stats"] = {
                    "indexed_books": data.get("indexed_books", 0),
                    "total_pages": data.get("total_pages", 0),
                    "library_size_mb": data.get("library_size_mb", 0),
                }
            except Exception as e:
                logging.error(f"Failed to get Grimoire stats: {e}")

            stats["server_stats"] = {
                "version": "Unknown",
                "transcoding_sessions": 0,
            }

            return stats

        except Exception as e:
            logging.error(f"Failed to get Grimoire statistics: {e}")
            return {
                "library_stats": {},
                "user_stats": {},
                "server_stats": {},
                "content_stats": {},
                "error": str(e),
            }

    def get_user_count(self) -> int:
        """Get lightweight user count from database without triggering sync."""
        try:
            from app.models import MediaServer, User

            if hasattr(self, "server_id") and self.server_id:
                count = User.query.filter_by(server_id=self.server_id).count()
            else:
                servers = MediaServer.query.filter_by(server_type="grimoire").all()
                if servers:
                    server_ids = [s.id for s in servers]
                    count = User.query.filter(User.server_id.in_(server_ids)).count()
                else:
                    count = 0
            return count
        except Exception as e:
            logging.error(f"Failed to get Grimoire user count from database: {e}")
            return 0

    def get_server_info(self) -> dict:
        """Get lightweight server information without triggering user sync."""
        return {
            "version": "Unknown",
            "transcoding_sessions": 0,
            "active_sessions": 0,
        }

    def get_readonly_statistics(self) -> dict:
        """Get lightweight statistics without triggering user synchronization."""
        try:
            user_count = self.get_user_count()
            server_info = self.get_server_info()

            return {
                "user_stats": {
                    "total_users": user_count,
                    "active_sessions": server_info.get("active_sessions", 0),
                },
                "server_stats": {
                    "version": server_info.get("version", "Unknown"),
                    "transcoding_sessions": server_info.get("transcoding_sessions", 0),
                },
                "library_stats": {},
                "content_stats": {},
            }
        except Exception as e:
            logging.error(f"Failed to get Grimoire readonly statistics: {e}")
            return {
                "user_stats": {"total_users": 0, "active_sessions": 0},
                "server_stats": {"version": "Unknown", "transcoding_sessions": 0},
                "library_stats": {},
                "content_stats": {},
                "error": str(e),
            }

    def get_recent_items(
        self,
        library_id: str | None = None,  # noqa: ARG002
        limit: int = 6,
        library_names: Any = None,  # noqa: ARG002
    ) -> list[dict[str, str]]:
        """Get recently added books from Grimoire for the wizard widget.

        API Endpoint: GET /api/books?sort=added_at&limit=N
        """
        if not self.url:
            return []

        try:
            response = self.get(f"/api/books?sort=added_at&limit={limit}")
            books = response.json()
            if not isinstance(books, list):
                return []

            items = []
            for book in books[:limit]:
                book_id = book.get("id")
                title = book.get("title", "Unknown")
                thumb_url = None
                if book_id:
                    thumb_url = f"{self.url.rstrip('/')}/api/books/{book_id}/thumbnail"
                    thumb_url = self.generate_image_proxy_url(thumb_url)

                items.append(
                    {
                        "title": title,
                        "thumb": thumb_url,
                    }
                )

            return items

        except Exception as e:
            logging.warning(f"Failed to get recent items from Grimoire: {e}")
            return []
