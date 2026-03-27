"""Optional Playwright-backed browser workflow runtime."""

from __future__ import annotations

import json
import hashlib
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from core.settings import data_root, scope_id


DEFAULT_BROWSER_TIMEOUT_MS = 15_000
_BODY_TOKEN_RE = re.compile(r"[a-z0-9]+")
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


def browser_session_root() -> Path:
    root = data_root() / "browser_sessions" / _safe_fragment(scope_id())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_dir(session_id: str) -> Path:
    safe_id = _safe_fragment(session_id)[:60]
    path = browser_session_root() / safe_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_manifest_path(session_id: str) -> Path:
    return _session_dir(session_id) / "session.json"


def _session_storage_state_path(session_id: str) -> Path:
    return _session_dir(session_id) / "storage_state.json"


def ensure_browser_session(
    session_id: str,
    *,
    browser_name: str = "chromium",
    headless: bool = True,
    attach_endpoint: str = "",
) -> dict[str, Any]:
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("browser session id is required")
    existing = get_browser_session(session_id)
    if existing and not attach_endpoint:
        return existing
    payload = dict(existing or {})
    payload.update({
        "session_id": session_id,
        "browser": str(browser_name or "chromium"),
        "headless": bool(headless),
        "created_at": float(payload.get("created_at") or time.time()),
        "updated_at": time.time(),
        "last_url": str(payload.get("last_url") or ""),
        "title": str(payload.get("title") or ""),
        "storage_state_path": str(_session_storage_state_path(session_id)),
        "artifact_root": str(_session_dir(session_id)),
        "attach_endpoint": str(attach_endpoint or payload.get("attach_endpoint") or ""),
    })
    _session_manifest_path(session_id).write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return payload


def get_browser_session(session_id: str) -> dict[str, Any] | None:
    path = _session_manifest_path(str(session_id or "").strip())
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def list_browser_sessions() -> list[dict[str, Any]]:
    sessions = []
    root = browser_session_root()
    for path in sorted(root.glob("*/session.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            sessions.append(payload)
    sessions.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    return sessions


def delete_browser_session(session_id: str) -> bool:
    session = get_browser_session(session_id)
    if not session:
        return False
    root = _session_dir(str(session_id))
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass
    return True


def attach_browser_session(
    session_id: str,
    attach_endpoint: str,
    *,
    browser_name: str = "chromium",
    headless: bool = True,
) -> dict[str, Any]:
    endpoint = str(attach_endpoint or "").strip()
    if not endpoint:
        raise ValueError("attach endpoint is required")
    return ensure_browser_session(
        session_id,
        browser_name=browser_name,
        headless=headless,
        attach_endpoint=endpoint,
    )


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


def _session_artifact_path(session_id: str, index: int, label: str, suffix: str = ".png") -> Path:
    stamp = int(time.time() * 1000)
    safe_label = _safe_fragment(label)[:40]
    return _session_dir(session_id) / f"{stamp}_{index}_{safe_label}{suffix}"


def _file_hash(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    file_path = Path(value)
    if not file_path.exists() or not file_path.is_file():
        return ""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _page_snapshot(
    page,
    *,
    index: int,
    label: str,
    session_id: str = "",
    screenshot_path: str = "",
) -> dict[str, Any]:
    path = str(screenshot_path or "").strip()
    if not path:
        shot = _session_artifact_path(session_id, index, label) if session_id else _artifact_path(index, label)
        try:
            page.screenshot(path=str(shot), full_page=True)
            path = str(shot)
        except Exception:
            path = ""
    return {
        "url": str(getattr(page, "url", "") or ""),
        "title": _safe_page_title(page),
        "body_preview": _safe_body_preview(page),
        "screenshot": path,
        "screenshot_hash": _file_hash(path),
    }


def _normalized_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/") or "/"
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
    return url.rstrip("/")


def _body_token_overlap(left: str, right: str) -> float:
    left_tokens = set(_BODY_TOKEN_RE.findall(str(left or "").lower()))
    right_tokens = set(_BODY_TOKEN_RE.findall(str(right or "").lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / float(len(left_tokens | right_tokens))


def _compare_snapshots(previous: dict[str, Any], current: dict[str, Any]) -> tuple[bool, str]:
    previous_url = _normalized_url(previous.get("url", ""))
    current_url = _normalized_url(current.get("url", ""))
    if previous_url and current_url and previous_url != current_url:
        return False, f"expected url {previous_url}, found {current_url}"

    previous_hash = str(previous.get("screenshot_hash") or "").strip()
    current_hash = str(current.get("screenshot_hash") or "").strip()
    if previous_hash and current_hash and previous_hash == current_hash:
        return True, "matched previous visual state"

    previous_title = str(previous.get("title") or "").strip().lower()
    current_title = str(current.get("title") or "").strip().lower()
    title_match = bool(previous_title and current_title and previous_title == current_title)

    overlap = _body_token_overlap(previous.get("body_preview", ""), current.get("body_preview", ""))
    if title_match and overlap >= 0.4:
        return True, f"matched page title with body overlap {overlap:.2f}"
    if overlap >= 0.7:
        return True, f"matched page body overlap {overlap:.2f}"

    expected = previous_url or previous.get("title") or "previous session state"
    observed = current_url or current.get("title") or "current page"
    return False, f"expected {expected}, found {observed} (body overlap {overlap:.2f})"


def _resume_preflight(page, *, session: dict[str, Any], session_id: str) -> tuple[bool, dict[str, Any], str, dict[str, Any] | None]:
    current = _page_snapshot(page, index=0, label="resume_preflight", session_id=session_id)
    previous = session.get("last_snapshot") if isinstance(session, dict) else None
    if not isinstance(previous, dict) or not previous:
        return True, current, "no previous session snapshot to compare", None

    ok, detail = _compare_snapshots(previous, current)
    if ok:
        return True, current, detail, None

    drift = {
        "suspected": True,
        "index": 0,
        "action": "resume_verification",
        "target": str(previous.get("url") or previous.get("title") or ""),
        "error": detail,
        "current_url": current.get("url", ""),
        "title": current.get("title", ""),
        "body_preview": current.get("body_preview", ""),
        "screenshot": current.get("screenshot", ""),
        "expected_snapshot": {
            "url": previous.get("url", ""),
            "title": previous.get("title", ""),
            "body_preview": previous.get("body_preview", ""),
            "screenshot": previous.get("screenshot", ""),
        },
    }
    return False, current, detail, drift


def _selector_candidates(step: dict[str, Any], *, key: str = "selector", fallback_key: str = "fallback_selectors") -> list[str]:
    candidates: list[str] = []
    primary = str(step.get(key) or "").strip()
    if primary:
        candidates.append(primary)
    fallbacks = step.get(fallback_key)
    if isinstance(fallbacks, (list, tuple)):
        for value in fallbacks:
            selector = str(value or "").strip()
            if selector and selector not in candidates:
                candidates.append(selector)
    return candidates


def _resolve_selector(
    page,
    step: dict[str, Any],
    *,
    key: str = "selector",
    fallback_key: str = "fallback_selectors",
    timeout_ms: int = DEFAULT_BROWSER_TIMEOUT_MS,
    state: str = "visible",
) -> tuple[str, bool, str]:
    candidates = _selector_candidates(step, key=key, fallback_key=fallback_key)
    if not candidates:
        return "", False, f"missing {key}"
    if len(candidates) == 1:
        return candidates[0], False, ""

    failures: list[str] = []
    primary = candidates[0]
    for candidate in candidates:
        try:
            page.locator(candidate).wait_for(state=state, timeout=timeout_ms)
            if candidate != primary:
                return candidate, True, f"re-anchored from {primary} to {candidate}"
            return candidate, False, ""
        except Exception as exc:
            failures.append(f"{candidate}: {exc}")
    return primary, False, "; ".join(failures[:3])


def _attempt_resume_recovery(
    page,
    *,
    session: dict[str, Any],
    session_id: str,
    timeout_ms: int,
) -> tuple[bool, dict[str, Any], str]:
    previous = session.get("last_snapshot") if isinstance(session, dict) else {}
    if not isinstance(previous, dict):
        previous = {}
    expected_url = str(previous.get("url") or session.get("last_url") or "").strip()
    if not expected_url:
        snapshot = _page_snapshot(page, index=0, label="resume_recovery_failed", session_id=session_id)
        return False, snapshot, "no expected url available for re-anchor"
    try:
        page.goto(expected_url, wait_until="load", timeout=timeout_ms)
    except Exception as exc:
        snapshot = _page_snapshot(page, index=0, label="resume_recovery_failed", session_id=session_id)
        return False, snapshot, f"re-anchor goto failed: {exc}"

    snapshot = _page_snapshot(page, index=0, label="resume_recovered", session_id=session_id)
    ok, detail = _compare_snapshots(previous or {"url": expected_url}, snapshot)
    if ok:
        return True, snapshot, f"recovered via {expected_url}: {detail}"
    return False, snapshot, f"re-anchor landed at {snapshot.get('url', '')}: {detail}"


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

    verify_selectors = _selector_candidates(step, key="verify_selector", fallback_key="fallback_verify_selectors")
    if verify_selectors:
        state = str(step.get("verify_state") or "visible")
        timeout_ms = int(step.get("timeout_ms") or DEFAULT_BROWSER_TIMEOUT_MS)
        failures: list[str] = []
        for candidate in verify_selectors:
            try:
                page.locator(candidate).wait_for(state=state, timeout=timeout_ms)
                if candidate != verify_selectors[0]:
                    return True, f"{candidate} is {state} (re-anchored verification from {verify_selectors[0]})"
                return True, f"{candidate} is {state}"
            except Exception as exc:
                failures.append(f"{candidate}: {exc}")
        return False, f"{verify_selectors[0]} not {state}: {'; '.join(failures[:3])}"

    verify_text_candidates = _selector_candidates(
        step,
        key="verify_text_selector",
        fallback_key="fallback_verify_text_selectors",
    )
    verify_text = str(step.get("verify_text") or "").strip()
    if verify_text_candidates and verify_text:
        timeout_ms = int(step.get("timeout_ms") or DEFAULT_BROWSER_TIMEOUT_MS)
        failures: list[str] = []
        for candidate in verify_text_candidates:
            try:
                text = page.locator(candidate).inner_text(timeout=timeout_ms)
                if verify_text in text:
                    if candidate != verify_text_candidates[0]:
                        return True, f"{candidate}={str(text)[:120]} (re-anchored verification from {verify_text_candidates[0]})"
                    return True, f"{candidate}={str(text)[:120]}"
                failures.append(f"{candidate}: missing text")
            except Exception as exc:
                failures.append(f"{candidate}: {exc}")
        return False, f"verification read failed: {'; '.join(failures[:3])}"

    if action in {"click", "fill", "press", "wait_for"}:
        return True, "action completed"
    return True, "step completed"


def _drift_report(page, index: int, step: dict[str, Any], exc: Exception, *, session_id: str = "") -> dict[str, Any]:
    screenshot = ""
    try:
        path = _session_artifact_path(session_id, index, "drift") if session_id else _artifact_path(index, "drift")
        page.screenshot(path=str(path), full_page=True)
        screenshot = str(path)
    except Exception:
        screenshot = ""
    report = {
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
    if step.get("fallback_selectors"):
        report["recovery_hint"] = "Try re-anchoring this step with fallback selectors or reteach the workflow."
    elif step.get("verify_selector") or step.get("verify_text_selector"):
        report["recovery_hint"] = "Verification target changed. Add fallback verification selectors or review the page state."
    return report


def _persist_browser_session(
    context,
    *,
    session_id: str,
    browser_name: str,
    headless: bool,
    final_url: str,
    title: str,
    ok: bool,
    attach_endpoint: str = "",
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = ensure_browser_session(
        session_id,
        browser_name=browser_name,
        headless=headless,
        attach_endpoint=attach_endpoint,
    )
    storage_state_path = Path(session["storage_state_path"])
    try:
        if hasattr(context, "storage_state"):
            context.storage_state(path=str(storage_state_path))
        elif not storage_state_path.exists():
            storage_state_path.write_text("{}", encoding="utf-8")
    except Exception:
        if not storage_state_path.exists():
            storage_state_path.write_text("{}", encoding="utf-8")
    payload = dict(session)
    payload.update({
        "browser": browser_name,
        "headless": bool(headless),
        "updated_at": time.time(),
        "last_url": final_url or "",
        "title": title or "",
        "last_ok": bool(ok),
        "last_snapshot": dict(snapshot or payload.get("last_snapshot") or {}),
        "storage_state_path": str(storage_state_path),
        "artifact_root": str(_session_dir(session_id)),
        "attach_endpoint": str(attach_endpoint or payload.get("attach_endpoint") or ""),
    })
    _session_manifest_path(session_id).write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return payload


def _existing_contexts(browser) -> list[Any]:
    contexts = getattr(browser, "contexts", [])
    if callable(contexts):
        contexts = contexts()
    return list(contexts or [])


def _existing_pages(context) -> list[Any]:
    pages = getattr(context, "pages", [])
    if callable(pages):
        pages = pages()
    return list(pages or [])


def _connect_browser(playwright, *, browser_name: str, headless: bool, attach_endpoint: str = ""):
    browser_type = getattr(playwright, browser_name, None)
    if browser_type is None:
        raise ValueError(f"Unsupported browser name: {browser_name}")
    endpoint = str(attach_endpoint or "").strip()
    if endpoint:
        connect = getattr(browser_type, "connect_over_cdp", None)
        if connect is None:
            raise ValueError(f"Browser {browser_name} does not support live attach.")
        return connect(endpoint), True
    return browser_type.launch(headless=headless), False


def _prepare_browser_context(browser, *, attached: bool, context_kwargs: dict[str, Any], prefer_existing_page: bool = False):
    reused_page = False
    contexts = _existing_contexts(browser) if attached else []
    context = contexts[0] if contexts else browser.new_context(**context_kwargs)
    pages = _existing_pages(context) if attached and prefer_existing_page else []
    if pages:
        page = pages[0]
        reused_page = True
    else:
        page = context.new_page()
    return context, page, reused_page


def run_browser_workflow(
    steps: list[dict[str, Any]],
    *,
    headless: bool = True,
    browser_name: str = "chromium",
    capture_final_screenshot: bool = True,
    default_timeout_ms: int = DEFAULT_BROWSER_TIMEOUT_MS,
    session_id: str = "",
    resume_session: bool = False,
    resume_last_page: bool = False,
    persist_session: bool = True,
    attach_endpoint: str = "",
    verify_resumed_state: bool = True,
    auto_reanchor: bool = True,
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
    session_id = str(session_id or "").strip()
    session = get_browser_session(session_id) if session_id else None
    resumed = bool(session_id and resume_session and session)
    attach_endpoint = str(attach_endpoint or (session.get("attach_endpoint") if resumed and session else "") or "").strip()
    attached = False
    storage_state_path = ""
    session_verification = "not applicable"
    if resumed and session and not attach_endpoint:
        storage_state_path = str(session.get("storage_state_path") or "")

    with factory() as playwright:
        context_kwargs: dict[str, Any] = {}
        if resumed and storage_state_path:
            context_kwargs["storage_state"] = storage_state_path
        browser, attached = _connect_browser(
            playwright,
            browser_name=browser_name,
            headless=headless,
            attach_endpoint=attach_endpoint,
        )
        context, page, reused_live_page = _prepare_browser_context(
            browser,
            attached=attached,
            context_kwargs=context_kwargs,
            prefer_existing_page=attached and resume_last_page,
        )
        saved_session = None
        try:
            if attached and reused_live_page and resume_last_page:
                executed.append(f"attach {str(getattr(page, 'url', '') or (session.get('last_url') if session else '') or '')}")
            elif resumed and resume_last_page and session and session.get("last_url"):
                page.goto(str(session.get("last_url") or ""), wait_until="load", timeout=default_timeout_ms)
                executed.append(f"resume {session.get('last_url')}")

            if session_id and (resumed or attached):
                if verify_resumed_state and session:
                    verified, current_snapshot, detail, drift = _resume_preflight(
                        page,
                        session=session,
                        session_id=session_id,
                    )
                    session_verification = detail
                    if current_snapshot.get("screenshot"):
                        screenshots.append(current_snapshot["screenshot"])
                    if not verified:
                        recovered = False
                        recovery_snapshot = current_snapshot
                        recovery_detail = ""
                        if auto_reanchor:
                            recovered, recovery_snapshot, recovery_detail = _attempt_resume_recovery(
                                page,
                                session=session,
                                session_id=session_id,
                                timeout_ms=default_timeout_ms,
                            )
                            if recovery_snapshot.get("screenshot") and recovery_snapshot["screenshot"] not in screenshots:
                                screenshots.append(recovery_snapshot["screenshot"])
                        if recovered:
                            expected_url = str(
                                (session.get("last_snapshot") or {}).get("url")
                                or session.get("last_url")
                                or ""
                            ).strip()
                            if expected_url:
                                executed.append(f"reanchor {expected_url}")
                            session_verification = recovery_detail
                        else:
                            if drift is None:
                                drift = {}
                            drift["recovery_attempted"] = bool(auto_reanchor)
                            if recovery_detail:
                                drift["recovery"] = recovery_detail
                            else:
                                drift["recovery_hint"] = "Reattach on the expected page or enable auto_reanchor to let PHANTOM navigate back before continuing."
                            final_url = recovery_snapshot.get("url", "")
                            title = recovery_snapshot.get("title", "")
                            if session_id and persist_session:
                                saved_session = _persist_browser_session(
                                    context,
                                    session_id=session_id,
                                    browser_name=browser_name,
                                    headless=headless,
                                    final_url=final_url,
                                    title=title,
                                    ok=False,
                                    attach_endpoint=attach_endpoint,
                                    snapshot=recovery_snapshot,
                                )
                            return {
                                "ok": False,
                                "error": f"session verification failed: {detail}",
                                "final_url": final_url,
                                "title": title,
                                "session_id": session_id,
                                "session_resumed": resumed,
                                "session_attached": attached,
                                "session_verification": session_verification,
                                "steps_executed": executed,
                                "extracted": extracted,
                                "screenshots": screenshots,
                                "step_results": step_results,
                                "drift_report": drift,
                                "session_saved": bool(saved_session),
                                "session": saved_session,
                            }
                else:
                    session_verification = "skipped by configuration"
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
                    selector_note = ""
                    if action == "goto":
                        url = str(step.get("url") or "").strip()
                        if not url:
                            raise ValueError("browser goto step requires url")
                        page.goto(url, wait_until=str(step.get("wait_until") or "load"), timeout=timeout_ms)
                        executed.append(f"goto {url}")
                    elif action == "click":
                        selector, reanchored, resolve_detail = _resolve_selector(page, step, timeout_ms=timeout_ms)
                        if not selector:
                            raise ValueError("browser click step requires selector")
                        page.locator(selector).click(timeout=timeout_ms)
                        selector_note = resolve_detail if reanchored else ""
                        executed.append(f"click {selector}" + (" (re-anchored)" if reanchored else ""))
                    elif action == "fill":
                        selector, reanchored, resolve_detail = _resolve_selector(page, step, timeout_ms=timeout_ms)
                        if not selector:
                            raise ValueError("browser fill step requires selector")
                        value = str(step.get("value") or "")
                        page.locator(selector).fill(value, timeout=timeout_ms)
                        selector_note = resolve_detail if reanchored else ""
                        executed.append(f"fill {selector}" + (" (re-anchored)" if reanchored else ""))
                    elif action == "press":
                        key = str(step.get("key") or "").strip()
                        if not key:
                            raise ValueError("browser press step requires key")
                        selector, reanchored, resolve_detail = _resolve_selector(page, step, timeout_ms=timeout_ms)
                        if selector:
                            page.locator(selector).press(key, timeout=timeout_ms)
                            selector_note = resolve_detail if reanchored else ""
                            executed.append(f"press {key} on {selector}" + (" (re-anchored)" if reanchored else ""))
                        else:
                            page.keyboard.press(key)
                            executed.append(f"press {key}")
                    elif action == "wait_for":
                        selector, reanchored, resolve_detail = _resolve_selector(
                            page,
                            step,
                            timeout_ms=timeout_ms,
                            state=str(step.get("state") or "visible"),
                        )
                        url_contains = str(step.get("url_contains") or "").strip()
                        state = str(step.get("state") or "visible")
                        if selector:
                            page.locator(selector).wait_for(state=state, timeout=timeout_ms)
                            selector_note = resolve_detail if reanchored else ""
                            executed.append(f"wait_for {selector}" + (" (re-anchored)" if reanchored else ""))
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
                        selector, reanchored, resolve_detail = _resolve_selector(page, step, timeout_ms=timeout_ms)
                        if not selector:
                            raise ValueError("browser extract_text step requires selector")
                        extracted_text = page.locator(selector).inner_text(timeout=timeout_ms)
                        extracted.append({
                            "selector": selector,
                            "name": str(step.get("name") or selector),
                            "text": extracted_text,
                        })
                        selector_note = resolve_detail if reanchored else ""
                        executed.append(f"extract_text {selector}" + (" (re-anchored)" if reanchored else ""))
                    elif action == "assert_text":
                        selector, reanchored, resolve_detail = _resolve_selector(page, step, timeout_ms=timeout_ms)
                        expected = str(step.get("expected") or "").strip()
                        if not selector or not expected:
                            raise ValueError("browser assert_text step requires selector and expected")
                        text = page.locator(selector).inner_text(timeout=timeout_ms)
                        if expected not in text:
                            raise RuntimeError(
                                f"assert_text failed for {selector}: expected substring {expected!r}"
                            )
                        selector_note = resolve_detail if reanchored else ""
                        executed.append(f"assert_text {selector}" + (" (re-anchored)" if reanchored else ""))
                    elif action == "screenshot":
                        label = str(step.get("name") or step.get("label") or f"step_{index}").strip()
                        path = _session_artifact_path(session_id, index, label) if session_id else _artifact_path(index, label)
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
                        drift = _drift_report(page, index, step, RuntimeError(detail), session_id=session_id)
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
                        if session_id and persist_session:
                            failure_snapshot = _page_snapshot(
                                page,
                                index=index,
                                label="failed_step_state",
                                session_id=session_id,
                                screenshot_path=drift.get("screenshot", ""),
                            )
                            saved_session = _persist_browser_session(
                                context,
                                session_id=session_id,
                                browser_name=browser_name,
                                headless=headless,
                                final_url=final_url,
                                title=title,
                                ok=False,
                                attach_endpoint=attach_endpoint,
                                snapshot=failure_snapshot,
                            )
                        return {
                            "ok": False,
                            "error": detail,
                            "final_url": final_url,
                            "title": title,
                            "session_id": session_id,
                            "session_resumed": resumed,
                            "session_attached": attached,
                            "session_verification": session_verification,
                            "steps_executed": executed,
                            "extracted": extracted,
                            "screenshots": screenshots,
                            "step_results": step_results,
                            "drift_report": drift,
                            "session_saved": bool(saved_session),
                            "session": saved_session,
                        }
                    step_result.update({
                        "ok": True,
                        "verified": bool(verified),
                        "detail": f"{detail}; {selector_note}" if selector_note else detail,
                        "url": str(getattr(page, "url", "") or ""),
                        "title": _safe_page_title(page),
                    })
                    step_results.append(step_result)
                except Exception as exc:
                    drift = _drift_report(page, index, step, exc, session_id=session_id)
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
                    if session_id and persist_session:
                        failure_snapshot = _page_snapshot(
                            page,
                            index=index,
                            label="failed_step_state",
                            session_id=session_id,
                            screenshot_path=drift.get("screenshot", ""),
                        )
                        saved_session = _persist_browser_session(
                            context,
                            session_id=session_id,
                            browser_name=browser_name,
                            headless=headless,
                            final_url=final_url,
                            title=title,
                            ok=False,
                            attach_endpoint=attach_endpoint,
                            snapshot=failure_snapshot,
                        )
                    return {
                        "ok": False,
                        "error": str(exc),
                        "final_url": final_url,
                        "title": title,
                        "session_id": session_id,
                        "session_resumed": resumed,
                        "session_attached": attached,
                        "session_verification": session_verification,
                        "steps_executed": executed,
                        "extracted": extracted,
                        "screenshots": screenshots,
                        "step_results": step_results,
                        "drift_report": drift,
                        "session_saved": bool(saved_session),
                        "session": saved_session,
                    }

            final_url = page.url
            title = page.title()
            final_shot = ""
            if capture_final_screenshot:
                path = _session_artifact_path(session_id, len(steps) + 1, "final_state") if session_id else _artifact_path(len(steps) + 1, "final_state")
                page.screenshot(path=str(path), full_page=True)
                final_shot = str(path)
                screenshots.append(final_shot)
            saved_session = None
            final_snapshot = _page_snapshot(
                page,
                index=len(steps) + 1,
                label="final_state_snapshot",
                session_id=session_id,
                screenshot_path=final_shot,
            )
            if session_id and persist_session:
                saved_session = _persist_browser_session(
                    context,
                    session_id=session_id,
                    browser_name=browser_name,
                    headless=headless,
                    final_url=final_url,
                    title=title,
                    ok=True,
                    attach_endpoint=attach_endpoint,
                    snapshot=final_snapshot,
                )
        finally:
            if not attached:
                browser.close()

    result = {
        "ok": True,
        "final_url": final_url,
        "title": title,
        "session_id": session_id,
        "session_resumed": resumed,
        "session_attached": attached,
        "session_verification": session_verification,
        "steps_executed": executed,
        "extracted": extracted,
        "screenshots": screenshots,
        "step_results": step_results,
        "drift_report": None,
    }
    if session_id and persist_session:
        result["session_saved"] = True
        result["session"] = saved_session
    return result


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
        expected = drift.get("expected_snapshot") or {}
        if expected:
            expected_bits = []
            if expected.get("url"):
                expected_bits.append(f"url={expected['url']}")
            if expected.get("title"):
                expected_bits.append(f"title={expected['title']}")
            if expected_bits:
                drift_text += "\nExpected: " + ", ".join(expected_bits)
        if drift.get("recovery"):
            drift_text += f"\nRecovery attempt: {drift['recovery']}"
        elif drift.get("recovery_hint"):
            drift_text += f"\nRecovery hint: {drift['recovery_hint']}"

    status = "Browser workflow complete." if result.get("ok", True) else f"Browser workflow failed: {result.get('error', '')}"
    session_text = ""
    if result.get("session_id"):
        flags = []
        if result.get("session_resumed"):
            flags.append("resumed")
        if result.get("session_attached"):
            flags.append("attached")
        if result.get("session_saved"):
            flags.append("saved")
        suffix = f" ({', '.join(flags)})" if flags else ""
        session_text = f"\nSession: {result.get('session_id')}{suffix}"
        if result.get("session_verification"):
            session_text += f"\nSession verification: {result.get('session_verification')}"
    return (
        f"{status}\n"
        f"URL: {result.get('final_url', '')}\n"
        f"Title: {result.get('title', '')}"
        f"{session_text}"
        f"{steps_text}{verification_text}{extracted_text}{drift_text}{screenshot_text}"
    ).strip()


def workflow_payload_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=True)
