import json

import pytest

from nexus_lite import core
from nexus_lite import provider_errors as pe


@pytest.mark.parametrize("text,cls", [
    ("Error: Rate limit exceeded", "rate_limit"),
    ("429 Too Many Requests", "rate_limit"),
    ("You've hit your weekly limit", "quota"),
    ("RESOURCE_EXHAUSTED: quota reached", "quota"),
    ("FreeUsageLimitError", "quota"),
    ("Please run /login", "auth"),
    ("Invalid API key", "auth"),
    ("401 Unauthorized", "auth"),
])
def test_text_classes(text, cls):
    assert pe.classify_provider_error(1, text) == cls
    assert pe.is_quota_or_auth_failure(1, text)


@pytest.mark.parametrize("body,cls", [
    ({"error": {"type": "rate_limit_error", "message": "x"}}, "rate_limit"),
    ({"error": {"code": 429}}, "rate_limit"),
    ({"api_error_status": 429}, "rate_limit"),
    ({"error": {"status": "RESOURCE_EXHAUSTED"}}, "quota"),
    ({"error": {"code": 401}}, "auth"),
    ({"error": {"status": 403}}, "auth"),
    ({"error": {"type": "authentication_error"}}, "auth"),
])
def test_json_body(body, cls):
    assert pe.classify_provider_error(1, json.dumps(body)) == cls
    assert pe.classify_provider_error(1, "log\n" + json.dumps(body)) == cls


def test_rc0_never_failure():
    assert pe.classify_provider_error(0, "Rate limit exceeded") is None
    assert pe.classify_provider_error(0, '{"error":{"code":429}}') is None


def test_unclassified():
    assert pe.classify_provider_error(1, "segfault") is None
    assert pe.classify_provider_error(1, "") is None
    assert pe.classify_provider_error(1, '{"error":{"code":500}}') is None


def test_is_failure_legacy_semantics():
    assert pe.is_quota_or_auth_failure(1, "segfault")
    assert pe.is_quota_or_auth_failure(0, "")
    assert not pe.is_quota_or_auth_failure(0, "fine")
    assert not pe.is_quota_or_auth_failure(3, "fine")


def test_core_aliases():
    assert core._is_quota_or_auth_failure is pe.is_quota_or_auth_failure
    assert "invalid api key" in core._QUOTA_OR_AUTH_MARKERS
