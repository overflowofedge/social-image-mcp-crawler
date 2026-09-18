from social_image_mcp.intent import parse_intent
from social_image_mcp.models import ImageCandidate, Platform
from social_image_mcp.ranking import rank_candidates


def test_ranking_prefers_matching_landscape_image_and_deduplicates():
    intent = parse_intent("咖啡店 横图")
    first = ImageCandidate(id="1", platform=Platform.XHS, image_url="https://cdn/a.jpg", title="极简咖啡店室内", width=1600, height=900)
    duplicate = first.model_copy(update={"id": "2", "image_url": "https://cdn/a.jpg?size=large"})
    portrait = ImageCandidate(id="3", platform=Platform.XHS, image_url="https://cdn/b.jpg", title="咖啡店", width=600, height=1200)
    ranked = rank_candidates([portrait, duplicate, first], intent, 10)
    assert len(ranked) == 2
    assert ranked[0].id == "1"


def test_ranking_excludes_watermarked_candidate_when_requested():
    intent = parse_intent("咖啡店 无水印")
    clean = ImageCandidate(id="1", platform=Platform.XHS, image_url="https://cdn/clean.jpg", title="咖啡店室内", width=1200, height=800)
    marked = ImageCandidate(id="2", platform=Platform.XHS, image_url="https://cdn/marked.jpg", title="咖啡店室内 水印", width=1600, height=1000)
    ranked = rank_candidates([marked, clean], intent, 10)
    assert [item.id for item in ranked] == ["1"]


def test_ranking_gives_exact_score_to_gallery_post_id():
    item = ImageCandidate(
        id="42:1",
        platform=Platform.DOUYIN,
        image_url="https://img.test/a.jpg",
        source_payload={"record": {"post_id": "42"}},
    )
    ranked = rank_candidates([item], parse_intent("douyin:42"), 1)
    assert ranked[0].score >= 3.0


def test_keyword_ranking_discards_candidates_without_any_term_match():
    intent = parse_intent("丝袜 御姐")
    unrelated = ImageCandidate(id="u", platform=Platform.WEIBO, image_url="https://cdn/u.jpg", title="风景摄影", width=4000, height=3000)
    relevant = ImageCandidate(id="r", platform=Platform.WEIBO, image_url="https://cdn/r.jpg", title="御姐丝袜穿搭", width=800, height=1200)
    ranked = rank_candidates([unrelated, relevant], intent, 10, require_match=True)
    assert [item.id for item in ranked] == ["r"]
