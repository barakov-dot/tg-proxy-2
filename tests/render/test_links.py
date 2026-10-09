from tgpanel.render.links import https_link, tg_link


def test_links() -> None:
    sec = "dd" + "a" * 32
    assert https_link("proxy.example.com", sec) == (
        f"https://t.me/webproxy?server=proxy.example.com&secret={sec}"
    )
    assert tg_link("proxy.example.com", sec) == (
        f"tg://webproxy?server=proxy.example.com&secret={sec}"
    )


def test_quoting() -> None:
    assert https_link("a b&c", "x/y+z=") == (
        "https://t.me/webproxy?server=a%20b%26c&secret=x%2Fy%2Bz%3D"
    )
