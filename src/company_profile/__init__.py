"""Профиль компании и группы компаний и их текстовое описание для агента.

    from src.company_profile import DataStore, build_profile, build_group_profile, describe_full, describe_group

    store = DataStore()                       # читает data/ (или $DATA_DIR)
    profile = build_profile(store, "U_9250")
    print(describe_full(profile))
    print(describe_group(build_group_profile(store, profile.group_id)))
"""

from .data import DataStore
from .describe import describe_full, describe_group, describe_short
from .group import GroupProfile, build_group_profile
from .profile import CompanyProfile, build_profile

__all__ = [
    "CompanyProfile",
    "DataStore",
    "GroupProfile",
    "build_group_profile",
    "build_profile",
    "describe_full",
    "describe_group",
    "describe_short",
]
