import pytest

from msu_hub_bot.providers.vk.posts import VkPost


@pytest.mark.parametrize("with_header", [True, False])
@pytest.mark.parametrize("repost", [True, False])
def test_synthetic_vk_response_rendering(with_header, repost):
    post = {"id": 1, "owner_id": -10, "date": 1_700_000_000, "text": "Текст <unsafe> & [id20|пример]", "attachments": []}
    if repost:
        post["copy_history"] = [{"id": 2, "owner_id": 20, "date": 1_700_000_000, "text": "Synthetic repost", "attachments": []}]
    response = {
        "items": [post],
        "groups": [{"id": 10, "name": "Example group", "screen_name": "example"}],
        "profiles": [{"id": 20, "first_name": "Example", "last_name": "User", "screen_name": "example_user"}],
    }
    parsed = VkPost.from_response(response)[0]
    rendered = parsed.render(with_header=with_header)
    assert "<unsafe>" not in rendered
    assert "Synthetic repost" in rendered if repost else "Текст" in rendered
    assert parsed.owner_id == -10
