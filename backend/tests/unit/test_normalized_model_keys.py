from backend.models.normalized_keys import (
    user_member_search_key_default,
    workspace_directory_title_key_default,
)
from backend.models.user import User
from backend.models.workspace import Workspace


class _DefaultContext:
    def __init__(self, parameters: dict[str, str]) -> None:
        self._parameters = parameters

    def get_current_parameters(self) -> dict[str, str]:
        return self._parameters


def test_normalized_keys_cover_constructor_order_and_later_assignments():
    user = User(
        email="Straße@example.com",
        display_name="  Ａlice\tﬁ  ",
        password_hash="hash",
    )
    assert user.member_search_key == "alice fi strasse@example.com"

    user.email = "SECOND@example.com"
    assert user.member_search_key == "alice fi second@example.com"
    user.display_name = "  Bob\n\nSmith  "
    assert user.member_search_key == "bob smith second@example.com"

    workspace = Workspace(slug="unicode", title="Straße\tﬁ", name="Unicode")
    assert workspace.directory_title_key == "strasse fi"
    workspace.title = "  Ａlpha\n\nTeam  "
    assert workspace.directory_title_key == "alpha team"


def test_core_insert_defaults_derive_keys_from_current_parameters():
    assert workspace_directory_title_key_default(
        _DefaultContext({"title": "  Ｔeam\tStraße  "})
    ) == "team strasse"
    assert user_member_search_key_default(
        _DefaultContext(
            {
                "display_name": "  Ａlice\tSmith ",
                "email": "Straße@example.com",
            }
        )
    ) == "alice smith strasse@example.com"
