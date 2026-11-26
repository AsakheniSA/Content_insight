"""Which collectors exist, and which ones this run should use.

Adding a source is: write the module, add its factory below, add the YAML entry. The
registry refuses to plan a run for an enabled source it has no module for, so a typo in
`sources.yaml` fails at planning time rather than silently collecting nothing.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import AppConfig, ResolvedSource
from ..utils.logging import get_logger
from .base import Collector, CollectorError, RunContext

log = get_logger(__name__)


def _web_search(name: str) -> Callable[[RunContext], Collector]:
    def build(ctx: RunContext) -> Collector:
        from .web_search import WebSearchCollector

        return WebSearchCollector(name)

    return build


def _youtube(ctx: RunContext) -> Collector:
    from .youtube import YouTubeCollector

    return YouTubeCollector()


def _news_rss(ctx: RunContext) -> Collector:
    from .news_rss import NewsRssCollector

    return NewsRssCollector()


def _manual(ctx: RunContext) -> Collector:
    from .manual import ManualCollector

    return ManualCollector()


#: Source name -> factory.
_FACTORIES: dict[str, Callable[[RunContext], Collector]] = {
    "web_questions": _web_search("web_questions"),
    # Disabled in sources.yaml: Reddit blocks Anthropic's crawler. Kept registered so it
    # can be switched back on, unchanged, if that ever changes.
    "reddit_search": _web_search("reddit_search"),
    "manual": _manual,
    "youtube": _youtube,
    "news_rss": _news_rss,
}


def known_collectors() -> frozenset[str]:
    """Names that have a module behind them."""
    return frozenset(_FACTORIES)


def build_collector(name: str, ctx: RunContext) -> Collector:
    try:
        factory = _FACTORIES[name]
    except KeyError:
        raise CollectorError(
            f"No collector module for source {name!r}. Known collectors: "
            f"{', '.join(sorted(_FACTORIES)) or '(none)'}."
        ) from None
    return factory(ctx)


def plan_sources(config: AppConfig, *, source: str = "all") -> list[ResolvedSource]:
    """The sources this run should collect from.

    `source` filters to one name. A source that is named explicitly but disabled is an
    error — asking for it and silently getting nothing is worse than being told.
    """
    enabled = config.enabled_sources

    if source and source != "all":
        all_names = {s.name for s in config.sources.resolve()}
        if source not in all_names:
            raise CollectorError(
                f"Unknown source {source!r}. Sources in config/sources.yaml: "
                f"{', '.join(sorted(all_names))}."
            )
        chosen = [s for s in enabled if s.name == source]
        if not chosen:
            raise CollectorError(
                f"Source {source!r} is disabled in config/sources.yaml. Set "
                f"`enabled: true` on it to collect from it."
            )
    else:
        chosen = list(enabled)

    missing = [s.name for s in chosen if s.name not in _FACTORIES]
    if missing:
        raise CollectorError(
            f"These sources are enabled in config/sources.yaml but have no collector "
            f"module yet: {', '.join(missing)}. Either build them or set "
            f"`enabled: false` until their stage lands."
        )

    log.info("plan", extra={"sources": ",".join(s.name for s in chosen) or "(none)"})
    return chosen
