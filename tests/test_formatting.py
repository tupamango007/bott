from telegram import MessageEntity

from formatting import (
    collect_urls,
    contains_blocked_domain,
    entities_to_markdown,
    find_ignored_word,
    split_message,
    strip_links,
)


def ent(type_, offset, length, **kw):
    return MessageEntity(type=type_, offset=offset, length=length, **kw)


def test_strip_links_keeps_newlines_and_filenames():
    text = "Привет\nсмотри https://x.com/a и www.y.ru\nфайл main.py и photo.jpg, Node.js"
    assert strip_links(text) == "Привет\nсмотри и\nфайл main.py и photo.jpg, Node.js"


def test_strip_links_bare_domains_and_tme():
    assert strip_links("заходи на spam-link.org/promo или t.me/chan") == "заходи на или"
    assert strip_links("сайт.рф") == ""


def test_strip_links_keeps_emails():
    assert strip_links("пиши на a@b.com") == "пиши на a@b.com"


def test_bold_italic_underline():
    text = "жирный курсив подчёркнутый"
    ents = [ent("bold", 0, 6), ent("italic", 7, 6), ent("underline", 14, 12)]
    assert entities_to_markdown(text, ents) == "**жирный** *курсив* __подчёркнутый__"


def test_offsets_are_utf16_with_emoji():
    text = "😀😀 bold"
    # каждый эмодзи — 2 единицы UTF-16, поэтому слово начинается с 5
    assert entities_to_markdown(text, [ent("bold", 5, 4)]) == "😀😀 **bold**"


def test_nested_entities():
    text = "abc def"
    ents = [ent("bold", 0, 7), ent("italic", 4, 3)]
    assert entities_to_markdown(text, ents) == "**abc *def***"


def test_text_link_removed_cleanly():
    text = "нажми сюда"
    ents = [ent("text_link", 6, 4, url="https://evil.com")]
    assert entities_to_markdown(text, ents) == "нажми сюда"
    assert entities_to_markdown(text, ents, remove_links=False) == "нажми [сюда](https://evil.com)"


def test_url_entity_removed():
    text = "см. example.com тут"
    assert entities_to_markdown(text, [ent("url", 4, 11)]) == "см. тут"


def test_code_not_escaped():
    text = "run a_b*c"
    assert entities_to_markdown(text, [ent("code", 4, 5)]) == "run `a_b*c`"


def test_pre_with_language():
    text = "print(1)"
    assert entities_to_markdown(text, [ent("pre", 0, 8, language="python")]) == "```python\nprint(1)\n```"


def test_plain_text_is_escaped():
    assert entities_to_markdown("a*b_c", []) == "a\\*b\\_c"


def test_blockquote():
    text = "до\nстрока1\nстрока2"
    assert entities_to_markdown(text, [ent("blockquote", 3, 15)]) == "до\n> строка1\n> строка2"


def test_spoiler_and_strike():
    text = "секрет зачёркнуто"
    ents = [ent("spoiler", 0, 6), ent("strikethrough", 7, 10)]
    assert entities_to_markdown(text, ents) == "||секрет|| ~~зачёркнуто~~"


def test_blocked_domain_boundaries():
    domains = ["example.com"]
    assert contains_blocked_domain("go to https://sub.example.com/x", domains)
    assert contains_blocked_domain("EXAMPLE.COM", domains)
    assert not contains_blocked_domain("notexample.com", domains)


def test_blocked_domain_in_hidden_link():
    entities = [ent("text_link", 0, 4, url="https://example.com/x")]
    assert contains_blocked_domain(collect_urls("тут", entities), ["example.com"])


def test_ignored_words():
    assert find_ignored_word("Это РЕКЛАМА", ["реклама"]) == "реклама"
    assert find_ignored_word("ёлка", ["елка"]) == "елка"
    assert find_ignored_word("нормально", ["спам"]) is None


def test_split_message():
    text = "\n".join(["x" * 900] * 5)
    parts = split_message(text, 2000)
    assert all(len(p) <= 2000 for p in parts)
    assert "".join(parts).replace("\n", "") == "x" * 4500
    assert split_message("short") == ["short"]
    assert split_message("") == []
    assert all(len(p) <= 2000 for p in split_message("y" * 5000))
