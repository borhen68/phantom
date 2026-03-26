"""Optional Playwright-backed browser workflow runtime."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from core.settings import data_root, scope_id


DEFAULT_BROWSER_TIMEOUT_MS = 15_000
BROWSER_ACTIONS = {
    "goto",
    "click",
    "fill",
    "press",
    "wait_for",
    "extract_text",
    "assert_text",
    "screenshot",
}


def _safe_fragment(value: str) -> str:
    chars = [char if char.isalnum() else "_" for char in str(value or "")]
    return "".join(chars)[:80] or "default_scope"


def browser_artifact_root() -> Path:
    root = data_root() / "browser_artifacts" / _safe_fragment(scope_id())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _load_sync_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "playwright is required for browser automation. Install it with "
            "`pip install playwright` and browser binaries with `python -m playwright install`."
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


def _artifact_path(index: int, label: str, suffix: str = ".png") -> Path:
    stamp = int(time.time() * 1000)
    safe_label = _safe_fragment(label)[:40]
    return browser_artifact_root() / f"{stamp}_{index}_{safe_label}{suffix}"


def _safe_page_title(page) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def _safe_body_preview(page, limit: int = 240) -> str:
    try:
        text = page.locator("body").inner_text(timeout=1500)
    except Exception:
        return ""
    return str(text).replace("\n", " ").strip()[:limit]


def _verify_browser_step(page, action: str, step: dict[str, Any], *, extracted_text: str = "", screenshot_path: str = "") -> tuple[bool, str]:
    if action == "goto":
        url = str(step.get("url") or "").strip()
        current = str(getattr(page, "url", "") or "")
        if url and current.startswith(url):
            return True, f"url={current}"
        if current:
            return False, f"landed at {current}"
        return False, "page url unavailable"
    if action == "extract_text":
        if extracted_text.strip():
            return True, f"extracted={extracted_text[:120]}"
        return False, "no text extracted"
    if action == "assert_text":
        return True, "asserted expected text"
    if action == "screenshot":
        return Path(screenshot_path).exists(), f"screenshot={screenshot_path}"

    verify_url_contains = str(step.get("verify_url_contains") or "").strip()
    if verify_url_contains:
        current = str(getattr(page, "url", "") or "")
        return verify_url_contains in current, f"url={current}"

    verify_selector = str(step.get("verify_selector") or "").strip()
    if verify_selector:
        state = str(step.get("verify_state") or "visible")
        try:
            page.locator(verify_selector).wait_for(state=state, timeout=int(step.get("timeout_ms") or DEFAULT_BROWSER_TIMEOUT_MS))
            return True, f"{verify_selector} is {state}"
        except Exception as exc:
            return False, f"{verify_selector} not {state}: {exc}"

    verify_text_selector = str(step.get("verify_text_selector") or "").strip()
    verify_text = str(step.get("verify_text") or "").strip()
    if verify_text_selector and verify_text:
        try:
            text = page.locator(verify_text_selector).inner_text(timeout=int(step.get("timeout_ms") or DEFAULT_BROWSER_TIMEOUT_MS))
            return verify_text in text, f"{verify_text_selector}={str(text)[:120]}"
        except Exception as exc:
            return False, f"verification read failed: {exc}"

    if action in {"click", "fill", "press", "wait_for"}:
        return True, "action completed"
    return True, "step completed"


def _drift_report(page, index: int, step: dict[str, Any], exc: Exception) -> dict[str, Any]:
    screenshot = ""
    try:
        path = _artifact_path(index, "drift")
        page.screenshot(path=str(path), full_page=True)
        screenshot = str(path)
    except Exception:
        screenshot = ""
    return {
        "suspected": True,
        "index": index,
        "action": str(step.get("action") or ""),
        "target": str(step.get("selector") or step.get("url") or step.get("url_contains") or step.get("name") or ""),
        "error": str(exc),
        "current_url": str(getattr(page, "url", "") or ""),
        "title": _safe_page_title(page),
        "body_preview": _safe_body_preview(page),
        "screenshot": screenshot,
    }


def run_browser_workflow(
    steps: list[dict[str, Any]],
    *,
    headless: bool = True,
    browser_name: str = "chromium",
    capture_final_screenshot: bool = True,
    default_timeout_ms: int = DEFAULT_BROWSER_TIMEOUT_MS,
    sync_playwright_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(steps, list) or not steps:
        raise ValueError("Browser workflow requires a non-empty list of steps.")

    factory = sync_playwright_factory
    timeout_exc = RuntimeError
    if factory is None:
        factory, timeout_exc = _load_sync_playwright()

    executed: list[str] = []
    extracted: list[dict[str, str]] = []
    screenshots: list[str] = []
    step_results: list[dict[str, Any]] = []
    title = ""
    final_url = ""

    with factory() as playwright:
        browser_type = getattr(playwright, browser_name, None)
        if browser_type is None:
            raise ValueError(f"Unsupported browser name: {browser_name}")
        browser = browser_type.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        try:
            for index, raw_step in enumerate(steps, start=1):
                if not isinstance(raw_step, dict):
                    raise ValueError(f"Browser step {index} must be an object.")
                step = dict(raw_step)
                action = str(step.get("action") or "").strip().lower()
                if action not in BROWSER_ACTIONS:
                    raise ValueError(f"Unsupported browser action: {action}")
                timeout_ms = int(step.get("timeout_ms") or default_timeout_ms)
                step_result = {
                    "index": index,
                    "action": action,
                    "ok": False,
                    "verified": False,
                    "detail": "",
                    "url": "",
                    "title": "",
                }
                try:
                    extracted_text = ""
                    screenshot_path = ""
                    if action == "goto":
                        url = str(step.get("url") or "").strip()
                        if not url:
                            raise ValueError("browser goto step requires url")
                        page.goto(url, wait_until=str(step.get("wait_until") or "load"), timeout=timeout_ms)
                        executed.append(f"goto {url}")
                    elif action == "click":
                        selector = str(step.get("selector") or "").strip()
                        if not selector:
                            raise ValueError("browser click step requires selector")
                        page.locator(selector).click(timeout=timeout_ms)
                        executed.append(f"click {selector}")
                    elif action == "fill":
                        selector = str(step.get("selector") or "").strip()
                        if not selector:
                            raise ValueError("browser fill step requires selector")
                        value = str(step.get("value") or "")
                        page.locator(selector).fill(value, timeout=timeout_ms)
                        executed.append(f"fill {selector}")
                    elif action == "press":
                        key = str(step.get("key") or "").strip()
                        if not key:
                            raise ValueError("browser press step requires key")
                        selector = str(step.get("selector") or "").strip()
                        if selector:
                            page.locator(selector).press(key, timeout=timeout_ms)
                            executed.append(f"press {key} on {selector}")
                        else:
                            page.keyboard.press(key)
                            executed.append(f"press {key}")
                    elif action == "wait_for":
                        selector = str(step.get("selector") or "").strip()
                        url_contains = str(step.get("url_contains") or "").strip()
                        state = str(step.get("state") or "visible")
                        if selector:
                            page.locator(selector).wait_for(state=state, timeout=timeout_ms)
                            executed.append(f"wait_for {selector}")
                        elif url_contains:
                            deadline = time.time() + (timeout_ms / 1000.0)
                            while url_contains not in page.url:
                                if time.time() > deadline:
                                    raise timeout_exc(f"url did not contain {url_contains!r} within timeout")
                                page.wait_for_timeout(100)
                            executed.append(f"wait_for url_contains={url_contains}")
                        else:
                            raise ValueError("browser wait_for step requires selector or url_contains")
                    elif action == "extract_text":
                        selector = str(step.get("selector") or "").strip()
                        if not selector:
                            raise ValueError("browser extract_text step requires selector")
                        extracted_text = page.locator(selector).inner_text(timeout=timeout_ms)
                        extracted.append({
                            "selector": selector,
                            "name": str(step.get("name") or selector),
                            "text": extracted_text,
                        })
                        executed.append(f"extract_text {selector}")
                    elif action == "assert_text":
                        selector = str(step.get("selector") or "").strip()
                        expected = str(step.get("expected") or "").strip()
                        if not selector or not expected:
                            raise ValueError("browser assert_text step requires selector and expected")
                        text = page.locator(selector).inner_text(timeout=timeout_ms)
                        if expected not in text:
                            raise RuntimeError(
                                f"assert_text failed for {selector}: expected substring {expected!r}"
                            )
                        executed.append(f"assert_text {selector}")
                    elif action == "screenshot":
                        label = str(step.get("name") or step.get("label") or f"step_{index}").strip()
                        path = _artifact_path(index, label)
                        page.screenshot(path=str(path), full_page=bool(step.get("full_page", True)))
                        screenshot_path = str(path)
                        screenshots.append(screenshot_path)
                        executed.append(f"screenshot {path.name}")

                    verified, detail = _verify_browser_step(
                        page,
                        action,
                        step,
                        extracted_text=extracted_text,
                        screenshot_path=screenshot_path,
                    )
                    if not verified:
                        drift = _drift_report(page, index, step, RuntimeError(detail))
                        step_result.update({
                            "ok": False,
                            "verified": False,
                            "detail": detail,
                            "url": drift.get("current_url", ""),
                            "title": drift.get("title", ""),
                            "drift": drift,
                        })
                        step_results.append(step_result)
                        final_url = drift.get("current_url", "")
                        title = drift.get("title", "")
                        if drift.get("screenshot"):
                            screenshots.append(drift["screenshot"])
                        return {
                            "ok": False,
                            "error": detail,
                            "final_url": final_url,
                            "title": title,
                            "steps_executed": executed,
                            "extracted": extracted,
                            "screenshots": screenshots,
                            "step_results": step_results,
                            "drift_report": drift,
                        }
                    step_result.update({
                        "ok": True,
                        "verified": bool(verified),
                        "detail": detail,
                        "url": str(getattr(page, "url", "") or ""),
                        "title": _safe_page_title(page),
                    })
                    step_results.append(step_result)
                except Exception as exc:
                    drift = _drift_report(page, index, step, exc)
                    step_result.update({
                        "ok": False,
                        "verified": False,
                        "detail": str(exc),
                        "url": drift.get("current_url", ""),
                        "title": drift.get("title", ""),
                        "drift": drift,
                    })
                    step_results.append(step_result)
                    final_url = drift.get("current_url", "")
                    title = drift.get("title", "")
                    if drift.get("screenshot"):
                        screenshots.append(drift["screenshot"])
                    return {
                        "ok": False,
                        "error": str(exc),
                        "final_url": final_url,
                        "title": title,
                        "steps_executed": executed,
                        "extracted": extracted,
                        "screenshots": screenshots,
                        "step_results": step_results,
                        "drift_report": drift,
                    }

            final_url = page.url
            title = page.title()
            if capture_final_screenshot:
                path = _artifact_path(len(steps) + 1, "final_state")
                page.screenshot(path=str(path), full_page=True)
                screenshots.append(str(path))
        finally:
            browser.close()

    return {
        "ok": True,
        "final_url": final_url,
        "title": title,
        "steps_executed": executed,
        "extracted": extracted,
        "screenshots": screenshots,
        "step_results": step_results,
        "drift_report": None,
    }


def summarize_browser_result(result: dict[str, Any]) -> str:
    extracted = result.get("extracted") or []
    extracted_text = ""
    if extracted:
        preview = []
        for item in extracted[:4]:
            name = item.get("name") or item.get("selector") or "text"
            value = str(item.get("text") or "").replace("\n", " ").strip()
            preview.append(f"{name}={value[:120]}")
        extracted_text = "\nExtracted: " + "; ".join(preview)

    screenshots = result.get("screenshots") or []
    screenshot_text = ""
    if screenshots:
        screenshot_text = "\nScreenshots:\n" + "\n".join(f"  - {path}" for path in screenshots[:4])

    step_results = result.get("step_results") or []
    verification_text = ""
    if step_results:
        rows = []
        for item in step_results[:8]:
            state = "verified" if item.get("verified") else "executed" if item.get("ok") else "failed"
            rows.append(f"  - {item.get('index')}. {item.get('action')} -> {state} ({item.get('detail', '')[:100]})")
        verification_text = "\nVerification:\n" + "\n".join(rows)

    steps = result.get("steps_executed") or []
    steps_text = ""
    if steps:
        steps_text = "\nSteps:\n" + "\n".join(f"  - {step}" for step in steps[:8])

    drift = result.get("drift_report") or {}
    drift_text = ""
    if drift:
        parts = [
            f"action={drift.get('action')}",
            f"target={drift.get('target')}" if drift.get("target") else "",
            f"url={drift.get('current_url')}" if drift.get("current_url") else "",
            f"screenshot={drift.get('screenshot')}" if drift.get("screenshot") else "",
        ]
        drift_text = "\nUI drift suspected: " + ", ".join(part for part in parts if part)
        if drift.get("body_preview"):
            drift_text += f"\nBody preview: {drift['body_preview']}"

    status = "Browser workflow complete." if result.get("ok", True) else f"Browser workflow failed: {result.get('error', '')}"
    return (
        f"{status}\n"
        f"URL: {result.get('final_url', '')}\n"
        f"Title: {result.get('title', '')}"
        f"{steps_text}{verification_text}{extracted_text}{drift_text}{screenshot_text}"
    ).strip()


def workflow_payload_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=True)
