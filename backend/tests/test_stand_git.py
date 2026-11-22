"""HTTPS Git URL validation for repository registration."""

import pytest

from portal import stand_git


@pytest.mark.parametrize("url", [
    "http://git/repo", "ssh://git/repo", "https://user:secret@git/repo",
    "https://git/a/../repo", "https://git/a/%2e%2e/repo", "https://git/a%2frepo",
    "https://git/repo?token=secret", "https://git/repo#fragment", "https://git/repo?",
    "https://git/repo\n", "https://git:65536/repo", "https://git:/repo",
    "https://git/a//repo", "https://git/", "https://git/.git",
])
def test_url_rejects_unsafe_input(url):
    with pytest.raises(stand_git.GitSourceError):
        stand_git.parse_git_url(url)


def test_url_suggestions_for_nested_repository():
    url, title, key = stand_git.parse_git_url("https://GIT.internal:8443/group/team/123_Test.git/")
    assert (url, title, key) == ("https://git.internal:8443/group/team/123_Test.git", "123_Test", "repo-123_test")
