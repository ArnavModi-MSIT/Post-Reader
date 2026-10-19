"""Bounded pagination with compact, local diagnostics and explicit outcomes."""
import time
import argparse
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin
from playwright.sync_api import Error as PlaywrightError


@dataclass
class PaginationResult:
    status: str
    current_page_url: str
    visible_job_count: int
    next_controls: list = field(default_factory=list)
    navigation_attempted: bool = False
    error: str = ""
    method: str = "controls"

    def __bool__(self):
        return self.status == "advanced"

    def to_dict(self):
        return asdict(self)

    @property
    def reason(self):
        return {
            "scroll_idle": "No new listings loaded after scrolling and waiting. Progress is saved; resume to try loading more.",
            "verified_last_page": "The visible Next control is disabled; no further page was offered.",
            "missing_control": "No usable Next control found. Pagination may have changed; inspect pagination diagnostics.",
            "stale_results": "Next controls did not load different listings; pending progress was kept.",
            "navigation_failed": "Next-page navigation failed; inspect pagination diagnostics and resume.",
        }.get(self.status, self.status)


def advance(page, groups, links, accept_target=None, timeout=20):
    import reader
    before = set(links(page))
    origin = page.url
    result = PaginationResult("missing_control", origin, len(before))
    disabled = False
    deadline = time.monotonic() + timeout
    for group in groups:
        for control in group.all():
            reader.check_cancel(vars(page).get("_reader_stop_event"))
            if len(result.next_controls) >= 20:
                return result
            try:
                visible = control.is_visible()
                classes = control.get_attribute("class") or ""
                ancestor = control.locator('xpath=ancestor::*[@disabled or @aria-disabled="true" or contains(concat(" ",normalize-space(@class)," ")," disabled ")]').count()
                enabled = (control.is_enabled() and control.get_attribute("aria-disabled") != "true"
                           and "disabled" not in (classes.split() if isinstance(classes, str) else [])
                           and not (isinstance(ancestor, int) and ancestor > 0))
                href = control.get_attribute("href")
                href = href if isinstance(href, str) else ""
                label = control.inner_text()
                label = label[:160] if isinstance(label, str) else ""
                result.next_controls.append({"text": label, "href": href[:2048], "visible": visible, "enabled": bool(enabled)})
                if visible and not enabled:
                    disabled = True
                if not visible or not enabled:
                    continue
                target = None
                if href and not href.startswith(("#", "javascript:")):
                    target = urljoin(origin, href)
                    if accept_target and not accept_target(target):
                        continue
                    if reader.search_signature(target) != reader.search_signature(origin):
                        continue
                if time.monotonic() >= deadline:
                    break
                result.navigation_attempted = True
                if target:
                    reader.navigate(page, target)
                else:
                    control.click(timeout=3000)
                until = min(deadline, time.monotonic() + 5)
                while time.monotonic() < until:
                    reader.check_cancel(vars(page).get("_reader_stop_event"))
                    reader.check_access(page)
                    try:
                        after = set(links(page))
                        if after and after != before:
                            if reader.search_signature(page.url) != reader.search_signature(origin):
                                raise ValueError("Next page changed the selected search filters.")
                            result.status, result.current_page_url = "advanced", page.url
                            return result
                    except PlaywrightError as exc:
                        if "Execution context was destroyed" not in str(exc):
                            raise
                    reader.interruptible_wait(page, 250)
                result.status = "stale_results"
            except reader.AccessBlocked as exc:
                result.status = "access_blocked"
                exc.pagination = result.to_dict()
                raise
            except (PlaywrightError, ValueError, RuntimeError, argparse.ArgumentTypeError) as exc:
                reader.check_access(page)
                result.status = "navigation_failed"
                result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                # Try other actual controls. Never invent an offset URL.
                continue
    if not result.navigation_attempted and disabled:
        result.status = "verified_last_page"
    return result
