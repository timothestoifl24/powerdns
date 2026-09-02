"""Group discovery and the role it produces.

These run against ldap3's in-process mock server rather than a hand-written
fake, so the code under test does real searches and reads real ldap3 Entry
objects. Every case here is a way a correctly configured administrator group
silently failed to grant the admin role.
"""

from __future__ import annotations

import pytest
from ldap3 import MOCK_SYNC, OFFLINE_AD_2012_R2, OFFLINE_SLAPD_2_4, Connection, Server

from app.auth.ldap_auth import (
    AD_NESTED_GROUP_FILTER,
    DEFAULT_GROUP_FILTER,
    _groups_from_entry,
    attribute_value,
)
from app.config import ROLE_ADMIN, ROLE_USER, GroupRoleMap, LdapConfig

BASE = "dc=example,dc=com"
USER_DN = "cn=jdoe,ou=People,dc=example,dc=com"
GROUP_DN = "cn=admin-ldap,ou=Groups,dc=example,dc=com"

ADMIN_MAP = GroupRoleMap(admin_groups=("admin-ldap",), default_role=ROLE_USER)


def ldap_config(**overrides) -> LdapConfig:
    settings = {
        "enabled": True,
        "uri": "ldap://directory.test",
        "base_dn": BASE,
        "roles": ADMIN_MAP,
    }
    settings.update(overrides)
    return LdapConfig(**settings)


@pytest.fixture
def directory():
    """An Active Directory-shaped mock that publishes memberOf."""
    server = Server("ldap://directory.test", get_info=OFFLINE_AD_2012_R2)
    connection = Connection(
        server, user="cn=svc,dc=example,dc=com", password="svc-pw", client_strategy=MOCK_SYNC
    )
    connection.strategy.add_entry(
        "cn=svc,dc=example,dc=com", {"objectClass": ["person"], "userPassword": "svc-pw"}
    )
    connection.strategy.add_entry(
        USER_DN,
        {
            "objectClass": ["person", "user"],
            "sAMAccountName": "jdoe",
            "cn": "jdoe",
            "mail": "jdoe@example.com",
            "memberOf": [GROUP_DN],
            "userPassword": "hunter2hunter2",
        },
    )
    connection.bind()
    return connection


def entry_for(connection, group_attribute: str = "memberOf") -> dict:
    """Search for the user the way `authenticate` does and return its attributes."""
    attributes = [
        attribute for attribute in {"sAMAccountName", "mail", "cn", group_attribute} if attribute
    ]
    connection.search(
        search_base=BASE,
        search_filter="(&(objectClass=person)(sAMAccountName=jdoe))",
        attributes=attributes,
    )
    found = connection.entries[0]
    return {**found.entry_attributes_as_dict, "dn": found.entry_dn, "username": "jdoe"}


class TestGroupAttributeLookup:
    def test_the_configured_attribute_is_read(self, directory):
        config = ldap_config(group_attribute="memberOf")
        groups = _groups_from_entry(config, directory, entry_for(directory))
        assert groups == [GROUP_DN]
        assert config.roles.resolve(groups) == ROLE_ADMIN

    def test_attribute_name_case_does_not_matter(self, directory):
        """LDAP attribute names are case-insensitive; a plain dict lookup is not.

        Configuring 'memberof' against a server that answers 'memberOf' used to
        yield no groups at all, and the user silently got the default role.
        """
        config = ldap_config(group_attribute="memberof")
        groups = _groups_from_entry(config, directory, entry_for(directory, "memberof"))
        assert config.roles.resolve(groups) == ROLE_ADMIN

    def test_an_unknown_attribute_falls_back_to_the_usual_ones(self, directory):
        config = ldap_config(group_attribute="notAnAttribute")
        groups = _groups_from_entry(config, directory, entry_for(directory))
        assert config.roles.resolve(groups) == ROLE_ADMIN

    def test_no_groups_anywhere_yields_the_default_role(self, directory):
        config = ldap_config()
        entry = {"sAMAccountName": ["jdoe"], "dn": USER_DN, "username": "jdoe"}
        groups = _groups_from_entry(config, directory, entry)
        assert groups == []
        assert config.roles.resolve(groups) == ROLE_USER


class TestAttributeValue:
    @pytest.mark.parametrize("name", ["memberOf", "memberof", "MEMBEROF"])
    def test_case_insensitive(self, name):
        assert attribute_value({"memberOf": ["a", "b"]}, name) == ["a", "b"]

    def test_a_single_value_is_wrapped(self):
        assert attribute_value({"cn": "solo"}, "cn") == ["solo"]

    def test_blank_and_missing_yield_nothing(self):
        assert attribute_value({"cn": ["", "  "]}, "cn") == []
        assert attribute_value({}, "cn") == []
        assert attribute_value({"cn": ["x"]}, "") == []


class TestGroupSearch:
    """Directories that record membership on the group, not on the user."""

    @pytest.fixture
    def posix_directory(self):
        server = Server("ldap://openldap.test", get_info=OFFLINE_SLAPD_2_4)
        connection = Connection(
            server, user="cn=svc,dc=example,dc=com", password="pw", client_strategy=MOCK_SYNC
        )
        connection.strategy.add_entry(
            "cn=svc,dc=example,dc=com", {"objectClass": ["person"], "userPassword": "pw"}
        )
        connection.strategy.add_entry(
            "uid=jdoe,ou=People,dc=example,dc=com",
            {"objectClass": ["inetOrgPerson"], "uid": "jdoe", "cn": "J Doe"},
        )
        connection.strategy.add_entry(
            GROUP_DN,
            {
                "objectClass": ["groupOfNames"],
                "cn": "admin-ldap",
                "member": ["uid=jdoe,ou=People,dc=example,dc=com"],
            },
        )
        connection.bind()
        return connection

    @staticmethod
    def _entry() -> dict:
        return {
            "uid": ["jdoe"],
            "dn": "uid=jdoe,ou=People,dc=example,dc=com",
            "username": "jdoe",
        }

    def test_a_search_base_alone_is_enough(self, posix_directory):
        """A base with no filter used to search for nothing, which is
        indistinguishable from the user being in no groups."""
        config = ldap_config(group_search_base="ou=Groups,dc=example,dc=com", group_filter="")
        groups = _groups_from_entry(config, posix_directory, self._entry())
        assert "admin-ldap" in groups
        assert config.roles.resolve(groups) == ROLE_ADMIN

    def test_both_the_name_and_the_dn_are_reported(self, posix_directory):
        """The mapping may be configured with either form."""
        config = ldap_config(group_search_base="ou=Groups,dc=example,dc=com")
        groups = _groups_from_entry(config, posix_directory, self._entry())
        assert "admin-ldap" in groups
        assert GROUP_DN in groups
        by_dn = GroupRoleMap(admin_groups=(GROUP_DN,), default_role=ROLE_USER)
        assert by_dn.resolve(groups) == ROLE_ADMIN

    def test_an_explicit_filter_is_still_honoured(self, posix_directory):
        config = ldap_config(
            group_search_base="ou=Groups,dc=example,dc=com",
            group_filter="(member={dn})",
        )
        groups = _groups_from_entry(config, posix_directory, self._entry())
        assert config.roles.resolve(groups) == ROLE_ADMIN


class TestFilterTemplates:
    def test_the_default_filter_covers_the_three_membership_schemas(self):
        for attribute in ("member=", "uniqueMember=", "memberUid="):
            assert attribute in DEFAULT_GROUP_FILTER

    def test_the_nested_filter_uses_the_in_chain_matching_rule(self):
        # LDAP_MATCHING_RULE_IN_CHAIN: the only way to see nested AD groups.
        assert "1.2.840.113556.1.4.1941" in AD_NESTED_GROUP_FILTER
