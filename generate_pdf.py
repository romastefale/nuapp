import pathlib
from playwright.sync_api import sync_playwright

SRC = pathlib.Path("infografico.html").resolve()
OUT = "Infografico_Moderacao_Bot.pdf"
WIDTH = 1240

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": WIDTH, "height": 1200})
    page.goto(SRC.as_uri(), wait_until="networkidle")
    page.emulate_media(media="screen")
    height = page.evaluate("Math.ceil(document.body.scrollHeight)")
    page.pdf(
        path=OUT,
        width=f"{WIDTH}px",
        height=f"{height}px",
        print_background=True,
        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
    )
    browser.close()
    print(f"done {OUT} {WIDTH}x{height}")
