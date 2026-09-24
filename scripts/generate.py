#!/usr/bin/env python3
"""Render the SVG cards for the GitHub profile README.

Pulls public contribution data from the GitHub GraphQL API, writes a light and
a dark variant of each card to assets/, and refreshes the generated block in
README.md (image alt text and the table view).

    GITHUB_TOKEN=$(gh auth token) python3 scripts/generate.py
"""

import argparse
import collections
import dataclasses
import datetime
import html
import json
import math
import os
import pathlib
import re
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
README = ROOT / "README.md"
API = "https://api.github.com/graphql"
ATTEMPTS = 3

NAME = "Emilien Macchi"
ROLE = "Staff Engineer & Architect at Red Hat"
TEAM = "AI Platform Core Components"
TAGLINES = [
    "productizing AI at Red Hat",
    "building Python wheels from source",
    "agentic CI and developer tooling",
    "previously: Kubernetes & OpenStack",
]
# Years whose work mostly happened off GitHub, called out on the yearly chart.
OFF_GITHUB = (2012, 2019, "OpenStack years: most of the work lived on Gerrit")

SANS = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', "
    "Helvetica, Arial, sans-serif"
)
MONO = (
    "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
    "'Liberation Mono', monospace"
)
TYPE_SIZE = 17
TYPE_SLOT = 4.5  # seconds each tagline stays on screen

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#6f6d68",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "border": "rgba(11,11,11,0.10)",
        "accent": "#2a78d6",
        "accent_text": "#256abf",
        "track": "#cde2fb",
        "violet": "#4a3aa7",
        "orange": "#eb6834",
        "aqua": "#1baf7a",
        "glow": "0.22",
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#ffffff",
        "ink2": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "border": "rgba(255,255,255,0.10)",
        "accent": "#3987e5",
        "accent_text": "#3987e5",
        "track": "#0d366b",
        "violet": "#9085e9",
        "orange": "#d95926",
        "aqua": "#199e70",
        "glow": "0.30",
    },
}

METRICS = [
    ("contributions", "Contributions"),
    ("commits", "Commits"),
    ("pull_requests", "Pull requests"),
    ("reviews", "Code reviews"),
    ("issues", "Issues"),
]

YEARS_QUERY = """
query($login: String!) {
  user(login: $login) { contributionsCollection { contributionYears } }
}
"""

REPO_FIELDS = """{
  repository { nameWithOwner isPrivate primaryLanguage { name } }
  contributions { totalCount }
}"""

YEAR_QUERY = f"""
query($login: String!, $from: DateTime!, $to: DateTime!) {{
  user(login: $login) {{
    contributionsCollection(from: $from, to: $to) {{
      contributionCalendar {{ totalContributions }}
      totalCommitContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalIssueContributions
      commitContributionsByRepository(maxRepositories: 100) {REPO_FIELDS}
      pullRequestContributionsByRepository(maxRepositories: 100) {REPO_FIELDS}
      pullRequestReviewContributionsByRepository(maxRepositories: 100) {REPO_FIELDS}
      issueContributionsByRepository(maxRepositories: 100) {REPO_FIELDS}
    }}
  }}
}}
"""

BASE_CSS = f"""
text {{ font-family: {SANS}; }}
.mono {{ font-family: {MONO}; }}
.num {{ font-variant-numeric: tabular-nums; }}
.fade {{ animation: fade 600ms ease-out both; }}
.grow-x {{ animation: grow-x 900ms cubic-bezier(.2,.7,.2,1) both; }}
.grow-y {{ animation: grow-y 900ms cubic-bezier(.2,.7,.2,1) both; }}
@keyframes fade {{ from {{ opacity: 0; }} }}
@keyframes grow-x {{ from {{ transform: scaleX(0); }} }}
@keyframes grow-y {{ from {{ transform: scaleY(0); }} }}
@media (prefers-reduced-motion: reduce) {{
  * {{ animation: none !important; }}
}}
"""


@dataclasses.dataclass
class Stats:
    years: dict[int, dict[str, int]]
    languages: list[tuple[str, float]]
    repositories: int

    def total(self, key: str) -> int:
        return sum(year[key] for year in self.years.values())


def graphql(query: str, variables: dict, token: str) -> dict:
    request = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-readme-generator",
        },
    )
    # Contribution queries are slow on GitHub's side and occasionally time out,
    # so transient failures get a few retries before failing the run.
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            retryable = error.code >= 500 or error.code == 403
            if not retryable or attempt == ATTEMPTS:
                raise RuntimeError(f"GitHub API {error.code}: {detail}") from error
        except urllib.error.URLError:
            if attempt == ATTEMPTS:
                raise
        else:
            errors = payload.get("errors") or []
            if any(e.get("type") == "NOT_FOUND" for e in errors):
                raise SystemExit(f"GitHub user {variables['login']!r} not found")
            if not errors:
                return payload["data"]["user"]
            if attempt == ATTEMPTS:
                raise RuntimeError(f"GraphQL errors: {errors}")
        time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def fetch(login: str, token: str) -> Stats:
    user = graphql(YEARS_QUERY, {"login": login}, token)
    years: dict[int, dict[str, int]] = {}
    commits_by_language: collections.Counter[str] = collections.Counter()
    repositories: set[str] = set()
    for year in sorted(user["contributionsCollection"]["contributionYears"]):
        variables = {
            "login": login,
            "from": f"{year}-01-01T00:00:00Z",
            "to": f"{year}-12-31T23:59:59Z",
        }
        data = graphql(YEAR_QUERY, variables, token)["contributionsCollection"]
        years[year] = {
            "contributions": data["contributionCalendar"]["totalContributions"],
            "commits": data["totalCommitContributions"],
            "pull_requests": data["totalPullRequestContributions"],
            "reviews": data["totalPullRequestReviewContributions"],
            "issues": data["totalIssueContributions"],
        }
        for key, items in data.items():
            if not key.endswith("ByRepository"):
                continue
            for item in items:
                repo = item["repository"]
                if repo["isPrivate"]:
                    continue
                repositories.add(repo["nameWithOwner"])
                if key.startswith("commit") and repo["primaryLanguage"]:
                    count = item["contributions"]["totalCount"]
                    commits_by_language[repo["primaryLanguage"]["name"]] += count
    total = sum(commits_by_language.values()) or 1
    languages = [
        (name, 100 * count / total)
        for name, count in commits_by_language.most_common(5)
    ]
    return Stats(years, languages, len(repositories))


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def compact(value: int) -> str:
    return f"{value:,}" if value < 10_000 else f"{value / 1000:.1f}K"


def percent(value: float) -> str:
    return "<1%" if value < 1 else f"{value:.0f}%"


def document(width: int, height: int, title: str, css: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="title">\n<title id="title">{esc(title)}</title>\n'
        f"<style>{BASE_CSS}{css}</style>\n{body}\n</svg>\n"
    )


def card(t: dict, width: int, height: int, title: str, subtitle: str) -> str:
    return (
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
        f'rx="12" fill="{t["surface"]}" stroke="{t["border"]}"/>\n'
        f'<text x="24" y="36" font-size="15" font-weight="600" '
        f'fill="{t["ink"]}">{esc(title)}</text>\n'
        f'<text x="24" y="56" font-size="12" fill="{t["ink2"]}">'
        f"{esc(subtitle)}</text>\n"
    )


def hbar(width: float, height: float, radius: float) -> str:
    """Horizontal bar from x=0, square at the baseline, rounded at the end."""
    r = round(min(radius, width, height / 2), 1)
    return (
        f"M0,0 H{width - r:.1f} A{r},{r} 0 0 1 {width:.1f},{r} "
        f"V{height - r:.1f} A{r},{r} 0 0 1 {width - r:.1f},{height} H0 Z"
    )


def vbar(width: float, height: float, radius: float) -> str:
    """Column rising from y=0, square at the baseline, rounded at the top."""
    r = round(min(radius, width / 2, height), 1)
    half = width / 2
    return (
        f"M{-half:.1f},0 V{r - height:.1f} "
        f"A{r},{r} 0 0 1 {r - half:.1f},{-height:.1f} "
        f"H{half - r:.1f} A{r},{r} 0 0 1 {half:.1f},{r - height:.1f} V0 Z"
    )


def nice_ticks(maximum: int, intervals: int = 5) -> list[int]:
    raw = max(maximum, 1) / intervals
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(
        int(m * magnitude)
        for m in (1, 2, 2.5, 5, 10)
        if m * magnitude >= raw and m * magnitude == int(m * magnitude)
    )
    top = max(math.ceil(maximum / step), 1) * step
    return list(range(0, top + 1, step))


def typing_frames(
    index: int, count: int, width: float, chars: int
) -> list[tuple[float, float, int | None]]:
    """Keyframes (percent, offset, steps) that type then erase one tagline."""
    slot = 100 / count
    start = index * slot
    return [
        (start, 0, chars),
        (start + slot * 0.30, width, None),
        (start + slot * 0.75, width, chars),
        (start + slot * 0.88, 0, None),
    ]


def keyframes(name: str, frames: list[tuple[float, float, int | None]]) -> str:
    if frames[0][0] > 0:
        frames = [(0, 0, None), *frames]
    frames = [*frames, (100, 0, None)]
    rules = []
    for pct, offset, steps in frames:
        timing = f" animation-timing-function: steps({steps}, end);" if steps else ""
        rules.append(
            f"{pct:.2f}% {{ transform: translateX({offset:.1f}px);{timing} }}"
        )
    return f"@keyframes {name} {{ {' '.join(rules)} }}\n"


def render_header(t: dict) -> str:
    width, height = 860, 240
    char = TYPE_SIZE * 0.6
    x, y = 48 + 2 * char, 204
    cycle = TYPE_SLOT * len(TAGLINES)

    css = """
.spin { animation: spin 24s linear infinite; }
.orbit-a { animation: spin 11s linear infinite; }
.orbit-b { animation: spin 17s linear infinite reverse; }
.orbit-c { animation: spin 29s linear infinite; }
.drift-a { animation: drift-a 16s ease-in-out infinite; }
.drift-b { animation: drift-b 21s ease-in-out infinite; }
.blink { animation: blink 1.1s step-end infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes drift-a { 50% { transform: translate(-40px, 24px); } }
@keyframes drift-b { 50% { transform: translate(30px, -20px); } }
@keyframes blink { 50% { opacity: 0; } }
"""
    clips, lines, cursor_frames = [], [], []
    for line in TAGLINES:
        if x + (len(line) + 1) * char > 600:
            raise SystemExit(f"Tagline too long, it would hit the wheel: {line!r}")
    for i, line in enumerate(TAGLINES):
        span = len(line) * char
        frames = typing_frames(i, len(TAGLINES), span, len(line))
        cursor_frames.extend(frames)
        css += keyframes(f"type-{i}", frames)
        # The static transform is what reduced-motion viewers see: the first
        # tagline fully typed and every other one hidden.
        shown = span if i == 0 else 0
        css += (
            f".type-{i} {{ transform: translateX({shown:.1f}px); "
            f"animation: type-{i} {cycle}s infinite; }}\n"
        )
        clips.append(
            f'<clipPath id="clip-{i}"><rect class="type-{i}" '
            f'x="{x - span:.1f}" y="{y - 20}" width="{span:.1f}" height="28"/>'
            f"</clipPath>"
        )
        lines.append(
            f'<text class="mono" x="{x:.1f}" y="{y}" font-size="{TYPE_SIZE}" '
            f'textLength="{span:.1f}" lengthAdjust="spacingAndGlyphs" '
            f'fill="{t["ink"]}" clip-path="url(#clip-{i})">{esc(line)}</text>'
        )
    first = len(TAGLINES[0]) * char
    css += keyframes("cursor", cursor_frames)
    css += (
        f".cursor {{ transform: translateX({first:.1f}px); "
        f"animation: cursor {cycle}s infinite; }}\n"
    )

    spokes = "".join(
        f'<line x1="{18 * math.cos(a):.1f}" y1="{18 * math.sin(a):.1f}" '
        f'x2="{58 * math.cos(a):.1f}" y2="{58 * math.sin(a):.1f}"/>'
        for a in (k * math.pi / 4 for k in range(8))
    )
    body = f"""<defs>
<clipPath id="frame"><rect width="{width}" height="{height}" rx="16"/></clipPath>
<filter id="blur" x="-50%" y="-50%" width="200%" height="200%">
<feGaussianBlur stdDeviation="42"/></filter>
<linearGradient id="name" x1="0" x2="1">
<stop offset="0" stop-color="{t["accent"]}"/>
<stop offset="1" stop-color="{t["violet"]}"/></linearGradient>
<pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse">
<circle cx="2" cy="2" r="1.1" fill="{t["muted"]}"/></pattern>
<linearGradient id="fade" x1="0" x2="1">
<stop offset="0.45" stop-color="#fff" stop-opacity="0"/>
<stop offset="1" stop-color="#fff"/></linearGradient>
<mask id="fade-mask"><rect width="{width}" height="{height}" fill="url(#fade)"/>
</mask>
{"".join(clips)}
</defs>
<g clip-path="url(#frame)">
<rect width="{width}" height="{height}" fill="{t["surface"]}"/>
<g filter="url(#blur)" opacity="{t["glow"]}">
<circle class="drift-a" cx="700" cy="70" r="120" fill="{t["accent"]}"/>
<circle class="drift-b" cx="820" cy="210" r="100" fill="{t["violet"]}"/>
<circle class="drift-a" cx="560" cy="250" r="70" fill="{t["orange"]}"/>
</g>
<rect width="{width}" height="{height}" fill="url(#dots)" mask="url(#fade-mask)"
 opacity="0.45"/>
</g>
<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="16"
 fill="none" stroke="{t["border"]}"/>
<text x="48" y="60" font-size="12" font-weight="600" letter-spacing="3"
 fill="{t["accent_text"]}">HELLO THERE!</text>
<text x="46" y="110" font-size="46" font-weight="700"
 fill="url(#name)">{esc(NAME)}</text>
<text x="48" y="142" font-size="18" font-weight="600"
 fill="{t["ink"]}">{esc(ROLE)}</text>
<text x="48" y="166" font-size="15" fill="{t["ink2"]}">{esc(TEAM)}</text>
<text class="mono" x="48" y="{y}" font-size="{TYPE_SIZE}" font-weight="700"
 fill="{t["accent_text"]}">$</text>
{"".join(lines)}
<g class="cursor"><rect class="blink" x="{x + 2:.1f}" y="{y - 15}"
 width="{char * 0.6:.1f}" height="19" rx="1" fill="{t["accent"]}"/></g>
<g transform="translate(720 124)">
<circle r="96" fill="none" stroke="{t["muted"]}" stroke-opacity="0.4"/>
<g class="spin">
<g stroke="{t["accent"]}" stroke-width="3" stroke-linecap="round">{spokes}</g>
<circle r="58" fill="none" stroke="{t["accent"]}" stroke-width="6"/>
</g>
<circle r="20" fill="{t["surface"]}" stroke="{t["accent"]}" stroke-width="4"/>
<text class="mono" y="4" font-size="10" font-weight="700" text-anchor="middle"
 fill="{t["ink"]}">.whl</text>
<g class="orbit-a"><circle cx="96" r="7" fill="{t["orange"]}"
 stroke="{t["surface"]}" stroke-width="2"/></g>
<g class="orbit-b"><circle cx="-96" r="6" fill="{t["aqua"]}"
 stroke="{t["surface"]}" stroke-width="2"/></g>
<g class="orbit-c"><circle cy="-96" r="5" fill="{t["violet"]}"
 stroke="{t["surface"]}" stroke-width="2"/></g>
</g>"""
    title = f"{NAME}, {ROLE}, {TEAM}"
    return document(width, height, title, css, body)


def render_stats(t: dict, stats: Stats) -> str:
    width, height = 420, 220
    first = min(stats.years)
    tiles = [(label, stats.total(key)) for key, label in METRICS]
    tiles.append(("Repos contributed", stats.repositories))
    body = card(t, width, height, "GitHub at a glance",
                f"All contributions since {first}")
    column = (width - 48) / 3
    for i, (label, value) in enumerate(tiles):
        x = 24 + (i % 3) * column
        y = 92 + (i // 3) * 68
        body += (
            f'<g class="fade" style="animation-delay:{i * 90}ms">'
            f'<rect x="{x:.1f}" y="{y - 11}" width="3" height="42" rx="1.5" '
            f'fill="{t["accent"]}"/>'
            f'<text x="{x + 12:.1f}" y="{y}" font-size="12" '
            f'fill="{t["ink2"]}">{esc(label)}</text>'
            f'<text x="{x + 12:.1f}" y="{y + 28}" font-size="24" '
            f'font-weight="600" fill="{t["ink"]}">{compact(value)}</text></g>\n'
        )
    return document(width, height, stats_alt(stats), "", body)


def render_languages(t: dict, stats: Stats) -> str:
    width, height = 420, 220
    body = card(t, width, height, "Top languages",
                "Share of commits, by repository language")
    track = width - 48
    for i, (name, share) in enumerate(stats.languages):
        y = 88 + i * 26
        body += (
            f'<text x="24" y="{y}" font-size="12.5" fill="{t["ink2"]}">'
            f"{esc(name)}</text>"
            f'<text class="num" x="{width - 24}" y="{y}" font-size="12.5" '
            f'font-weight="600" text-anchor="end" fill="{t["ink"]}">'
            f"{esc(percent(share))}</text>"
            f'<rect x="24" y="{y + 6}" width="{track}" height="6" rx="3" '
            f'fill="{t["track"]}"/>'
            f'<g transform="translate(24 {y + 6})"><path class="grow-x" '
            f'style="animation-delay:{i * 90}ms" '
            f'd="{hbar(track * share / 100, 6, 3)}" fill="{t["accent"]}"/></g>\n'
        )
    return document(width, height, languages_alt(stats), "", body)


def render_years(t: dict, stats: Stats, today: datetime.date) -> str:
    width, height = 860, 280
    left, right, top, bottom = 64, 836, 88, 236
    years = sorted(stats.years)
    values = [stats.years[year]["contributions"] for year in years]
    ticks = nice_ticks(max(values))
    band = (right - left) / len(years)
    bar = min(24, band * 0.6)

    # Labels get a surface-colored outline so gridlines never cut through them.
    halo = f'stroke="{t["surface"]}" stroke-width="4" paint-order="stroke"'

    def scale(value: int) -> float:
        return (bottom - top) * value / ticks[-1]

    subtitle = "GitHub contributions"
    if years[-1] == today.year:
        subtitle += f", {today.year} is year to date"
    body = card(t, width, height, "Contributions per year", subtitle)
    for tick in ticks:
        ty = bottom - scale(tick)
        color = t["axis"] if tick == 0 else t["grid"]
        body += (
            f'<line x1="{left}" x2="{right}" y1="{ty:.1f}" y2="{ty:.1f}" '
            f'stroke="{color}"/>'
            f'<text class="num" x="{left - 10}" y="{ty + 4:.1f}" font-size="11" '
            f'text-anchor="end" fill="{t["muted"]}">{tick:,}</text>\n'
        )
    labelled = {years[values.index(max(values))], years[-1]}
    for i, (year, value) in enumerate(zip(years, values)):
        cx = left + band * (i + 0.5)
        body += (
            f'<text class="num" x="{cx:.1f}" y="{bottom + 18}" font-size="11" '
            f'text-anchor="middle" fill="{t["muted"]}">{year}</text>'
        )
        h = scale(value)
        if h >= 0.5:
            body += (
                f'<g transform="translate({cx:.1f} {bottom})">'
                f'<path class="grow-y" style="animation-delay:{i * 50}ms" '
                f'd="{vbar(bar, h, 4)}" fill="{t["accent"]}"/></g>'
            )
        if year in labelled:
            body += (
                f'<text class="fade num" style="animation-delay:900ms" '
                f'x="{cx:.1f}" y="{bottom - h - 8:.1f}" font-size="12" '
                f'font-weight="600" text-anchor="middle" fill="{t["ink"]}" '
                f"{halo}>{value:,}</text>"
            )
        body += "\n"

    start, end, note = OFF_GITHUB
    inside = [i for i, year in enumerate(years) if start <= year <= end]
    if inside:
        x1 = left + band * (inside[0] + 0.15)
        x2 = left + band * (inside[-1] + 0.85)
        tallest = max(scale(values[i]) for i in inside)
        by = max(top + 20, bottom - tallest - 22)
        body += (
            f'<path d="M{x1:.1f},{by + 6:.1f} V{by:.1f} H{x2:.1f} V{by + 6:.1f}" '
            f'fill="none" stroke="{t["axis"]}"/>'
            f'<text x="{(x1 + x2) / 2:.1f}" y="{by - 7:.1f}" font-size="11" '
            f'text-anchor="middle" fill="{t["ink2"]}" {halo}>'
            f"{esc(note)}</text>\n"
        )
    return document(width, height, years_alt(stats), "", body)


def stats_alt(stats: Stats) -> str:
    parts = [f"{stats.total(key):,} {label.lower()}" for key, label in METRICS]
    parts.append(f"{stats.repositories:,} repositories contributed to")
    return "GitHub at a glance: " + ", ".join(parts)


def languages_alt(stats: Stats) -> str:
    parts = [f"{name} {percent(share)}" for name, share in stats.languages]
    return "Top languages by commits: " + ", ".join(parts)


def years_alt(stats: Stats) -> str:
    years = sorted(stats.years)
    peak = max(years, key=lambda year: stats.years[year]["contributions"])
    return (
        f"Contributions per year from {years[0]} to {years[-1]}, peaking at "
        f"{stats.years[peak]['contributions']:,} in {peak}"
    )


def picture(name: str, alt: str, width: int) -> str:
    return (
        "<picture>\n"
        f'  <source media="(prefers-color-scheme: dark)" '
        f'srcset="assets/{name}-dark.svg">\n'
        f'  <img alt="{esc(alt)}" src="assets/{name}-light.svg" '
        f'width="{width}">\n'
        "</picture>"
    )


def readme_block(stats: Stats, today: datetime.date) -> str:
    rows = []
    for year in sorted(stats.years, reverse=True):
        label = f"{year} (YTD)" if year == today.year else str(year)
        cells = " | ".join(f"{stats.years[year][key]:,}" for key, _ in METRICS)
        rows.append(f"| {label} | {cells} |")
    totals = " | ".join(f"**{stats.total(key):,}**" for key, _ in METRICS)
    rows.append(f"| **Total** | {totals} |")
    languages = [
        f"| {name} | {percent(share)} |" for name, share in stats.languages
    ]
    header = " | ".join(label for _, label in METRICS)
    return "\n".join([
        "<!-- Generated by scripts/generate.py: edits here are overwritten. -->",
        '<p align="center">',
        picture("stats", stats_alt(stats), 420),
        picture("languages", languages_alt(stats), 420),
        "</p>",
        '<p align="center">',
        picture("years", years_alt(stats), 860),
        "</p>",
        "",
        "<details>",
        "<summary>Same numbers, as tables</summary>",
        "",
        f"| Year | {header} |",
        "|---:|" + "---:|" * len(METRICS),
        *rows,
        "",
        "| Language | Share of commits |",
        "|---|---:|",
        *languages,
        "",
        "</details>",
        "",
        f"<sub>Cards regenerated daily by [a GitHub Action]"
        f"(.github/workflows/update-stats.yml). Last update: "
        f"{today.isoformat()}.</sub>",
    ])


def update_readme(stats: Stats, today: datetime.date) -> None:
    start, end = "<!-- stats:start -->", "<!-- stats:end -->"
    pattern = re.compile(re.escape(start) + ".*?" + re.escape(end), re.S)
    text = README.read_text(encoding="utf-8")
    if not pattern.search(text):
        raise SystemExit(f"README.md is missing the {start} / {end} markers")
    block = f"{start}\n{readme_block(stats, today)}\n{end}"
    README.write_text(pattern.sub(lambda _: block, text), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--user",
        default=os.environ.get("GITHUB_REPOSITORY_OWNER", "EmilienM"),
        help="GitHub login to render (default: %(default)s)",
    )
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is not set")

    stats = fetch(args.user, token)
    if not stats.years:
        raise SystemExit(f"No contributions found for {args.user!r}")
    today = datetime.datetime.now(datetime.timezone.utc).date()
    # Render everything before writing so a failure leaves no half-updated tree.
    cards = {}
    for mode, theme in THEMES.items():
        cards[f"header-{mode}.svg"] = render_header(theme)
        cards[f"stats-{mode}.svg"] = render_stats(theme, stats)
        cards[f"languages-{mode}.svg"] = render_languages(theme, stats)
        cards[f"years-{mode}.svg"] = render_years(theme, stats, today)
    ASSETS.mkdir(exist_ok=True)
    for name, content in cards.items():
        (ASSETS / name).write_text(content, encoding="utf-8")
    update_readme(stats, today)


if __name__ == "__main__":
    main()
