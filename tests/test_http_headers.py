from tunebox.http_headers import single


def test_single_answers_one_line_only() -> None:
    pairs = [("Content-Length", "5"), ("Origin", "a"), ("origin", "b")]
    assert single(pairs, "content-length") == "5"
    assert single(pairs, "Origin") is None
    assert single(pairs, "X-Missing") is None
