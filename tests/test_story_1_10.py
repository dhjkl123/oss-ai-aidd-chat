"""Story 1.10 source gate: the things a running browser cannot show you.

Four claims live here, because everything else about this story is observable at
runtime and is asserted in `test_story_1_10_browser.py` instead:

* the Canonical Component IDs are one join key shared by markup and stylesheet -- a
  browser cannot tell you that a key nobody selects on has quietly gone missing;
* every colour literal is in the single `:root` block, and every token in it is
  used -- an unused token is a value nothing verifies;
* every font-size and every spacing value reads a token, so DESIGN.md's type ramp
  and 4px scale hold everywhere rather than only where someone looked;
* the palette meets the ratios the frozen matrix names, computed from the tokens.

All three source files are read from the working tree, so a non-editable install
cannot make this compare shipped markup against working-tree tests.
"""

from pathlib import Path
import re

from test_story_1_10_browser import CANONICAL_IDS


ROOT = Path(__file__).parents[1]
WEB = ROOT / "src" / "aidd_chat" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "styles.css").read_text(encoding="utf-8")
SCRIPT = (WEB / "app.js").read_text(encoding="utf-8")

ROOT_BLOCK = re.search(r":root\s*\{(.*?)\n\}", CSS, re.S)
# Rules only: prose in a comment may say "white canvas" without declaring one.
OUTSIDE_ROOT = re.sub(
    r"/\*.*?\*/", "", CSS[: ROOT_BLOCK.start()] + CSS[ROOT_BLOCK.end():], flags=re.S)

# Every notation a colour can hide in, not only the two the first draft caught.
COLOUR_NOTATION = (
    r"#[0-9a-fA-F]{3,8}\b"
    r"|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix)\("
    r"|\b(?:red|blue|green|black|white|gray|grey|silver|orange|purple|yellow|pink"
    r"|brown|navy|teal|olive|maroon|lime|aqua|cyan|magenta|fuchsia|gold|beige|ivory"
    r"|coral|crimson|indigo|violet|khaki|salmon|tan|plum|orchid|tomato|wheat)\b"
)
# Properties whose values DESIGN.md owns: the type ramp and the 4px spacing scale.
SCALED_PROPERTIES = (
    r"font-size"
    r"|(?:padding|margin)(?:-(?:top|right|bottom|left|inline|block))?"
    r"|(?:row-|column-)?gap"
)


def declarations(source, properties):
    return re.findall(rf"(?:^|[;{{])\s*({properties})\s*:\s*([^;{{}}]+)", source, re.M)


def test_every_canonical_id_is_one_join_key_across_markup_and_css():
    # The two message IDs are set by app.js rather than shipped in index.html, so
    # "markup" is the pair of files that can produce a node. The third leg of the
    # join -- that the browser suite selects on the same key -- is asserted there,
    # against a live DOM.
    markup = HTML + SCRIPT
    assert len(CANONICAL_IDS) == len(set(CANONICAL_IDS)) == 12
    for name in CANONICAL_IDS:
        assert f'"{name}"' in markup, f"{name} missing from markup"
        assert f'[data-ux="{name}"]' in CSS, f"{name} missing from styles.css"


def test_no_data_ux_key_exists_outside_the_canonical_twelve():
    for label, source in (("html", HTML), ("css", CSS), ("js", SCRIPT)):
        found = set(re.findall(r"UX-[A-Z][A-Z-]+", source))
        assert found <= set(CANONICAL_IDS), (label, found - set(CANONICAL_IDS))


def test_every_colour_literal_lives_in_the_root_token_block():
    assert ROOT_BLOCK, "styles.css has no :root token block"
    # Anchored to a property value, so an id selector that happens to start with
    # hex-ish characters is not mistaken for a colour.
    literals = re.findall(rf":[^;{{}}]*?({COLOUR_NOTATION})", OUTSIDE_ROOT)
    assert not literals, literals
    for token in re.findall(r"--([a-z-]+):", ROOT_BLOCK.group(1)):
        assert f"var(--{token})" in CSS, f"--{token} is declared but never used"


def test_the_type_ramp_and_the_spacing_scale_are_tokens_everywhere():
    """DESIGN.md owns both, and the frozen Always names both, so a stray `18px`
    has to fail exactly the way a stray `#f8fafc` does."""
    for name, value in declarations(OUTSIDE_ROOT, SCALED_PROPERTIES):
        for part in value.replace("!important", "").split():
            assert part in {"0", "auto", "inherit"} or part.startswith("var(--"), (name, value)
    # Non-vacuous: the ramp and the scale really are declared, and really are used.
    ramp = re.findall(r"--(text-[a-z-]+):", ROOT_BLOCK.group(1))
    scale = re.findall(r"--(space-\d+):", ROOT_BLOCK.group(1))
    assert len(ramp) >= 6 and len(scale) >= 8, (ramp, scale)
    assert any(name == "font-size" for name, _ in declarations(OUTSIDE_ROOT, SCALED_PROPERTIES))


def _relative_luminance(value):
    channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(left, right):
    first, second = _relative_luminance(left), _relative_luminance(right)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def test_the_palette_meets_the_wcag_body_border_and_focus_ratios():
    """The ratios the frozen matrix names, computed from the tokens themselves, so a
    palette edit cannot quietly drop below them. Task 25 renamed the tokens to the
    Apple set (docs/DESIGN.md, Project Application)."""
    tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6});", CSS))
    # Every surface text or a focus ring can land on: the white canvas (also the
    # active navigation item), parchment (sidebar, bubble, code) and the error card.
    surfaces = ["bg", "parchment", "danger-soft"]

    for surface in surfaces:
        # 본문·설명·metadata: 4.5:1.
        for ink in ("ink", "ink-2"):
            assert _contrast(tokens[ink], tokens[surface]) >= 4.5, (ink, surface)
        # Focus: 3:1, on the white canvas and on every subsidiary surface.
        assert _contrast(tokens["focus"], tokens[surface]) >= 3, surface

    # --accent is text, not only a fill: source titles are drawn in it on white.
    assert _contrast(tokens["accent"], tokens["bg"]) >= 4.5
    # The blue fills (send, stop, 재시도) carry --on-accent.
    assert _contrast(tokens["on-accent"], tokens["accent"]) >= 4.5
    # The 신뢰도 낮음 / 논쟁 중 flags are text on the white source card.
    assert _contrast(tokens["warning"], tokens["bg"]) >= 4.5
    # The owner's AA decision: #7a7a7a (--ink-fine) is fine print only -- the
    # tagline, the TTL note, the placeholder and a disabled control's glyph --
    # never body text, notices, metadata or the input warning.
    fine = {selector.strip() for selector in re.findall(
        r"([^{}]+)\{[^}]*var\(--ink-fine\)[^}]*\}", OUTSIDE_ROOT)}
    allowed = {".brand small", ".nav-note", ".pill textarea::placeholder", ".pill-btn:disabled",
               '.pill-btn[data-ux="UX-PRIMARY-ACTION"]:disabled', ".send:disabled",
               ".think.live span"}
    assert fine and fine <= allowed, fine - allowed
    assert "outline: 2px solid var(--focus)" in CSS


def test_status_and_correlation_ids_are_never_monospace():
    # Narrow: what a font-family actually resolves to, and the tags that carry a
    # monospace default -- not the word appearing anywhere in a comment. Since
    # Task 25 answers render Markdown code in monospace: that face is the single
    # --font-mono token, and only the answer's own code (`.md ... code/pre`) reads it,
    # so a status line, a recovery row or a Correlation ID can never pick it up.
    mono = re.search(r"--font-mono:\s*([^;]+);", ROOT_BLOCK.group(1))
    assert mono and re.search(r"\bmonospace\b", mono.group(1))
    for label, source in (("html", HTML), ("css", OUTSIDE_ROOT), ("js", SCRIPT)):
        for family in re.findall(r"font(?:-family)?\s*:\s*([^;{}\"']+)", source):
            assert not re.search(
                r"\b(?:monospace|courier|consolas|menlo|monaco|ui-monospace)\b", family, re.I
            ), (label, family)
        assert not re.search(r"<\s*(?:code|pre|kbd|samp)\b", source, re.I), label
    readers = [selector.strip() for selector in re.findall(
        r"([^{}]+)\{[^}]*var\(--font-mono\)[^}]*\}", OUTSIDE_ROOT)]
    assert readers and all(selector.startswith(".md ") for selector in readers), readers
