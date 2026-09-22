"""Controlled, interactive inspection of a real Deportick page."""

import argparse
import hashlib
import json
import os
import queue
import random
import re
import select
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

START_URL = "https://www.deportick.com/"
ARTIFACTS = Path("artifacts")
PROFILE = Path(os.environ.get("DEPORTICK_PROFILE", ".playwright-profile"))
EVENT_KEYWORDS = ("Argentina", "Benin", "Benín")
POLL_INTERVAL = max(15.0, float(os.environ.get("POLL_INTERVAL", "30")))
RELOAD_INTERVAL = max(300.0, float(os.environ.get("RELOAD_INTERVAL", "600")))
HEADLESS = False
OCR_SOURCE = Path(__file__).with_name("ocr_card.swift")
OCR_BINARY = ARTIFACTS / "ocr_card"
OCR_SDK = "/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk"
SAFE_WORDS = {
    "event_card": ("evento", "ver evento", "entradas"),
    "buy_button": ("comprar", "sacar entradas", "adquirir"),
    "continue": ("continuar", "seguir", "seleccionar entradas"),
    "sector": ("localidad", "sector", "tribuna", "platea", "popular"),
    "quantity": ("cantidad",),
}
BLOCKED = re.compile(
    r"pagar|pago|tarjeta|confirmar compra|finalizar compra|realizar compra|"
    r"aceptar compra|checkout|captcha|verificaci[oó]n|cola|fila virtual|"
    r"iniciar sesi[oó]n|login|ingresar|registrarse",
    re.IGNORECASE,
)

INSPECT_JS = """() => {
  const visible = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' &&
      Number(s.opacity) !== 0 && r.width > 0 && r.height > 0;
  };
  const attrs = el => Object.fromEntries([...el.attributes]
    .filter(a => a.name.startsWith('data-')).map(a => [a.name, a.value]));
  const item = el => ({tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 300),
    id: el.id || null, role: el.getAttribute('role'),
    name: el.getAttribute('name'), type: el.getAttribute('type'),
    href: el.getAttribute('href'), src: el.getAttribute('src'), title: el.getAttribute('title'),
    context: (el.parentElement?.innerText || '').trim().slice(0, 500),
    context_event_links: [...(el.parentElement?.querySelectorAll('a[href*="/event/"]') || [])].length,
    ancestors: [el.parentElement, el.parentElement?.parentElement]
      .filter(Boolean).map(parent => ({text: (parent.innerText || '').trim().slice(0, 500),
        event_links: parent.querySelectorAll('a[href*="/event/"]').length,
        headings: [...parent.querySelectorAll('h1,h2,h3,h4,h5,h6')]
          .map(h => (h.innerText || '').trim()).filter(Boolean).slice(0, 5),
        data: attrs(parent)})),
    images: [...el.querySelectorAll('img')].map(img => ({alt: img.alt || '',
      title: img.title || '', aria_label: img.getAttribute('aria-label') || '',
      src: img.getAttribute('src') || '',
      filename: (img.getAttribute('src') || '').split('/').pop().split('?')[0],
      loading: img.loading || '', data: attrs(img)})),
    parent_class: el.parentElement?.className || '',
    parent_data: el.parentElement ? attrs(el.parentElement) : {},
    aria_label: el.getAttribute('aria-label'), data: attrs(el)});
  const get = selector => [...document.querySelectorAll(selector)]
    .filter(visible).map(item);
  return {
    url: location.href, title: document.title,
    body_text: (document.body?.innerText || '').slice(0, 10000),
    verification_text: document.body?.innerText || '',
    labels: get('label,[role=checkbox],[aria-label]'),
    headings: get('h1,h2,h3,h4,h5,h6'),
    buttons: get('button,input[type=button],input[type=submit]'),
    links: get('a[href]'), inputs: get('input,textarea'),
    challenges: get('.g-recaptcha,.h-captcha,.cf-turnstile,#challenge-form,#challenge-stage,[id*=captcha i],[class*=captcha i],[id*=challenge i],[class*=challenge i],[data-testid*=challenge i]'),
    selects: get('select'), iframes: get('iframe'),
    roles: get('[role]'), data_elements: get('*').filter(x => Object.keys(x.data).length),
    layout: {scroll_height: document.documentElement.scrollHeight,
      viewport_height: window.innerHeight,
      carousel_elements: document.querySelectorAll('[class*="carousel"],[class*="swiper"],[class*="slick"]').length}
  };
}"""

HOT_PROBE_JS = """() => {
  if (!window.__snipeHotObserver) {
    window.__snipeHotVersion = 0;
    window.__snipeHotObserver = new MutationObserver(() => { window.__snipeHotVersion++; });
    window.__snipeHotObserver.observe(document.documentElement,
      {subtree:true, childList:true, attributes:true, characterData:true});
  }
  const visible = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0 && r.width > 0 && r.height > 0;
  };
  const item = el => ({tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.getAttribute('aria-label') ||
      (el.matches('input[type=button],input[type=submit]') ? el.value : '') ||
      '').trim().slice(0, 200),
    id: el.id || null, role: el.getAttribute('role'),
    name: el.getAttribute('name'), type: el.getAttribute('type'),
    href: el.getAttribute('href'), src: el.getAttribute('src'),
    title: el.getAttribute('title'),
    aria_label: el.getAttribute('aria-label'),
    context: (el.parentElement?.innerText || '').trim().slice(0, 200),
    in_navigation: !!el.closest('header,nav'),
    data: Object.fromEntries([...el.attributes].filter(a => a.name.startsWith('data-'))
      .map(a => [a.name, a.value]))});
  const get = selector => [...document.querySelectorAll(selector)].filter(visible).map(item);
  return {url: location.href, title: document.title,
    hot_version: window.__snipeHotVersion,
    body_text: (document.body?.innerText || '').slice(0, 5000),
    verification_text: document.body?.innerText || '',
    labels: get('label,[role=checkbox],[aria-label]'),
    headings: get('h1,h2,h3,h4,h5,h6'),
    buttons: get('button,input[type=button],input[type=submit]'),
    links: get('a[href]'), inputs: get('input,textarea'),
    challenges: get('.g-recaptcha,.h-captcha,.cf-turnstile,#challenge-form,#challenge-stage,[id*=captcha i],[class*=captcha i],[id*=challenge i],[class*=challenge i],[data-testid*=challenge i]'),
    selects: get('select'), iframes: get('iframe')};
}"""

HOT_WAIT_JS = """({timeoutMs, version}) => new Promise(resolve => {
  let observer, timer;
  const finish = changed => { observer?.disconnect(); clearTimeout(timer); resolve(changed); };
  if (window.__snipeHotVersion !== version) { resolve(true); return; }
  observer = new MutationObserver(() => finish(true));
  observer.observe(document.documentElement, {subtree:true, childList:true,
    attributes:true, characterData:true});
  if (window.__snipeHotVersion !== version) { finish(true); return; }
  timer = setTimeout(() => finish(false), timeoutMs);
})"""

VISUAL_WAIT_JS = """({known, timeoutMs}) => new Promise(resolve => {
  const signatures = () => [...document.querySelectorAll('a[href*="/event/"]')]
    .map(a => a.getAttribute('href') + '|' + [...a.querySelectorAll('img')]
      .map(img => img.getAttribute('src') || '').join('|'));
  const changed = () => signatures().some(value => !known.includes(value));
  if (changed()) { resolve(true); return; }
  let observer, timer;
  const finish = value => { observer.disconnect(); clearTimeout(timer); resolve(value); };
  observer = new MutationObserver(() => { if (changed()) finish(true); });
  observer.observe(document.documentElement,
    {subtree:true, childList:true, attributes:true, attributeFilter:['href','src','srcset']});
  if (changed()) { finish(true); return; }
  timer = setTimeout(() => finish(false), timeoutMs);
})"""


def inspect(page, step):
    folder = ARTIFACTS / f"step_{step:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    data = page.evaluate(INSPECT_JS)
    page.screenshot(path=str(folder / "screenshot.png"), full_page=True)
    (folder / "page.html").write_text(page.content(), encoding="utf-8")
    (folder / "elements.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for key, value in data.items():
        print(f"\n{key.upper()}: {json.dumps(value, ensure_ascii=False, indent=2)}")
    print(f"\nGuardado: {folder}")
    return data


def unique_locator(page, element):
    """Return a selector only after it uniquely matches the observed element."""
    tag = element["tag"]
    candidates = []
    if element["id"]:
        candidates.append(page.locator(f"{tag}[id={json.dumps(element['id'])}]"))
    for name in ("data-testid", "data-test", "data-qa"):
        if element["data"].get(name):
            candidates.append(page.locator(
                f"{tag}[{name}={json.dumps(element['data'][name])}]"
            ))
    if element["role"] and element["text"]:
        candidates.append(page.get_by_role(element["role"], name=element["text"], exact=True))
    if tag in ("button", "a") and element["text"]:
        candidates.append(page.get_by_role(
            "button" if tag == "button" else "link",
            name=element["text"], exact=True,
        ))
    if tag == "a" and element["href"]:
        candidates.append(page.locator(f"a[href={json.dumps(element['href'])}]"))
    for locator in candidates:
        if locator.count() == 1 and locator.is_visible():
            return locator
    return None


def detect(page, data):
    options = []
    for element in data["links"] + data["buttons"] + data["roles"]:
        label = element["text"] or element["aria_label"] or ""
        if BLOCKED.search(label):
            continue
        category = next((key for key, words in SAFE_WORDS.items()
                         if any(word in label.casefold() for word in words)), None)
        if not category:
            continue
        locator = unique_locator(page, element)
        if locator and not any(x["locator"] == str(locator) for x in options):
            options.append({"category": category, "element": element,
                            "locator": str(locator), "handle": locator})
    return options


def record(option):
    path = ARTIFACTS / "discovered_selectors.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    entry = {"locator": option["locator"], "text": option["element"]["text"],
             "element": option["element"]}
    entries = saved.setdefault(option["category"], [])
    if entry not in entries:
        entries.append(entry)
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")


def load_discovered_selectors(path=None):
    """Read selectors for future bot modes; callers must validate before use."""
    if path is None:
        path = ARTIFACTS / "discovered_selectors.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def act(page, data):
    options = detect(page, data)
    if not options:
        print("No se detectó una acción segura con selector único.")
        return
    for number, option in enumerate(options, 1):
        print(f"{number}. [{option['category']}] {option['element']['text']!r} — {option['locator']}")
    choice = input("Número de acción (ENTER cancela): ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(options):
        return
    option = options[int(choice) - 1]
    print(f"CURRENT STATE: {data['title']}\nDETECTED ACTION: {option['category']}\n"
          f"LOCATOR: {option['locator']}\nTEXT: {option['element']['text']}\nURL: {page.url}")
    if option["category"] in ("sector", "quantity"):
        print("Esta selección requiere autorización explícita para este elemento.")
    if input("Escribí CONFIRMAR para ejecutar esta acción: ").strip() != "CONFIRMAR":
        return
    # Recheck the page and locator immediately before the click.
    if page.url != data["url"] or option["handle"].count() != 1 or not option["handle"].is_visible():
        print("La página o el selector cambió. Inspeccioná nuevamente.")
        return
    option["handle"].click(timeout=5000)
    record(option)
    print("Acción ejecutada y selector validado. Inspeccioná el nuevo estado.")


def explore():
    ARTIFACTS.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE), headless=False, accept_downloads=False
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")
        step = 1
        data = inspect(page, step)
        try:
            while True:
                command = input("\nENTER=inspeccionar | a=acción | s=guardar | q=salir: ").strip().lower()
                if command == "q":
                    break
                if command == "a":
                    act(page, data)
                elif command in ("", "s"):
                    step += 1
                    data = inspect(page, step)
        finally:
            context.close()


def normalized(value):
    plain = "".join(ch for ch in unicodedata.normalize("NFD", value.casefold().strip())
                    if unicodedata.category(ch) != "Mn")
    return " ".join(re.sub(r"[^\w]+", " ", plain).split())


def edit_distance_at_most_one(left, right):
    if abs(len(left) - len(right)) > 1:
        return False
    if left == right:
        return True
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    shorter, longer = sorted((left, right), key=len)
    return any(longer[:index] + longer[index + 1:] == shorter for index in range(len(longer)))


def visual_card_match(ocr_text, card_evidence=""):
    """Require both full names in one card's OCR; metadata may only support a fuzzy read."""
    words = normalized(ocr_text).split()
    evidence = normalized(card_evidence)
    argentina = any(word == "argentina" or
                    (len(word) >= 8 and edit_distance_at_most_one(word, "argentina"))
                    for word in words)
    benin_exact = "benin" in words
    benin_fuzzy = any(len(word) == 5 and word.startswith("be") and
                      edit_distance_at_most_one(word, "benin") for word in words)
    benin = benin_exact or (benin_fuzzy and (argentina or "benin" in evidence.split()))
    conflicting_rival = any(word in words for word in ("bolivia", "brasil", "burkina"))
    return {"match_argentina": argentina, "match_benin": benin,
            "accepted": argentina and benin and not conflicting_rival}


def ensure_ocr_binary():
    if OCR_BINARY.exists() and OCR_BINARY.stat().st_mtime >= OCR_SOURCE.stat().st_mtime:
        return OCR_BINARY
    ARTIFACTS.mkdir(exist_ok=True)
    command = ["swiftc", "-module-cache-path", "/private/tmp/snipebot-swift-cache"]
    if Path(OCR_SDK).exists():
        command += ["-sdk", OCR_SDK]
    command += [str(OCR_SOURCE), "-o", str(OCR_BINARY)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"No se pudo compilar OCR local: {result.stderr.strip()}")
    return OCR_BINARY


def ocr_card(png_bytes):
    result = subprocess.run([str(ensure_ocr_binary())], input=png_bytes,
                            capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"OCR local falló: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def visual_scan_cards(page, cache, save_cards=False, stop_on_match=True):
    """OCR each changed, visible event card; return the first verified card locator."""
    anchors = page.locator('a[href*="/event/"]')
    reports = []
    signatures = []
    for index in range(anchors.count()):
        anchor = anchors.nth(index)
        info = anchor.evaluate("""a => ({href:a.getAttribute('href'),
          text:(a.innerText || '').trim(), title:a.title || '',
          aria_label:a.getAttribute('aria-label') || '',
          images:[...a.querySelectorAll('img')].map(img => ({
            src:img.getAttribute('src') || '', alt:img.alt || '', title:img.title || '',
            aria_label:img.getAttribute('aria-label') || ''}))})""")
        href = info.get("href") or ""
        sources = [image["src"] for image in info["images"]]
        signatures.append(href + "|" + "|".join(sources))
        if not href or not sources:
            continue
        box = anchor.bounding_box()
        if not box or box["width"] < 20 or box["height"] < 20:
            continue
        # Reject whole-page wrappers; screenshots must stay limited to a card.
        if box["width"] > 1800 or box["height"] > 1400:
            continue
        signature = hashlib.sha256(json.dumps(
            [href, sources, round(box["width"]), round(box["height"])],
            ensure_ascii=False).encode()).hexdigest()
        report = cache.get(signature)
        if report is None:
            anchor.scroll_into_view_if_needed()
            for image in anchor.locator("img").all():
                if image.is_visible():
                    image.evaluate("img => img.decode().catch(() => {})")
            png = anchor.screenshot()
            ocr_text = ocr_card(png)
            evidence = " ".join([href, info["text"], info["title"], info["aria_label"]] +
                                [" ".join(image.values()) for image in info["images"]])
            matches = visual_card_match(ocr_text, evidence)
            report = {"href": urljoin(page.url, href), "image_src": sources,
                      "ocr_text": ocr_text, **matches, "signature": signature}
            cache[signature] = report
            if save_cards:
                folder = ARTIFACTS / "visual_scan"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"card_{index + 1:02d}.png").write_bytes(png)
        report = {**report, "selector": str(anchor),
                  "element": {"tag": "a", "text": info["text"], "id": None,
                              "role": None, "href": href, "data": {}}}
        reports.append(report)
        if report["accepted"] and stop_on_match:
            return reports, anchor, signatures
    return reports, None, signatures


def enter_verified_card(page, anchor, report, deadline):
    """Click the OCR-confirmed HOME anchor before writing files or normal logs."""
    detected_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    target_url = report["href"]
    try:
        anchor.click(timeout=5000, no_wait_after=True)
    except PlaywrightError as exc:
        try:
            challenge = captcha_signal(page.evaluate(HOT_PROBE_JS))
        except PlaywrightError:
            challenge = None
        if challenge:
            return hot_path_watch(page, deadline, target_url)
        save_validated_event(target_url,
                             {"source": "visual_home_card", "image_src": report["image_src"],
                              "ocr_text": report["ocr_text"]}, report["ocr_text"])
        log_watch(f"TARGET CARD CLICK ERROR | URL={target_url} | {exc}")
        snapshot(page, "visual_click_error_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
        alert("CARD TARGET CONFIRMADO - CLICK FALLÓ - REVISAR MANUALMENTE")
        keep_open()
    clicked_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    save_validated_event(target_url,
                         {"source": "visual_home_card", "image_src": report["image_src"],
                          "ocr_text": report["ocr_text"]}, report["ocr_text"])
    if report.get("selector") and report.get("element"):
        record({"category": "event_card", "locator": report["selector"],
                "element": report["element"]})
    log_watch_precise(f"TARGET CARD VERIFIED | timestamp={detected_at} | "
                      f"text_ocr={report['ocr_text']!r} | href={target_url}")
    log_watch_precise(f"TARGET EVENT VERIFIED | source=visual_home_card | URL={target_url}")
    log_watch_precise(f"AUTO NAVIGATION | selector={report.get('selector', 'card visual')} | "
                      f"clicked_at={clicked_at} | URL={page.url}")
    return hot_path_watch(page, deadline, target_url)


def event_matches(value, keywords=EVENT_KEYWORDS):
    required = {normalized(word) for word in keywords}
    candidate = normalized(value)
    return all(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", candidate) for word in required)


def candidate_evidence(element):
    image_text = " ".join(" ".join(str(image.get(key, "")) for key in
                                   ("alt", "title", "aria_label", "src", "filename", "data"))
                          for image in element.get("images", []))
    ancestor_text = " ".join(str(ancestor.get(key, "")) for ancestor in element.get("ancestors", [])
                             if ancestor.get("event_links", 0) <= 1
                             for key in ("text", "headings", "data"))
    return " ".join((element.get("text", ""), element.get("context", ""),
                     element.get("href", ""), element.get("aria_label", "") or "",
                     str(element.get("data", {})), str(element.get("parent_data", {})),
                     ancestor_text, image_text))


def home_target_verified(element):
    """Require both complete country names inside one observed event card."""
    if element.get("context_event_links", 1) > 1:
        return False
    return event_matches(candidate_evidence(element), ("Argentina", "Benin"))


def argentina_candidate(element):
    evidence = normalized(candidate_evidence(element))
    return "argentina" in evidence or bool(re.search(r"\barg\b", evidence))


def candidate_links(data, page_url):
    """Read only event links whose own card has an Argentina signal."""
    result = []
    seen = set()
    for element in data.get("links", []):
        href = element.get("href")
        if not href or "/event/" not in href or element.get("context_event_links", 1) > 1:
            continue
        absolute = urljoin(page_url, href)
        host = urlparse(absolute).hostname or ""
        if host != "deportick.com" and not host.endswith(".deportick.com"):
            continue
        if argentina_candidate(element) and absolute not in seen:
            result.append((absolute, element))
            seen.add(absolute)
    return result


def verify_event_identity(data):
    """Confirm both teams within one title, heading, or short visible text block."""
    blocks = [data.get("title", "")] + [x.get("text", "") for x in data.get("headings", [])]
    lines = [line.strip() for line in data.get("body_text", "").splitlines() if line.strip()]
    blocks += lines
    if any(event_matches(block, ("Argentina", "Benin")) for block in blocks):
        return "TARGET"
    if any({normalized(lines[index]), normalized(lines[index + 1])} == {"argentina", "benin"}
           for index in range(len(lines) - 1)):
        return "TARGET"
    if any(re.search(r"\bargentina\s+(?:vs|v|contra)\s+\w+", normalized(block))
           or re.search(r"\b\w+\s+(?:vs|v|contra)\s+argentina\b", normalized(block))
           for block in blocks):
        return "REJECTED"
    return "UNKNOWN"


def home_candidates(data, keywords):
    candidates = []
    for element in data.get("links", []):
        href = element.get("href")
        if not href or "/event/" not in href:
            continue
        evidence = candidate_evidence(element)
        candidates.append({
            "text": element.get("text") or element.get("context") or "",
            "href": href,
            "absolute_url": urljoin(data.get("url", START_URL), href),
            "slug": urlparse(urljoin(data.get("url", START_URL), href)).path.rstrip("/").split("/")[-1],
            "title": element.get("title"),
            "aria_label": element.get("aria_label"),
            "data": element.get("data", {}),
            "ancestors": element.get("ancestors", []),
            "selector": f"a[href={json.dumps(href)}]",
            "match_first": event_matches(evidence, (keywords[0],)),
            "match_second": event_matches(evidence, (keywords[1],)) if len(keywords) > 1 else None,
            "images": element.get("images", []),
            "context_event_links": element.get("context_event_links", 1),
            "parent_class": element.get("parent_class", ""),
        })
    return candidates


def queue_signal(data):
    url = data["url"].casefold()
    text = normalized(data.get("body_text", ""))
    title = normalized(data.get("title", ""))
    frames = " ".join(normalized(x.get("title", "") or "") + " " +
                      normalized(x.get("src", "") or "") for x in data.get("iframes", []))
    for word in ("queue", "waitingroom", "waiting-room", "fila-virtual"):
        if word in url:
            return f"URL contiene {word!r}"
        if word in frames:
            return f"iframe contiene {word!r}"
    for word in ("fila virtual", "sala de espera", "personas delante", "en la cola",
                 "waiting room", "en espera de tu turno"):
        if word in title:
            return f"title contiene {word!r}"
        if word in frames:
            return f"iframe contiene {word!r}"
        if word in text:
            return f"texto visible contiene {word!r}"
    return None


def captcha_signal(data):
    """Read visible challenge signals only; never interact with provider widgets."""
    if data.get("challenges"):
        return "elemento de challenge visible"
    for frame in data.get("iframes", []):
        evidence = normalized(" ".join(str(frame.get(key) or "")
                                       for key in ("src", "title", "name", "id")))
        if any(word in evidence for word in ("recaptcha", "hcaptcha", "turnstile",
                                             "challenges cloudflare com", "captcha")):
            return "iframe de CAPTCHA visible"
    parts = [data.get("verification_text", data.get("body_text", "")), data.get("title", "")]
    for key in ("labels", "inputs", "buttons", "roles"):
        for element in data.get(key, []):
            parts.extend(str(element.get(attr) or "") for attr in
                         ("text", "aria_label", "title", "id", "name", "context"))
    text = normalized(" ".join(parts))
    if any(word in text for word in ("captcha", "verificacion", "no soy un robot",
                                    "verifica que eres humano", "verifica que no eres un robot",
                                    "verify you are human", "checking your browser", "verification",
                                    "challenge", "i m not a robot", "i am not a robot")):
        return "verificación humana visible"
    return None


def classify_with_signal(data, event_url=None):
    url = data["url"].casefold()
    challenge = captcha_signal(data)
    if challenge:
        return "CAPTCHA_REQUIRED", challenge
    text = normalized(data.get("body_text", ""))
    if (any(x.get("type") == "password" for x in data.get("inputs", [])) or
            "sesion expirada" in text or "sesion vencida" in text):
        return "LOGIN_REQUIRED", "login o sesión expirada visible"
    problem = detect_problem(data)
    if problem:
        return "ERROR", problem
    labels = " ".join(normalized(x.get("text", "")) for key in ("headings", "buttons", "selects", "inputs")
                      for x in data.get(key, []))
    has_selection = any(word in labels for word in ("seleccion de entradas", "seleccionar entradas",
                                                 "selecciona tus entradas", "localidad", "sector", "cantidad"))
    if has_selection and any(word in labels for word in ("localidad", "sector", "cantidad")):
        return "TICKET_SELECTION", "controles visibles de selección de entradas"
    signal = queue_signal(data)
    if signal:
        return "QUEUE", signal
    if event_url and data["url"].rstrip("/") == event_url.rstrip("/"):
        if buy_candidate(data):
            return "EVENT_AVAILABLE", "URL objetivo y acción Comprar visible"
        return "SALE_NOT_STARTED", "URL objetivo sin acción Comprar visible"
    if "/event/" in url:
        if buy_candidate(data):
            return "EVENT_AVAILABLE", "URL de evento y acción Comprar visible"
        return "SALE_NOT_STARTED", "URL de evento sin acción Comprar visible"
    if url.rstrip("/") == START_URL.rstrip("/") or "/page/" in url:
        return "HOME", "URL de portada o listado"
    return "UNKNOWN", "ninguna señal conocida"


def detect_problem(data):
    text = normalized(data.get("body_text", ""))
    title = normalized(data.get("title", ""))
    if any(word in title or word in text for word in ("error 403", "error 429", "access denied",
                                                     "too many requests", "sin conexion", "no hay internet")):
        return "error de acceso o conexión"
    if captcha_signal(data):
        return "CAPTCHA/verificación visible"
    if any(x.get("type") == "password" for x in data.get("inputs", [])):
        return "formulario de login visible"
    if "sesion expirada" in text or "sesion vencida" in text:
        return "sesión expirada"
    return None


def session_status(data):
    if any(x.get("type") == "password" for x in data.get("inputs", [])):
        return "LOGIN_REQUIRED"
    if any("ingresar registrarse" in normalized(x.get("text", ""))
           for x in data.get("links", [])):
        return "LOGIN_REQUIRED"
    return "NO_LOGIN_PROMPT_VISIBLE"


def event_identity_matches(data, keywords=("Argentina", "Benin")):
    blocks = [data.get("title", "")] + [x.get("text", "") for x in data.get("headings", [])]
    return any(event_matches(block, keywords) for block in blocks)


def classify(data, event_url=None):
    return classify_with_signal(data, event_url)[0]


def parse_keywords(value):
    words = tuple(word.strip() for word in value.split(",") if word.strip())
    if not words:
        raise ValueError("EVENT_KEYWORDS/--keywords debe incluir al menos un término")
    return words


def resolve_target(cli_url=None, cli_keywords=None, environ=None):
    env = os.environ if environ is None else environ
    if cli_url:
        return cli_url.strip(), EVENT_KEYWORDS, "--url"
    if cli_keywords is not None:
        return None, parse_keywords(cli_keywords), "--keywords"
    if env.get("EVENT_URL"):
        return env["EVENT_URL"].strip(), EVENT_KEYWORDS, "EVENT_URL"
    if env.get("EVENT_KEYWORDS"):
        return None, parse_keywords(env["EVENT_KEYWORDS"]), "EVENT_KEYWORDS"
    return None, EVENT_KEYWORDS, "default"


def buy_candidate(data):
    return any(re.search(r"\b(comprar|sacar entradas|adquirir)\b", x.get("text", ""), re.IGNORECASE)
               and not BLOCKED.search(x.get("text", ""))
               for key in ("buttons", "links") for x in data.get(key, []))


def log_watch(message):
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    ARTIFACTS.mkdir(exist_ok=True)
    with (ARTIFACTS / "watch.log").open("a", encoding="utf-8") as output:
        output.write(line + "\n")


def log_watch_precise(message):
    stamp = datetime.now().astimezone().isoformat(timespec="microseconds")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    ARTIFACTS.mkdir(exist_ok=True)
    with (ARTIFACTS / "watch.log").open("a", encoding="utf-8") as output:
        output.write(line + "\n")


def alert(message, repeat=3):
    print(f"\n\a{message}\a\n", flush=True)
    for _ in range(repeat):
        try:
            subprocess.run(["afplay", "/System/Library/Sounds/Sosumi.aiff"],
                           check=False, timeout=4, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        print("\a", end="", flush=True)
        time.sleep(0.6)


def snapshot(page, name):
    folder = ARTIFACTS / name
    folder.mkdir(parents=True, exist_ok=True)
    data = page.evaluate(INSPECT_JS)
    (folder / "elements.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    page.screenshot(path=str(folder / "screenshot.png"), full_page=True)
    for attempt in range(4):
        try:
            (folder / "page.html").write_text(page.content(), encoding="utf-8")
            break
        except PlaywrightError:
            if attempt == 3:
                raise
            page.wait_for_timeout(500)
    return data


def saved_locator(page, category):
    for entry in load_discovered_selectors().get(category, []):
        element = entry.get("element")
        locator = unique_locator(page, element) if element else None
        if locator:
            return locator
    return None


def event_link(page, data, event_url=None, keywords=EVENT_KEYWORDS):
    if event_url:
        for element in data.get("links", []):
            if element.get("href") and urljoin(page.url, element["href"]) == event_url:
                return unique_locator(page, element)
        return None
    saved = saved_locator(page, "event_card")
    if saved and event_matches(saved.inner_text(), keywords):
        return saved
    matches = [element for element in data.get("links", [])
               if element.get("href") and "/event/" in element["href"]
               and element.get("context_event_links", 1) <= 1
               and event_matches(candidate_evidence(element), keywords)]
    return unique_locator(page, matches[0]) if len(matches) == 1 else None


def buy_locator(page, data):
    saved = saved_locator(page, "buy_button")
    if saved and not BLOCKED.search(saved.inner_text()):
        return saved
    matches = [element for key in ("buttons", "links") for element in data.get(key, [])
               if re.search(r"\b(comprar|sacar entradas|adquirir)\b", element.get("text", ""), re.IGNORECASE)
               and not BLOCKED.search(element.get("text", ""))]
    return unique_locator(page, matches[0]) if len(matches) == 1 else None


def save_home_diagnostics(data, keywords):
    ARTIFACTS.mkdir(exist_ok=True)
    report = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
              "url": data["url"], "keywords": list(keywords),
              "candidates": home_candidates(data, keywords),
              "iframes": data.get("iframes", []), "layout": data.get("layout", {})}
    (ARTIFACTS / "home_candidates.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def keep_open():
    print("El navegador queda abierto. Escribí q + ENTER o pulsá Ctrl+C para terminar.", flush=True)
    while True:
        if read_watch_command() == "q":
            raise SystemExit(0)
        time.sleep(1)


def parse_sale_time(value):
    if not value:
        raise ValueError("SALE_TIME es obligatorio en --presale (ejemplo: 2026-09-22T18:00:00-03:00)")
    sale_time = datetime.fromisoformat(value)
    if sale_time.tzinfo is None or sale_time.utcoffset() is None:
        raise ValueError("SALE_TIME debe incluir el offset de zona horaria")
    return sale_time


def validated_event_url():
    path = ARTIFACTS / "target_event.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return (data.get("url") if data.get("verified") is True and
            data.get("keywords") == ["Argentina", "Benin"] else None)


def save_validated_event(url, evidence=None, verified_text=""):
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "target_event.json").write_text(
        json.dumps({"url": url, "keywords": ["Argentina", "Benin"], "verified": True,
                    "verified_at": datetime.now().astimezone().isoformat(timespec="microseconds"),
                    "verified_text": verified_text,
                    "evidence": evidence or {}}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def selection_risk(data):
    labels = normalized(" ".join(x.get("text", "") for key in ("headings", "buttons", "selects", "inputs")
                                 for x in data.get(key, [])))
    return bool(data.get("selects")) or any(word in labels for word in (
        "localidad", "sector", "cantidad", "checkout", "finalizar compra", "datos del asistente"
    ))


def wait_for_dom_change(page, previous_text, seconds):
    """Wait for a real page mutation, with a bounded timeout and no network requests."""
    try:
        page.wait_for_function(
            "previous => (document.body?.innerText || '').slice(0, 10000) !== previous",
            arg=previous_text, timeout=max(1000, int(seconds * 1000)), polling=1000,
        )
    except PlaywrightError:
        pass


def verify_candidate(context, url, element=None, debug_watch=False):
    """Open a public event page in a temporary tab and inspect it without clicking."""
    log_watch(f"EVENT VERIFYING | URL={url}")
    temporary = context.new_page()
    try:
        temporary.goto(url, wait_until="domcontentloaded", timeout=30000)
        data = temporary.evaluate(INSPECT_JS)
        queue_before_identity = queue_signal(data)
        if queue_before_identity:
            attributes = ({"href": element.get("href"), "text": element.get("text"),
                           "context": element.get("context"), "aria_label": element.get("aria_label"),
                           "title": element.get("title"), "data": element.get("data"),
                           "parent_data": element.get("parent_data"), "ancestors": element.get("ancestors"),
                           "images": element.get("images")}
                          if element else {"href": url, "source": "cached candidate"})
            log_watch("QUEUE ENCOUNTERED BEFORE EVENT VERIFICATION | "
                      f"candidate_href={element.get('href') if element else url} | "
                      f"candidate_attributes={json.dumps(attributes, ensure_ascii=False)} | "
                      f"URL={temporary.url} | señal={queue_before_identity}")
            evidence_saved = False
            try:
                snapshot(temporary, "queue_unverified_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
                evidence_saved = True
            except PlaywrightError as exc:
                log_watch(f"QUEUE_UNVERIFIED evidence capture failed | {exc}")
            if debug_watch and evidence_saved:
                temporary.close()
                return "QUEUE_UNVERIFIED", None
            return "QUEUE_UNVERIFIED", temporary
        if detect_problem(data):
            log_watch(f"EVENT VERIFYING ABORTED | {detect_problem(data)} | URL={temporary.url}")
            return "PROBLEM", temporary
        identity = verify_event_identity(data)
        if identity == "UNKNOWN":
            wait_for_dom_change(temporary, data.get("body_text", ""), 5)
            data = temporary.evaluate(INSPECT_JS)
            identity = verify_event_identity(data)
        if identity == "TARGET":
            verified_blocks = [data.get("title", "")] + [heading.get("text", "")
                                                       for heading in data.get("headings", [])]
            verified_blocks += data.get("body_text", "").splitlines()
            verified_text = next((block.strip() for block in verified_blocks
                                  if event_matches(block, ("Argentina", "Benin"))), "")
            if not verified_text:
                verified_text = "Argentina / Benin (líneas visibles consecutivas)"
            save_validated_event(temporary.url,
                                 {"source": "event_page", "candidate_href": url,
                                  "title": data.get("title", ""),
                                  "headings": [x.get("text", "") for x in data.get("headings", [])]},
                                 verified_text)
            log_watch_precise(f"TARGET EVENT VERIFIED | URL={temporary.url} | "
                              f"title={data.get('title', '')!r}")
            return "TARGET", temporary
        if identity == "REJECTED":
            log_watch(f"EVENT REJECTED | URL={temporary.url} | "
                      f"title={data.get('title', '')!r} | "
                      f"headings={[x.get('text') for x in data.get('headings', [])]}")
            temporary.close()
            return "REJECTED", None
        log_watch(f"EVENT VERIFYING INCONCLUSIVE | URL={temporary.url}")
        temporary.close()
        return "UNKNOWN", None
    except PlaywrightError as exc:
        log_watch(f"EVENT VERIFYING ERROR | URL={url} | {exc}")
        if not temporary.is_closed():
            temporary.close()
        return "UNKNOWN", None


def should_stop_automation(state):
    return state in ("CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "ERROR", "TICKET_SELECTION", "UNKNOWN")


def can_discover_more(queue_mode):
    return queue_mode == "NONE"


class HotLogger:
    """Keep disk and terminal I/O off the detection-to-click path."""

    def __init__(self):
        ARTIFACTS.mkdir(exist_ok=True)
        self.events = queue.SimpleQueue()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _run(self):
        with (ARTIFACTS / "watch.log").open("a", encoding="utf-8") as output:
            while True:
                item = self.events.get()
                if item is None:
                    break
                output.write(item + "\n")
                output.flush()
                print(item, flush=True)

    def emit(self, message, timestamp=None):
        stamp = timestamp or datetime.now().astimezone().isoformat(timespec="microseconds")
        self.events.put(f"[{stamp}] {message}")

    def close(self):
        self.events.put(None)
        self.worker.join(timeout=2)


def hot_action_locator(page, data):
    if selection_risk(data) or detect_problem(data):
        return None
    matches = []
    for element in data.get("buttons", []) + data.get("links", []):
        label = normalized(element.get("text", ""))
        if label not in ("comprar", "comprar entradas", "ingresar", "entradas", "continuar"):
            continue
        if element.get("in_navigation"):
            continue
        locator = unique_locator(page, element)
        if locator:
            matches.append((locator, label))
    return matches[0] if len(matches) == 1 else None


class CaptchaAlarm:
    """A cancellable sound worker; it has no access to the browser."""

    def __init__(self):
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _run(self):
        while not self.stopped.is_set():
            print("\a", end="", flush=True)
            try:
                sound = subprocess.Popen(["afplay", "/System/Library/Sounds/Sosumi.aiff"],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                sound = None
            if self.stopped.wait(1):
                if sound and sound.poll() is None:
                    sound.terminate()
                return
            if sound and sound.poll() is None:
                sound.terminate()

    def stop(self):
        self.stopped.set()


class CaptchaGuard:
    def __init__(self, logger):
        self.logger = logger
        self.alarm = None
        self.awaiting_safe_state = False

    def observe(self, page, state):
        if state == "CAPTCHA_REQUIRED":
            self.awaiting_safe_state = True
            if self.alarm is None:
                self.logger.emit(f"CAPTCHA_DETECTED | state=CAPTCHA_REQUIRED | URL={page.url}")
                print("\n========================================\n"
                      "CAPTCHA DETECTADO - RESOLVER AHORA\n"
                      "========================================", flush=True)
                self.alarm = CaptchaAlarm()
                try:
                    page.bring_to_front()
                except PlaywrightError as exc:
                    self.logger.emit(f"CAPTCHA focus unavailable | {exc}")
            return
        if self.alarm is not None:
            self.close()
            self.logger.emit(f"CAPTCHA_CLEARED | CAPTCHA CLEARED | URL={page.url}")

    def close(self):
        if self.alarm is not None:
            self.alarm.stop()
            self.alarm = None


def probe_page(page):
    """Read visible documents, including Queue-it's embedded verification UI."""
    data = page.evaluate(HOT_PROBE_JS)
    if captcha_signal(data):
        return data
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        current = frame
        visible = True
        while current.parent_frame is not None:
            element = current.frame_element()
            try:
                if not element.is_visible():
                    visible = False
                    break
            finally:
                element.dispose()
            current = current.parent_frame
        if visible:
            child = frame.evaluate(HOT_PROBE_JS)
            if captcha_signal(child):
                data = dict(data, challenges=[{"text": "challenge en iframe visible"}])
                break
    return data


def wait_short(page, data):
    try:
        page.evaluate(HOT_WAIT_JS, {"timeoutMs": 100, "version": data.get("hot_version")})
    except PlaywrightError as exc:
        if page.is_closed() or not transient_navigation_error(exc):
            raise


def wait_after_action(page, event_url, guard, timeout=5):
    """One event-driven race for all terminal states; CAPTCHA always wins a probe."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            data = probe_page(page)
            state, _ = classify_with_signal(data, event_url)
            guard.observe(page, state)
            if state in ("CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "ERROR", "QUEUE", "TICKET_SELECTION"):
                return data
            if time.monotonic() >= deadline:
                return data
            wait_short(page, data)
        except PlaywrightError as exc:
            if page.is_closed() or not transient_navigation_error(exc):
                raise
            time.sleep(0.01)


def hot_step(page, target_url, logger, clicked_signatures, captcha_guard=None):
    """Probe only the locked target; click before any disk write or evidence capture."""
    data = probe_page(page)
    state, signal = classify_with_signal(data, target_url)
    if captcha_guard is not None:
        captcha_guard.observe(page, state)
    if state in ("CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "ERROR", "QUEUE", "TICKET_SELECTION", "UNKNOWN"):
        return state, signal, data.get("hot_version")
    if data["url"].rstrip("/") != target_url.rstrip("/"):
        return "UNKNOWN", "navegación fuera del target confirmado", data.get("hot_version")
    if verify_event_identity(data) == "REJECTED":
        return "UNKNOWN", "la URL confirmada muestra otro partido", data.get("hot_version")
    detected_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    detected_ns = time.perf_counter_ns()
    action = hot_action_locator(page, data)
    if action:
        locator, label = action
        signature = (data["url"], str(locator))
        if signature not in clicked_signatures:
            click_ns = time.perf_counter_ns()
            click_at = datetime.now().astimezone().isoformat(timespec="microseconds")
            try:
                locator.click(timeout=5000, no_wait_after=True)
            except PlaywrightError:
                after_click = page.evaluate(HOT_PROBE_JS)
                if not captcha_signal(after_click):
                    raise
                if captcha_guard is not None:
                    captcha_guard.observe(page, "CAPTCHA_REQUIRED")
                clicked_signatures.add(signature)
                return "CAPTCHA_REQUIRED", captcha_signal(after_click), after_click.get("hot_version")
            clicked_signatures.add(signature)
            if captcha_guard is not None:
                after_click = wait_after_action(page, target_url, captcha_guard)
                state, signal = classify_with_signal(after_click, target_url)
                data = after_click
            reaction_ms = (click_ns - detected_ns) / 1_000_000
            logger.emit(f"BUY ACTION APPEARED | label={label} | selector={locator} | URL={data['url']}",
                        detected_at)
            logger.emit(f"BUY ACTION CLICKED | label={label} | selector={locator} | "
                        f"reaction_ms={reaction_ms:.3f} | URL={data['url']}", click_at)
    return state, signal, data.get("hot_version")


def transient_navigation_error(exc):
    message = str(exc).casefold()
    return any(fragment in message for fragment in (
        "execution context was destroyed", "cannot find context with specified id",
        "most likely because of a navigation", "page is navigating", "frame was detached",
        "frame has been detached"
    ))


def hot_path_watch(page, deadline, target_url):
    logger = HotLogger()
    logger.emit(f"HOT PATH ENTERED | URL={target_url} | QUEUE_MODE=TARGET_VERIFIED")
    captcha_guard = CaptchaGuard(logger)
    previous_state = None
    clicked_signatures = set()
    awake_process = None
    queue_started = None
    try:
        while True:
            command = read_watch_command()
            if command == "q":
                logger.emit("HOT PATH stopped by q")
                return
            if command == "r":
                print("Reload deshabilitado en HOT PATH.", flush=True)
            if command == "i":
                print(f"HOT PATH | target={target_url} | state={previous_state}", flush=True)
            try:
                state, signal, hot_version = hot_step(page, target_url, logger, clicked_signatures, captcha_guard)
            except PlaywrightError as exc:
                if not page.is_closed() and transient_navigation_error(exc):
                    continue
                if captcha_guard.alarm is not None:
                    time.sleep(0.1)
                    continue
                logger.emit(f"HOT PATH ERROR | {exc}")
                try:
                    snapshot(page, "hot_error_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
                except PlaywrightError as evidence_error:
                    logger.emit(f"HOT PATH ERROR EVIDENCE FAILED | {evidence_error}")
                alert("ERROR EN HOT PATH - REVISAR MANUALMENTE")
                keep_open()
            if state != previous_state:
                logger.emit(f"HOT STATE {previous_state or 'START'} -> {state} | URL={page.url} | señal={signal}")
                if state == "QUEUE":
                    queue_started = time.monotonic()
                    logger.emit(f"QUEUE_ENTERED | QUEUE ENTERED | URL={page.url} | QUEUE_MODE=TARGET_VERIFIED")
                elif previous_state == "QUEUE":
                    elapsed = time.monotonic() - queue_started if queue_started else 0
                    logger.emit(f"QUEUE EXITED | duration_s={elapsed:.1f} | URL={page.url}")
                previous_state = state
            if state == "TICKET_SELECTION":
                logger.emit(f"TICKET_SELECTION | URL={page.url}")
                page.bring_to_front()
                alert("====================================\n"
                      "TURNO DISPONIBLE - SEGUIR MANUALMENTE\n"
                      "====================================", repeat=6)
                keep_open()
            if state == "UNKNOWN" and captcha_guard.awaiting_safe_state:
                # A redirect can briefly expose an empty document. Remain passive.
                try:
                    page.evaluate(HOT_WAIT_JS, {"timeoutMs": 100, "version": hot_version})
                except PlaywrightError:
                    pass
                continue
            if state != "UNKNOWN" and state != "CAPTCHA_REQUIRED":
                captcha_guard.awaiting_safe_state = False
            if state in ("UNKNOWN", "LOGIN_REQUIRED", "ERROR"):
                logger.emit(f"HOT PATH UNKNOWN | URL={page.url} | señal={signal}")
                try:
                    snapshot(page, "hot_unknown_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
                except PlaywrightError as evidence_error:
                    logger.emit(f"HOT PATH ERROR EVIDENCE FAILED | {evidence_error}")
                alert("ESTADO DESCONOCIDO - REVISAR MANUALMENTE")
                keep_open()
            if state != "CAPTCHA_REQUIRED" and awake_process is None and (
                    state == "QUEUE" or deadline - time.monotonic() <= 600):
                if page.is_closed():
                    logger.emit("HOT PATH ERROR | pestaña del target cerrada")
                    alert("PESTAÑA DEL TARGET CERRADA")
                    keep_open()
                try:
                    awake_process = subprocess.Popen(["caffeinate", "-di"], stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL)
                except OSError:
                    awake_process = False
                    logger.emit("HOT PATH ERROR | caffeinate no disponible")
            try:
                page.evaluate(HOT_WAIT_JS, {"timeoutMs": 100 if state == "CAPTCHA_REQUIRED" else 250, "version": hot_version})
            except PlaywrightError:
                # Navigation cancels the observer; the next probe handles the new page.
                pass
    finally:
        captcha_guard.close()
        if awake_process:
            awake_process.terminate()
        logger.close()


def read_watch_command():
    if not sys.stdin.isatty():
        return ""
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return sys.stdin.readline().strip().lower() if ready else ""


def watch_command_action(command, state, now, next_reload_at, target_verified=False):
    if command == "q":
        return "quit"
    if command == "i":
        return "inspect"
    if command == "r":
        return "reload" if state == "HOME" and not target_verified and now >= next_reload_at else "denied"
    return "none"


def format_debug_status(state, url, event_links, candidates, rejected, next_reload_at,
                        sale_deadline, now=None, queue_mode="NONE"):
    now = time.monotonic() if now is None else now
    sale_seconds = max(0, int(sale_deadline - now))
    hours, minutes = divmod(sale_seconds // 60, 60)
    reload_seconds = max(0, int(next_reload_at - now)) if state == "HOME" else None
    reload_text = f"{reload_seconds}s" if reload_seconds is not None else "disabled"
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    return (f"[{stamp}] {state}\nURL={url}\nevent_links={event_links}\n"
            f"argentina_candidates={candidates}\nrejected={rejected}\n"
            f"QUEUE_MODE={queue_mode}\nnext_reload={reload_text}\n"
            f"sale_in={hours}h{minutes:02d}m ({sale_seconds}s)")


def preflight():
    import platform

    checks = {}
    ARTIFACTS.mkdir(exist_ok=True)
    probe = ARTIFACTS / "preflight_write_check.txt"
    probe.write_text("ok", encoding="utf-8")
    checks["artifacts_writable"] = probe.read_text(encoding="utf-8") == "ok"
    checks["python"] = platform.python_version()
    checks["playwright"] = "importado"
    checks["timezone"] = datetime.now().astimezone().isoformat()
    checks["keywords"] = event_matches("Argentina vs Benín", ("Argentina", "Benin")) and not event_matches(
        "Argentina vs Brasil", ("Argentina", "Benin")
    )
    checks["ticket_selection_stops"] = should_stop_automation("TICKET_SELECTION")
    checks["profile_path"] = str(PROFILE.resolve())
    checks["profile_writable"] = os.access(PROFILE if PROFILE.exists() else PROFILE.parent, os.W_OK)
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(PROFILE), headless=False,
                                                                     accept_downloads=False)
            checks["chromium"] = "abrió"
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)
            data = page.evaluate(INSPECT_JS)
            checks["deportick"] = data["url"].startswith(START_URL)
            checks["login"] = session_status(data)
            snapshot(page, "preflight")
            context.close()
    except PlaywrightError as exc:
        checks["chromium_or_deportick_error"] = str(exc)
    alert("PRUEBA DE ALARMA", repeat=2)
    checks["audio"] = "reproducción solicitada; confirmar audición manualmente"
    path = ARTIFACTS / "preflight.json"
    path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return checks


def presale_watch(sale_time, event_url=None, debug_watch=False):
    locked_url = validated_event_url()
    if locked_url:
        event_url = locked_url
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(PROFILE), headless=False,
                                                                 accept_downloads=False)
        page = context.pages[0] if locked_url and context.pages else context.new_page()
        deadline = time.monotonic() + max(0, (sale_time - datetime.now(sale_time.tzinfo)).total_seconds())
        if locked_url:
            log_watch_precise(f"TARGET EVENT VERIFIED | desde target_event.json | URL={locked_url}")
            current_data = probe_page(page)
            current_state = classify(current_data, locked_url)
            protected_page = (current_state in ("QUEUE", "TICKET_SELECTION") or
                              detect_problem(current_data) is not None)
            if page.url.rstrip("/") != locked_url.rstrip("/") and not protected_page:
                page.goto(locked_url, wait_until="commit")
                log_watch(f"AUTO NAVIGATION | target confirmado | URL={page.url}")
            return hot_path_watch(page, deadline, locked_url)
        page.goto(START_URL, wait_until="commit")
        log_watch(f"AUTO NAVIGATION | HOME abierto | URL={page.url}")
        initial_data = page.evaluate(INSPECT_JS)
        if classify(initial_data, event_url) == "HOME" and session_status(initial_data) == "LOGIN_REQUIRED":
            log_watch("LOGIN_REQUIRED | sesión no confirmada; intervención manual necesaria")
            snapshot(page, "login_required_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
            alert("LOGIN REQUERIDO - INICIAR SESIÓN MANUALMENTE")
            keep_open()
        log_watch(f"PRE-SALE | objetivo Argentina + Benin | SALE_TIME={sale_time.isoformat()}")
        queue_mode = "NONE"
        log_watch(f"QUEUE_MODE: {queue_mode}")
        previous_state = None
        previous_url = page.url
        auto_pending = False
        last_auto_action = 0.0
        last_navigation_source = "INITIAL"
        queue_started = None
        last_reload = time.monotonic()
        next_home_reload = last_reload + (30 if debug_watch else RELOAD_INTERVAL + random.uniform(0, 15))
        previous_home_links = set()
        last_home_candidates = []
        target_verified = bool(locked_url)
        visual_cache = {}
        latest_signatures = []
        safety_logger = HotLogger()
        safety_guard = CaptchaGuard(safety_logger)
        try:
            while True:
                try:
                    data = probe_page(page)
                    state, signal = classify_with_signal(data, event_url)
                    safety_guard.observe(page, state)
                    if state == "CAPTCHA_REQUIRED" or (state == "UNKNOWN" and safety_guard.awaiting_safe_state):
                        wait_short(page, data)
                        continue
                    safety_guard.awaiting_safe_state = False
                    if state == "HOME":
                        data = page.evaluate(INSPECT_JS)
                        if classify(data, event_url) != "HOME":
                            continue
                except PlaywrightError as exc:
                    if not page.is_closed() and (transient_navigation_error(exc) or safety_guard.alarm is not None):
                        time.sleep(0.01)
                        continue
                    log_watch(f"UNKNOWN | pérdida de conexión o página cerrada: {exc}")
                    try:
                        snapshot(page, "unknown_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
                    except PlaywrightError:
                        pass
                    alert("CONEXIÓN O PÁGINA NO DISPONIBLE - REVISAR MANUALMENTE")
                    keep_open()
                if page.url != previous_url:
                    source = ("AUTO NAVIGATION" if auto_pending and
                              time.monotonic() - last_auto_action <= 10 else "MANUAL/EXTERNAL NAVIGATION")
                    log_watch(f"{source} | {previous_url} -> {page.url}")
                    last_navigation_source = source
                    previous_url = page.url
                    auto_pending = False
                elif auto_pending and time.monotonic() - last_auto_action > 10:
                    auto_pending = False
                remaining = max(0, deadline - time.monotonic())
                event_links = [item for item in data.get("links", [])
                               if "/event/" in (item.get("href") or "")]
                argentina_candidates = candidate_links(data, page.url) if state == "HOME" else []
                if debug_watch:
                    print(format_debug_status(state, page.url, len(event_links),
                                              len(argentina_candidates),
                                              sum(not item["accepted"] for item in visual_cache.values()),
                                              next_home_reload, deadline, queue_mode=queue_mode), flush=True)
                else:
                    print(f"Hora local: {datetime.now().astimezone().isoformat(timespec='seconds')} | "
                          f"SALE_TIME: {sale_time.isoformat()} | Restan: {remaining:.0f} s | "
                          f"Estado: {state} | URL: {page.url}", flush=True)
                log_watch(f"URL={page.url} | estado={state} | señal={signal} | QUEUE_MODE={queue_mode}")
                if state == "HOME" and debug_watch:
                    current_links = {urljoin(page.url, item["href"]) for item in event_links}
                    new_links = current_links - previous_home_links
                    log_watch(f"HOME SCAN | links encontrados={len(event_links)} | "
                              f"nuevos links={len(new_links)}")
                    for item in event_links:
                        absolute = urljoin(page.url, item["href"])
                        if absolute in new_links:
                            attributes = {"aria_label": item.get("aria_label"), "data": item.get("data"),
                                          "parent_data": item.get("parent_data"),
                                          "images": item.get("images")}
                            log_watch(f"NEW EVENT LINK DETECTED | href={item['href']} | "
                                      f"attributes={json.dumps(attributes, ensure_ascii=False)}")
                    previous_home_links = current_links
                    last_home_candidates = home_candidates(data, ("Argentina", "Benin"))
                if state != previous_state:
                    log_watch(f"Estado: {previous_state or 'START'} -> {state} | señal={signal}")
                    if state == "QUEUE":
                        queue_started = time.monotonic()
                        log_watch(f"QUEUE ENTERED | timestamp={datetime.now().astimezone().isoformat()} | "
                                  f"URL={page.url} | señal={signal} | QUEUE_MODE={queue_mode}")
                    elif previous_state == "QUEUE":
                        elapsed = time.monotonic() - queue_started if queue_started else 0
                        log_watch(f"QUEUE EXITED | tiempo en cola={elapsed:.0f} s | URL={page.url}")
                previous_state = state
                if should_stop_automation(state) and state == "TICKET_SELECTION":
                    page.bring_to_front()
                    log_watch(f"Llegada a selección; automatización detenida | navegación={last_navigation_source}")
                    if last_navigation_source == "MANUAL/EXTERNAL NAVIGATION":
                        log_watch("NO AUTO SUCCESS: llegada mediante navegación manual/externa")
                    alert("====================================\n"
                          "TURNO DISPONIBLE - SEGUIR MANUALMENTE\n"
                          "====================================", repeat=6)
                    keep_open()
                if state in ("UNKNOWN", "LOGIN_REQUIRED", "ERROR"):
                    snapshot(page, "unknown_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
                    alert(f"ESTADO DESCONOCIDO - {signal} - REVISAR MANUALMENTE")
                    keep_open()
                command = read_watch_command()
                action = watch_command_action(command, state, time.monotonic(), next_home_reload,
                                              target_verified)
                if action == "quit":
                    log_watch("Pre-sale detenido por comando q")
                    break
                if action == "inspect":
                    current = (home_candidates(data, ("Argentina", "Benin")) if state == "HOME"
                               else last_home_candidates)
                    print(json.dumps(current, ensure_ascii=False, indent=2), flush=True)
                if action == "denied":
                    print("Reload denegado: sólo HOME, sin verificación pendiente y tras el intervalo seguro.",
                          flush=True)
                if action == "reload":
                    log_watch(f"HOME RELOAD | reason=manual_request | URL={page.url}")
                    page.reload(wait_until="commit")
                    last_reload = time.monotonic()
                    next_home_reload = last_reload + (30 if debug_watch else RELOAD_INTERVAL + random.uniform(0, 15))
                    continue
                if state == "QUEUE":
                    queue_mode = "UNVERIFIED"
                    log_watch(f"QUEUE_MODE: {queue_mode} | discovery halted")
                    snapshot(page, "queue_unverified_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
                    alert("QUEUE_UNVERIFIED - DETENER DISCOVERY - REVISAR MANUALMENTE")
                    keep_open()
                elif state == "HOME":
                    if not can_discover_more(queue_mode):
                        log_watch(f"HOME con discovery bloqueado | QUEUE_MODE={queue_mode}")
                        alert("DISCOVERY BLOQUEADO - REVISAR MANUALMENTE")
                        keep_open()
                    try:
                        reports, target_anchor, latest_signatures = visual_scan_cards(page, visual_cache)
                    except (PlaywrightError, RuntimeError) as exc:
                        log_watch(f"VISUAL DISCOVERY ERROR | {exc}")
                        snapshot(page, "visual_error_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
                        alert("OCR O CARD DISCOVERY FALLÓ - REVISAR MANUALMENTE")
                        keep_open()
                    if target_anchor:
                        queue_mode = "TARGET_VERIFIED"
                        return enter_verified_card(page, target_anchor, reports[-1], deadline)
                    save_home_diagnostics(data, ("Argentina", "Benin"))
                    if page.url == START_URL and time.monotonic() >= next_home_reload:
                        reason = "debug_validation" if debug_watch else "conservative_interval"
                        log_watch(f"HOME RELOAD | reason={reason} | URL={page.url}")
                        page.reload(wait_until="commit")
                        auto_pending = True
                        last_auto_action = time.monotonic()
                        last_reload = time.monotonic()
                        next_home_reload = last_reload + (
                            30 if debug_watch else RELOAD_INTERVAL + random.uniform(0, 15)
                        )
                elif state in ("EVENT_AVAILABLE", "SALE_NOT_STARTED"):
                    log_watch(f"Discovery left HOME without target lock | URL={page.url}")
                    snapshot(page, "unknown_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
                    alert("EVENTO SIN TARGET LOCK - REVISAR MANUALMENTE")
                    keep_open()
                if state == "HOME":
                    try:
                        page.evaluate(VISUAL_WAIT_JS,
                                      {"known": latest_signatures,
                                       "timeoutMs": int(min(POLL_INTERVAL, 5) * 1000)})
                    except PlaywrightError:
                        pass
                else:
                    wait_for_dom_change(page, data.get("body_text", ""), min(POLL_INTERVAL, 5))
        except KeyboardInterrupt:
            log_watch("Pre-sale detenido por el usuario")
        finally:
            safety_guard.close()
            safety_logger.close()
            context.close()


def visual_scan():
    """Inspect HOME cards visually without clicking an event."""
    ensure_ocr_binary()
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(PROFILE), headless=False,
                                                                 accept_downloads=False)
        try:
            page = context.new_page()
            page.goto(START_URL, wait_until="domcontentloaded")
            reports, _, _ = visual_scan_cards(page, {}, save_cards=True, stop_on_match=False)
            for index, report in enumerate(reports, 1):
                print(f"CARD {index}\n"
                      f"href={report['href']}\n"
                      f"image_src={json.dumps(report['image_src'], ensure_ascii=False)}\n"
                      f"ocr_text={report['ocr_text']!r}\n"
                      f"match_argentina={str(report['match_argentina']).lower()}\n"
                      f"match_benin={str(report['match_benin']).lower()}\n"
                      f"accepted={str(report['accepted']).lower()}\n", flush=True)
            if not reports:
                print("No se encontraron cards visibles con imágenes de eventos.", flush=True)
            return reports
        finally:
            context.close()


def watch(simulation=None, event_url=None, keywords=EVENT_KEYWORDS):
    if simulation:
        states = [classify(item, event_url) for item in json.loads(Path(simulation).read_text(encoding="utf-8"))]
        for state in states:
            print(state)
        return states
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(PROFILE), headless=HEADLESS,
                                                                 accept_downloads=False)
        page = context.pages[0] if context.pages else context.new_page()
        logger = HotLogger()
        guard = CaptchaGuard(logger)
        previous = None
        last_reload = time.monotonic()
        next_home_reload = last_reload + RELOAD_INTERVAL + random.uniform(0, 15)
        next_home_scan = 0
        clicked = set()
        pending = None
        try:
            if (event_url and page.url.rstrip("/") != event_url.rstrip("/")) or page.url == "about:blank":
                # Preserve an already-open challenge or queue on startup.
                current = probe_page(page)
                state = classify(current, event_url)
                if state not in ("CAPTCHA_REQUIRED", "QUEUE", "TICKET_SELECTION", "LOGIN_REQUIRED", "ERROR"):
                    page.goto(event_url or START_URL, wait_until="commit")
                    pending = wait_after_action(page, event_url, guard)
            while True:
                try:
                    data = pending if pending is not None else probe_page(page)
                    pending = None
                    state, signal = classify_with_signal(data, event_url)
                    guard.observe(page, state)
                    if state != previous:
                        logger.emit(f"URL={data['url']} | estado={state} | señal={signal}")
                        if state == "QUEUE":
                            logger.emit(f"QUEUE_ENTERED | URL={data['url']} | QUEUE_ACTUAL")
                        previous = state
                    if state == "CAPTCHA_REQUIRED" or (state == "UNKNOWN" and guard.awaiting_safe_state):
                        wait_short(page, data)
                        continue
                    guard.awaiting_safe_state = False
                    if state in ("LOGIN_REQUIRED", "ERROR", "TICKET_SELECTION", "UNKNOWN"):
                        page.bring_to_front()
                        alert(f"{state} - REVISAR MANUALMENTE")
                        keep_open()
                    target = None
                    if state == "HOME" and time.monotonic() >= next_home_scan:
                        # Discovery is throttled separately from safety-state detection.
                        details = page.evaluate(INSPECT_JS)
                        if classify(details, event_url) != "HOME":
                            continue
                        target = event_link(page, details, event_url, keywords)
                        next_home_scan = time.monotonic() + POLL_INTERVAL
                    elif state == "EVENT_AVAILABLE":
                        target = buy_locator(page, data)
                    signature = (data["url"], str(target))
                    if target and signature not in clicked:
                        # No navigation/load wait: observe all safety states immediately.
                        target.click(timeout=5000, no_wait_after=True)
                        clicked.add(signature)
                        pending = wait_after_action(page, event_url, guard)
                        logger.emit(f"AUTO CLICK | selector={target} | URL={page.url}")
                        continue
                    if ((state == "HOME" and time.monotonic() >= next_home_reload) or
                            (state == "SALE_NOT_STARTED" and time.monotonic() - last_reload >= RELOAD_INTERVAL)):
                        page.reload(wait_until="commit")
                        last_reload = time.monotonic()
                        next_home_reload = last_reload + RELOAD_INTERVAL + random.uniform(0, 15)
                        pending = wait_after_action(page, event_url, guard)
                        continue
                    wait_short(page, data)
                except PlaywrightError as exc:
                    if not page.is_closed() and (transient_navigation_error(exc) or guard.alarm is not None):
                        time.sleep(0.01)
                        continue
                    raise
        except KeyboardInterrupt:
            logger.emit("Watch detenido por el usuario")
        finally:
            guard.close()
            logger.close()
            context.close()


def captcha_test():
    """Local synthetic DOM transitions; no website or CAPTCHA provider is contacted."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.set_content("<h1>TARGET VERIFIED</h1>")
        target = page.url
        logger = HotLogger()
        logger.emit("TARGET VERIFIED | synthetic captcha-test")
        guard = CaptchaGuard(logger)
        try:
            page.set_content("<h1>CAPTCHA — verificación simulada</h1>")
            hot_step(page, target, logger, set(), guard)
            page.wait_for_timeout(1200)
            # Replace our own fixture; this does not solve a challenge.
            page.set_content("<h1>Fila virtual</h1>")
            state, _, _ = hot_step(page, target, logger, set(), guard)
            assert state == "QUEUE"
            logger.emit("QUEUE_ENTERED | QUEUE | QUEUE_MODE=TARGET_VERIFIED | synthetic")
        finally:
            guard.close()
            logger.close()
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["explore", "watch", "visual-scan", "preflight",
                                          "alarm-test", "captcha-test"])
    parser.add_argument("--simulate", metavar="JSON")
    parser.add_argument("--url", metavar="URL")
    parser.add_argument("--keywords", metavar="TEXTO,TEXTO")
    parser.add_argument("--presale", action="store_true")
    parser.add_argument("--debug-watch", action="store_true")
    args = parser.parse_args()
    if args.mode == "explore":
        explore()
    elif args.mode == "visual-scan":
        visual_scan()
    elif args.mode == "captcha-test":
        captcha_test()
    elif args.mode == "alarm-test":
        alert("PRUEBA DE ALARMA", repeat=4)
    elif args.mode == "preflight":
        preflight()
    else:
        if args.debug_watch and not args.presale:
            parser.error("--debug-watch requiere --presale")
        event_url, keywords, source = resolve_target(args.url, args.keywords)
        if args.presale:
            if args.keywords and {normalized(x) for x in keywords} != {"argentina", "benin"}:
                parser.error("--presale solo admite el objetivo Argentina + Benin")
            sale_time = parse_sale_time(os.environ.get("SALE_TIME"))
            presale_watch(sale_time, event_url, args.debug_watch)
        else:
            log_watch(f"Objetivo configurado desde {source}: URL={event_url or 'sin URL'} | keywords={keywords}")
            watch(args.simulate, event_url, keywords)
