from social_image_mcp.intent import parse_intent, requested_media_limit, visual_contrast_prompts, visual_prompt
from social_image_mcp.object_semantics import ObjectObservation, decide_observation, parse_content_spec


def test_explicit_media_limit_is_honored():
    assert requested_media_limit("抖音作品 最多三张", 20) == 3
    assert requested_media_limit("只要2张图片", 20) == 2
    assert requested_media_limit("完整图集", 20) == 100


def test_keyword_intent_extracts_orientation_and_negative_terms():
    intent = parse_intent("极简风咖啡店室内，横图，不要水印")
    assert "咖啡店" in intent.tokens
    assert intent.orientation == "landscape"
    assert "水印" in intent.negative_tokens
    assert intent.quality_preference == "high"
    assert intent.exclude_watermark is True
    assert "coffee" in visual_prompt(intent)


def test_url_intent_extracts_platform_id():
    intent = parse_intent("https://x.com/example/status/123456789")
    assert intent.identifier == "123456789"
    assert intent.identifier_platform == "x"


def test_bare_long_id_is_treated_as_identifier():
    intent = parse_intent("123456789")
    assert intent.identifier == "123456789"
    assert intent.identifier_platform is None


def test_platform_prefix_preserves_case_sensitive_content_id():
    instagram = parse_intent("ins:ABC_123-xY")
    weibo = parse_intent("WEIBO:Mx123")

    assert instagram.identifier_platform == "instagram"
    assert instagram.identifier == "ABC_123-xY"
    assert weibo.identifier_platform == "weibo"
    assert weibo.identifier == "Mx123"


def test_creator_prefix_is_distinguished_from_post_id():
    intent = parse_intent("douyin-user:Gracebb0722")
    assert intent.identifier == "Gracebb0722"
    assert intent.identifier_platform == "douyin"
    assert intent.identifier_scope == "creator"
    assert intent.is_keyword is False


def test_at_handle_is_treated_as_x_creator():
    intent = parse_intent("@jwj180")
    assert intent.identifier == "jwj180"
    assert intent.identifier_platform == "x"
    assert intent.identifier_scope == "creator"


def test_x_from_filter_is_treated_as_creator_when_it_is_the_whole_query():
    intent = parse_intent("from:jwj180")
    assert intent.identifier == "jwj180"
    assert intent.identifier_platform == "x"
    assert intent.identifier_scope == "creator"


def test_creator_name_prefix_routes_to_creator_lookup():
    intent = parse_intent("douyin-name:放学小野猪")
    assert intent.identifier == "放学小野猪"
    assert intent.identifier_platform == "douyin"
    assert intent.identifier_scope == "creator_name"


def test_creator_profile_urls_are_creator_targets():
    douyin = parse_intent("https://www.douyin.com/user/MS4wLjABAAAAabc")
    weibo = parse_intent("https://weibo.com/u/5756404150")
    assert douyin.identifier_scope == "creator"
    assert douyin.identifier == "MS4wLjABAAAAabc"
    assert weibo.identifier_scope == "creator"
    assert weibo.identifier == "5756404150"


def test_douyin_homepage_short_link_stays_on_douyin_platform():
    intent = parse_intent("https://v.douyin.com/abc123/")
    assert intent.identifier_platform == "douyin"
    assert intent.identifier_platform != "other"


def test_douyin_share_user_profile_url_is_creator_target():
    intent = parse_intent("https://www.douyin.com/share/user/MS4wLjABAAAAabc")
    assert intent.identifier_platform == "douyin"
    assert intent.identifier_scope == "creator"
    assert intent.identifier == "MS4wLjABAAAAabc"


def test_interior_intent_builds_closeup_and_outdoor_distractors():
    positive, negative = visual_contrast_prompts(parse_intent("咖啡店室内"))

    assert any("interior" in prompt.lower() for prompt in positive)
    assert any("close-up" in prompt.lower() for prompt in negative)
    assert any("outdoor" in prompt.lower() for prompt in negative)


def test_content_spec_splits_objects_regions_and_exclusions():
    spec = parse_content_spec("只要人物穿搭和日常生活，排除风景、建筑、纯场景图")
    assert "person" in spec.include
    assert "clothing" in spec.include
    assert "person" in spec.required
    assert {"landscape", "architecture", "scene"}.issubset(set(spec.exclude))
    assert spec.region == "full_image"


def test_content_spec_supports_body_region():
    assert parse_content_spec("只要腿部穿搭").region == "lower_body"
    assert parse_content_spec("只要鞋子").region == "feet"
    assert parse_content_spec("只要脸部").region == "face"


def test_object_decision_rejects_missing_required_and_excluded_objects():
    spec = parse_content_spec("只要人物穿搭，排除风景")
    assert decide_observation(spec, ObjectObservation(("landscape",), {"landscape": 0.8}))[0] is False
    accepted, reason = decide_observation(spec, ObjectObservation(("person", "clothing"), {"person": 0.5, "clothing": 0.3}))
    assert accepted is True and reason is None
    assert decide_observation(spec, ObjectObservation(("person",), {"person": 0.5}))[0] is False


def test_object_decision_uses_area_to_ignore_tiny_background_objects():
    spec = parse_content_spec("只要人物穿搭，排除风景")
    tiny_person = ObjectObservation(("person", "clothing", "landscape"), {"person": 0.01, "clothing": 0.2, "landscape": 0.7})
    accepted, reason = decide_observation(spec, tiny_person)
    assert accepted is False and reason.startswith("required_object_missing")

    tiny_landscape = ObjectObservation(("person", "clothing", "landscape"), {"person": 0.4, "clothing": 0.25, "landscape": 0.02})
    assert decide_observation(spec, tiny_landscape)[0] is True
