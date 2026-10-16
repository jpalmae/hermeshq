import unittest

from hermeshq.routers.m365 import _SCOPE_TO_INTEGRATION
from hermeshq.services.m365_oauth import AVAILABLE_SCOPES, _build_scopes

AZURE_FINAL_LIST = [
    "User.Read",
    "Mail.Read",
    "Mail.Send",
    "Mail.Send.Shared",
    "Calendars.ReadWrite",
    "Contacts.ReadWrite",
    "Files.ReadWrite.All",
    "Sites.ReadWrite.All",
    "Group.Read.All",
    "GroupMember.Read.All",
    "Chat.Create",
    "Chat.ReadWrite",
]


class TestM365ScopeMapping(unittest.TestCase):
    def test_every_scope_of_the_azure_list_is_in_the_catalog(self):
        missing = [scope for scope in AZURE_FINAL_LIST if scope not in AVAILABLE_SCOPES]
        self.assertEqual(missing, [])

    def test_every_scope_of_the_azure_list_is_requested_at_login(self):
        requested = _build_scopes(AZURE_FINAL_LIST)
        self.assertEqual(sorted(requested), sorted(set(AZURE_FINAL_LIST)))

    def test_every_mapped_scope_exists_in_the_catalog(self):
        self.assertEqual([s for s in _SCOPE_TO_INTEGRATION if s not in AVAILABLE_SCOPES], [])

    def test_each_azure_scope_activates_an_integration_or_is_identity_only(self):
        unmapped = [s for s in AZURE_FINAL_LIST if s not in _SCOPE_TO_INTEGRATION and s != "User.Read"]
        self.assertEqual(unmapped, [])

    def test_new_scopes_map_to_the_right_integrations(self):
        expected = {
            "Mail.Send.Shared": "ms365-mail",
            "Contacts.ReadWrite": "ms365-mail",
            "Files.ReadWrite.All": "sharepoint",
            "Sites.ReadWrite.All": "sharepoint",
            "Group.Read.All": "ms365-teams",
            "GroupMember.Read.All": "ms365-teams",
            "Chat.Create": "ms365-teams",
        }
        for scope, integration in expected.items():
            self.assertEqual(_SCOPE_TO_INTEGRATION[scope], integration)
