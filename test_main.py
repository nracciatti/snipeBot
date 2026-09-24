import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

import main


class SafetyTests(unittest.TestCase):
    def test_payment_and_verification_are_blocked(self):
        for label in ("Pagar", "Confirmar compra", "Tarjeta", "CAPTCHA", "Fila virtual"):
            self.assertIsNotNone(main.BLOCKED.search(label))

    def test_selector_is_only_recorded_after_action(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "ARTIFACTS", Path(folder)):
            self.assertEqual(main.load_discovered_selectors(), {})
            main.record({"category": "buy_button", "locator": "button[id=\"buy\"]",
                         "element": {"text": "Comprar"}})
            main.record({"category": "buy_button", "locator": "button[id=\"buy\"]",
                         "element": {"text": "Comprar"}})
            self.assertEqual(len(main.load_discovered_selectors()["buy_button"]), 1)

    def test_simulated_state_machine_stops_at_selection(self):
        states = [
            {"url": "https://www.deportick.com/", "body_text": "", "links": []},
            {"url": "https://www.deportick.com/event/example", "body_text": "Próximamente",
             "buttons": []},
            {"url": "https://www.deportick.com/event/example", "body_text": "",
             "buttons": [{"text": "Comprar"}]},
            {"url": "https://queue.deportick.com/queue", "body_text": "Fila virtual"},
            {"url": "https://www.deportick.com/event/example", "body_text": "",
             "headings": [{"text": "Seleccioná tus entradas"}],
             "selects": [{"text": "Cantidad"}]},
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "states.json"
            path.write_text(main.json.dumps(states), encoding="utf-8")
            self.assertEqual(main.watch(path), ["HOME", "SALE_NOT_STARTED",
                                                "EVENT_AVAILABLE", "QUEUE", "TICKET_SELECTION"])

    def test_event_requires_both_names(self):
        self.assertFalse(main.event_matches("Argentina vs Brasil"))
        self.assertTrue(main.event_matches("Argentina vs Benín"))

    def test_visual_match_requires_both_names_in_one_card_ocr(self):
        self.assertFalse(main.visual_card_match("ARGENTINA C.BOLIVIA")["accepted"])
        self.assertTrue(main.visual_card_match("ARGENTINA C. BENÍN")["accepted"])
        self.assertTrue(main.visual_card_match("ARGENTINA C. BENlN")["accepted"])
        self.assertFalse(main.visual_card_match("ARGENTINA C. BURKINA FASO")["accepted"])
        self.assertFalse(main.visual_card_match("ARGENTINA C. BOLIVIA", "benin")["accepted"])
        self.assertFalse(main.visual_card_match("ARGENTINA", "benin")["accepted"])

    def test_visual_cache_skips_unchanged_card_ocr(self):
        anchor = MagicMock()
        anchor.evaluate.return_value = {"href": "../event/argentinabol26", "text": "",
                                        "title": "", "aria_label": "",
                                        "images": [{"src": "poster.png", "alt": "", "title": "",
                                                    "aria_label": ""}]}
        info = anchor.evaluate.return_value
        anchor.evaluate.side_effect = lambda script: ('' if script == main.CARD_EVIDENCE_JS else dict(info))
        anchor.bounding_box.return_value = {"width": 400, "height": 200}
        anchor.locator.return_value.all.return_value = []
        anchor.screenshot.return_value = b"png"
        anchors = MagicMock()
        anchors.count.return_value = 1
        anchors.nth.return_value = anchor
        page = MagicMock()
        page.url = main.START_URL
        page.locator.return_value = anchors
        cache = {}
        with patch.object(main, "ocr_card", return_value="ARGENTINA C. BOLIVIA") as ocr:
            first, selected, _ = main.visual_scan_cards(page, cache)
            second, selected_again, _ = main.visual_scan_cards(page, cache)
        self.assertEqual(first, second)
        self.assertIsNone(selected)
        self.assertIsNone(selected_again)
        ocr.assert_called_once_with(b"png")

    def test_visual_target_click_precedes_file_and_log(self):
        events = []
        page = MagicMock()
        page.url = main.START_URL
        anchor = MagicMock()
        anchor.click.side_effect = lambda **_: events.append("click")
        report = {"href": "https://www.deportick.com/event/argentinabenin",
                  "image_src": ["poster.png"], "ocr_text": "ARGENTINA C. BENIN"}
        with patch.object(main, "save_validated_event",
                          side_effect=lambda *_: events.append("save")), \
             patch.object(main, "log_watch_precise",
                          side_effect=lambda *_: events.append("log")), \
             patch.object(main, "snapshot", side_effect=AssertionError("evidence before click")), \
             patch.object(main, "hot_path_watch", side_effect=lambda *_: events.append("hot")):
            main.enter_verified_card(page, anchor, report, 100)
        self.assertEqual(events, ["click", "save", "log", "log", "log", "hot"])

    def test_target_precedence(self):
        env = {"EVENT_URL": "https://www.deportick.com/event/env",
               "EVENT_KEYWORDS": "Env A,Env B"}
        self.assertEqual(main.resolve_target("https://www.deportick.com/event/cli", "Cli A,Cli B", env),
                         ("https://www.deportick.com/event/cli", main.EVENT_KEYWORDS, "--url"))
        self.assertEqual(main.resolve_target(cli_keywords="Cli A,Cli B", environ=env),
                         (None, ("Cli A", "Cli B"), "--keywords"))
        self.assertEqual(main.resolve_target(environ=env)[2], "EVENT_URL")
        self.assertEqual(main.resolve_target(environ={"EVENT_KEYWORDS": "A,B"}),
                         (None, ("A", "B"), "EVENT_KEYWORDS"))
        self.assertEqual(main.resolve_target(environ={})[2], "default")

    def test_classification_signal_and_selection_priority(self):
        data = {"url": "https://www.deportick.com/event/example", "body_text": "",
                "headings": [{"text": "Selección de entradas"}],
                "selects": [{"text": "Cantidad"}], "buttons": [{"text": "Comprar"}]}
        state, signal = main.classify_with_signal(data)
        self.assertEqual(state, "TICKET_SELECTION")
        self.assertIn("selección", signal)

    def test_presale_identity_is_same_heading(self):
        self.assertFalse(main.event_identity_matches({"title": "Deportick", "headings": [
            {"text": "Argentina vs Brasil"}, {"text": "Benín vs Chile"}]}))
        self.assertTrue(main.event_identity_matches({"headings": [
            {"text": "  ARGENTINA   vs  BENÍN "}]}))
        self.assertTrue(main.event_matches(" ARGENTINA    vs   BENÍN ", ("Argentina", "Benin")))

    def test_queue_signals_and_errors(self):
        base = {"url": "https://www.deportick.com/event/example", "body_text": ""}
        self.assertEqual(main.classify_with_signal({**base, "title": "Sala de espera"})[0], "QUEUE")
        self.assertEqual(main.classify_with_signal({**base, "iframes": [
            {"src": "https://queue.deportick.com/room"}]})[0], "QUEUE")
        self.assertEqual(main.classify_with_signal({**base, "body_text": "Error 429"})[0], "ERROR")

    def test_presale_time_and_stop_gate(self):
        self.assertIsNotNone(main.parse_sale_time("2026-09-22T18:00:00-03:00").utcoffset())
        with self.assertRaises(ValueError):
            main.parse_sale_time("2026-09-22T18:00:00")
        self.assertTrue(main.should_stop_automation("TICKET_SELECTION"))
        self.assertFalse(main.should_stop_automation("QUEUE"))

    def test_session_prompt(self):
        self.assertEqual(main.session_status({"links": [{"text": "Ingresar / Registrarse"}]}),
                         "LOGIN_REQUIRED")

    def test_ambiguous_ticket_controls_block_presale_actions(self):
        self.assertTrue(main.selection_risk({"selects": [{"text": "2"}]}))
        self.assertTrue(main.selection_risk({"headings": [{"text": "Datos del asistente"}]}))
        self.assertFalse(main.selection_risk({"headings": [{"text": "Argentina vs Benín"}]}))

    def test_home_candidates_require_both_names_in_one_card(self):
        links = [
            {"href": "../event/central", "text": "", "context": "",
             "images": [{"src": "https://example.test/rosario-central-estudiantes.png"}],
             "context_event_links": 1},
            {"href": "../event/otro", "text": "Rosario Central vs Boca", "context": "",
             "images": [], "context_event_links": 1},
        ]
        candidates = main.home_candidates({"links": links}, ("Rosario Central", "Estudiantes"))
        self.assertEqual([(x["match_first"], x["match_second"]) for x in candidates],
                         [(True, True), (True, False)])
        self.assertEqual(candidates[0]["selector"], 'a[href="../event/central"]')

    def test_presale_candidate_discovery_uses_observed_attributes(self):
        links = [
            {"href": "../event/argentinabol26", "text": "", "context": "",
             "images": [{"src": "https://cdn.example/arg-bol.png"}], "context_event_links": 1},
            {"href": "../event/brasil", "text": "Brasil vs Chile", "context_event_links": 1},
            {"href": "../event/hiddenname", "text": "", "context": "",
             "aria_label": "", "parent_data": {"data-team": "Argentina"},
             "context_event_links": 1},
        ]
        found = main.candidate_links({"links": links}, "https://www.deportick.com/")
        self.assertEqual([url for url, _ in found],
                         ["https://www.deportick.com/event/argentinabol26",
                          "https://www.deportick.com/event/hiddenname"])

    def test_candidate_verification_never_clicks_and_rejects_other_match(self):
        page = MagicMock()
        page.url = "https://www.deportick.com/event/argentinabol26"
        page.evaluate.return_value = {"url": page.url, "title": "Argentina vs Bolivia",
                                      "body_text": "Argentina vs Bolivia", "headings": []}
        context = MagicMock()
        context.new_page.return_value = page
        with patch.object(main, "log_watch"):
            outcome, kept = main.verify_candidate(context, page.url)
        self.assertEqual((outcome, kept), ("REJECTED", None))
        page.close.assert_called_once()
        page.click.assert_not_called()

    def test_confirmed_candidate_persists_identity_before_hot_path(self):
        page = MagicMock()
        page.url = "https://www.deportick.com/event/argentinabenin"
        page.evaluate.return_value = {"url": page.url, "title": "Argentina vs Benín",
                                      "body_text": "Argentina vs Benín", "headings": []}
        context = MagicMock()
        context.new_page.return_value = page
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "ARTIFACTS", Path(folder)), \
             patch.object(main, "log_watch_precise"):
            outcome, kept = main.verify_candidate(context, page.url)
            saved = json.loads((Path(folder) / "target_event.json").read_text())
        self.assertEqual((outcome, kept), ("TARGET", page))
        self.assertEqual(saved["url"], page.url)
        self.assertEqual(saved["verified_text"], "Argentina vs Benín")
        page.click.assert_not_called()

    def test_verification_does_not_combine_separate_matches(self):
        data = {"title": "Deportick", "headings": [],
                "body_text": "Argentina vs Bolivia\nBenín vs Chile"}
        self.assertEqual(main.verify_event_identity(data), "REJECTED")
        self.assertEqual(main.verify_event_identity({"title": "Argentina vs Benín"}), "TARGET")

    def test_debug_reload_gate(self):
        self.assertEqual(main.watch_command_action("r", "HOME", 29, 30), "denied")
        self.assertEqual(main.watch_command_action("r", "HOME", 30, 30), "reload")
        self.assertEqual(main.watch_command_action("r", "QUEUE", 60, 30), "denied")
        self.assertEqual(main.watch_command_action("r", "UNKNOWN", 60, 30), "denied")
        self.assertEqual(main.watch_command_action("r", "HOME", 60, 30, True), "denied")
        self.assertEqual(main.watch_command_action("i", "QUEUE", 60, 30), "inspect")
        self.assertEqual(main.watch_command_action("q", "QUEUE", 60, 30), "quit")

    def test_debug_status_contains_required_counts_and_time(self):
        display = main.format_debug_status("HOME", "https://www.deportick.com/", 4, 1, 1,
                                           next_reload_at=130, sale_deadline=3600, now=100)
        for value in ("HOME", "event_links=4", "argentina_candidates=1", "rejected=1",
                      "next_reload=30s", "3500s"):
            self.assertIn(value, display)

    def test_queue_before_identity_stops_discovery_and_saves_before_close(self):
        events = []
        page = MagicMock()
        page.url = "https://www.deportick.com/event/ambiguous"
        page.evaluate.return_value = {"url": page.url, "title": "Fila virtual",
                                      "body_text": "Esperá tu turno",
                                      "headings": [{"text": "Selección de entradas"}],
                                      "selects": [{"text": "Cantidad"}]}
        page.close.side_effect = lambda: events.append("close")
        context = MagicMock()
        context.new_page.return_value = page
        with patch.object(main, "log_watch") as log, patch.object(main, "snapshot",
                                                       side_effect=lambda *_: events.append("evidence")):
            outcome, kept = main.verify_candidate(context, page.url, {"href": "../event/ambiguous"}, True)
        self.assertEqual((outcome, kept), ("QUEUE_UNVERIFIED", None))
        self.assertEqual(events, ["evidence", "close"])
        self.assertTrue(any("QUEUE ENCOUNTERED BEFORE EVENT VERIFICATION" in call.args[0]
                            for call in log.call_args_list))
        self.assertFalse(main.can_discover_more("UNVERIFIED"))

    def test_production_unverified_queue_tab_is_kept(self):
        page = MagicMock()
        page.url = "https://www.deportick.com/event/ambiguous"
        page.evaluate.return_value = {"url": page.url, "title": "Fila virtual", "body_text": ""}
        context = MagicMock()
        context.new_page.return_value = page
        with patch.object(main, "log_watch"), patch.object(main, "snapshot"):
            outcome, kept = main.verify_candidate(context, page.url, None, False)
        self.assertEqual(outcome, "QUEUE_UNVERIFIED")
        self.assertIs(kept, page)
        page.close.assert_not_called()

    def test_verified_target_file_locks_queue_and_discovery(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "ARTIFACTS", Path(folder)):
            main.save_validated_event("https://www.deportick.com/event/argentinabenin",
                                      {"source": "event_page"})
            self.assertEqual(main.validated_event_url(),
                             "https://www.deportick.com/event/argentinabenin")
        self.assertFalse(main.can_discover_more("TARGET_VERIFIED"))
        page = MagicMock()
        page.url = "https://www.deportick.com/event/argentinabenin"
        page.evaluate.return_value = {"url": page.url, "title": "Fila virtual", "body_text": ""}
        logger = MagicMock()
        with patch.object(main, "candidate_links", side_effect=AssertionError("discovery called")), \
             patch.object(main, "snapshot", side_effect=AssertionError("evidence called")):
            state, _, _ = main.hot_step(page, page.url, logger, set())
        self.assertEqual(state, "QUEUE")
        page.close.assert_not_called()
        page.reload.assert_not_called()
        page.goto.assert_not_called()
        logger.emit.assert_not_called()

    def test_two_argentina_events_do_not_identify_benin(self):
        links = [{"href": "../event/argentinabol26", "text": "Argentina vs Bolivia",
                  "context_event_links": 1},
                 {"href": "../event/argentinabrasil", "text": "Argentina vs Brasil",
                  "context_event_links": 1}]
        self.assertEqual(len(main.candidate_links({"links": links}, main.START_URL)), 2)
        self.assertTrue(all(not main.home_target_verified(link) for link in links))

    def test_snapshot_retries_html_during_navigation(self):
        page = MagicMock()
        page.evaluate.return_value = {"url": "https://www.deportick.com/queue", "title": "Fila"}
        page.content.side_effect = [PlaywrightError("page is navigating"), "<html>fila</html>"]
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "ARTIFACTS", Path(folder)):
            main.snapshot(page, "queue")
            self.assertTrue((Path(folder) / "queue" / "elements.json").exists())
            self.assertEqual((Path(folder) / "queue" / "page.html").read_text(), "<html>fila</html>")
        page.wait_for_timeout.assert_called_once_with(500)

    def test_hot_buy_click_precedes_logging_and_skips_discovery_evidence(self):
        target = "https://www.deportick.com/event/argentinabenin"
        data = {"url": target, "title": "Argentina vs Benín", "body_text": "",
                "headings": [], "buttons": [{"tag": "button", "text": "Comprar", "id": "buy",
                                             "role": None, "data": {}, "href": None,
                                             "in_navigation": False}],
                "links": [], "inputs": [], "selects": [], "iframes": []}
        events = []
        page = MagicMock()
        page.evaluate.side_effect = lambda *_: (events.append("probe"), data)[1]
        locator = MagicMock()
        locator.click.side_effect = lambda **_: events.append("click")
        logger = MagicMock()
        logger.emit.side_effect = lambda *_: events.append("log")
        with patch.object(main, "unique_locator", return_value=locator), \
             patch.object(main, "candidate_links", side_effect=AssertionError("discovery called")), \
             patch.object(main, "snapshot", side_effect=AssertionError("evidence called")):
            state, _, _ = main.hot_step(page, target, logger, set())
        self.assertEqual(state, "EVENT_AVAILABLE")
        self.assertEqual(events, ["probe", "click", "log", "log"])
        self.assertIn("reaction_ms=", logger.emit.call_args_list[1].args[0])

    def test_hot_path_rejects_changed_event_identity_without_click(self):
        target = "https://www.deportick.com/event/argentinabenin"
        page = MagicMock()
        page.evaluate.return_value = {"url": target, "title": "Argentina vs Bolivia",
                                      "body_text": "Argentina vs Bolivia", "headings": [],
                                      "buttons": [{"tag": "button", "text": "Comprar", "id": "buy",
                                                   "role": None, "data": {}, "href": None}],
                                      "links": [], "inputs": [], "selects": [], "iframes": []}
        logger = MagicMock()
        state, signal, _ = main.hot_step(page, target, logger, set())
        self.assertEqual(state, "UNKNOWN")
        self.assertIn("otro partido", signal)
        page.click.assert_not_called()
        logger.emit.assert_not_called()

    def test_captcha_signals_override_queue_and_buy(self):
        for signals in ({"body_text": "No soy un robot"},
                        {"title": "Verificación"},
                        {"challenges": [{"id": "challenge-stage"}]},
                        *({"iframes": [{"src": src}]} for src in (
                            "https://google.com/recaptcha/api2/anchor",
                            "https://hcaptcha.com/widget",
                            "https://challenges.cloudflare.com/turnstile"))):
            with self.subTest(signals=signals):
                data = {"url": "https://queue.deportick.com/queue", **signals}
                self.assertEqual(main.classify_with_signal(data)[0], "CAPTCHA_REQUIRED")

    def test_captcha_guard_passive_then_automatic_queue(self):
        target = "https://www.deportick.com/event/argentinabenin"
        page, logger = MagicMock(), MagicMock()
        guard = main.CaptchaGuard(logger)
        challenge = {"url": target, "body_text": "captcha", "hot_version": 1}
        page.evaluate.side_effect = [challenge, challenge,
                                     {"url": "https://queue.deportick.com/queue"}]
        with patch.object(main, "CaptchaAlarm") as alarm, \
             patch.object(main, "hot_action_locator", side_effect=AssertionError("click attempted")), \
             patch.object(main, "snapshot", side_effect=AssertionError("evidence attempted")), \
             patch.object(main, "visual_scan_cards", side_effect=AssertionError("discovery attempted")):
            states = [main.hot_step(page, target, logger, set(), guard)[0] for _ in range(3)]
            self.assertEqual(states, ["CAPTCHA_REQUIRED", "CAPTCHA_REQUIRED", "QUEUE"])
            alarm.assert_called_once()
            alarm.return_value.stop.assert_called_once()
        page.bring_to_front.assert_called_once()
        for method in (page.reload, page.goto, page.close, page.screenshot, page.content):
            method.assert_not_called()
        messages = [call.args[0] for call in logger.emit.call_args_list]
        self.assertIn("CAPTCHA_DETECTED", messages[0])
        self.assertIn("CAPTCHA_CLEARED", messages[1])

    def test_captcha_clear_resumes_buy_in_same_probe(self):
        page, logger = MagicMock(), MagicMock()
        target = "https://www.deportick.com/event/argentinabenin"
        guard = main.CaptchaGuard(logger)
        guard.alarm = MagicMock()
        page.evaluate.return_value = {"url": target, "body_text": ""}
        locator = MagicMock()
        with patch.object(main, "hot_action_locator", return_value=(locator, "comprar")):
            main.hot_step(page, target, logger, set(), guard)
        locator.click.assert_called_once()
        self.assertIn("CAPTCHA_CLEARED", logger.emit.call_args_list[0].args[0])


    def test_hot_loop_captcha_redirect_queue_without_evidence_or_enter(self):
        target = "https://www.deportick.com/event/argentinabenin"
        page = MagicMock()
        page.url = target
        page.is_closed.return_value = False
        probes = iter([
            {"url": target, "body_text": "captcha"},
            {"url": "https://www.deportick.com/redirect", "body_text": ""},
            {"url": "https://queue.deportick.com/queue"},
        ])
        page.evaluate.side_effect = lambda script, *args: next(probes) if script == main.HOT_PROBE_JS else None
        with patch.object(main, "HotLogger") as logger, \
             patch.object(main, "CaptchaAlarm") as alarm, \
             patch.object(main, "read_watch_command", side_effect=["", "", "", "q"]), \
             patch.object(main.subprocess, "Popen"), \
             patch.object(main, "snapshot", side_effect=AssertionError("evidence")), \
             patch.object(main, "keep_open", side_effect=AssertionError("ENTER")), \
             patch.object(main, "hot_action_locator", side_effect=AssertionError("action")):
            main.hot_path_watch(page, main.time.monotonic() + 1000, target)
        alarm.return_value.stop.assert_called_once()
        messages = [call.args[0] for call in logger.return_value.emit.call_args_list]
        self.assertTrue(any("QUEUE_ENTERED" in msg and "TARGET_VERIFIED" in msg for msg in messages))
        page.reload.assert_not_called()
        page.goto.assert_not_called()


    def test_real_queue_it_captcha_priority(self):
        url = "https://deportick.queue-it.net/?c=deportick&e=argentinabol26"
        self.assertEqual(main.classify({"url": url, "body_text": "No soy un robot"}),
                         "CAPTCHA_REQUIRED")
        self.assertEqual(main.classify({"url": url, "body_text": "Fila virtual. Espera tu turno"}),
                         "QUEUE")
        for text in ("I'm not a robot", "I’m not a robot", "verification", "challenge", "verificación"):
            with self.subTest(text=text):
                self.assertEqual(main.classify({"url": url, "body_text": text}), "CAPTCHA_REQUIRED")
        self.assertEqual(main.classify({"url": url, "inputs": [
            {"type": "checkbox", "aria_label": "No soy un robot"}]}), "CAPTCHA_REQUIRED")

    def test_state_priority_over_queue(self):
        data = {"url": "https://deportick.queue-it.net/", "body_text": "Error 429",
                "inputs": [{"type": "password"}], "selects": [{"text": "sector cantidad"}]}
        self.assertEqual(main.classify(data), "LOGIN_REQUIRED")
        self.assertEqual(main.classify({**data, "body_text": "captcha Error 429"}), "CAPTCHA_REQUIRED")
        data["inputs"] = []
        self.assertEqual(main.classify(data), "ERROR")
        data["body_text"] = ""
        self.assertEqual(main.classify(data), "TICKET_SELECTION")
        data["selects"] = []
        self.assertEqual(main.classify(data), "QUEUE")

    def test_post_click_race_has_no_long_poll(self):
        page, logger = MagicMock(), MagicMock()
        data = {"url": "https://deportick.queue-it.net/", "body_text": "No soy un robot"}
        guard = main.CaptchaGuard(logger)
        with patch.object(main, "probe_page", side_effect=[
                {"url": "https://www.deportick.com/event/example"}, data]), \
             patch.object(main, "CaptchaAlarm"), \
             patch.object(main.time, "sleep", side_effect=AssertionError("polling sleep")):
            result = main.wait_after_action(page, None, guard)
        self.assertEqual(main.classify(result), "CAPTCHA_REQUIRED")
        self.assertEqual(page.evaluate.call_args.args[1]["timeoutMs"], 100)
        page.bring_to_front.assert_called_once()
        guard.close()

    def test_generic_watch_captcha_then_queue_is_passive(self):
        page = MagicMock()
        page.url = "https://deportick.queue-it.net/"
        page.frames = []
        challenge = {"url": page.url, "body_text": "No soy un robot"}
        queue = {"url": page.url, "body_text": "Sala de espera"}
        probes = iter([challenge, challenge, queue])
        def evaluate(script, *args):
            if script == main.HOT_PROBE_JS:
                try:
                    return next(probes)
                except StopIteration:
                    raise KeyboardInterrupt
        page.evaluate.side_effect = evaluate
        with patch.object(main, "sync_playwright") as playwright, \
             patch.object(main, "HotLogger") as logger, \
             patch.object(main, "CaptchaAlarm") as alarm, \
             patch.object(main, "keep_open", side_effect=AssertionError("ENTER")), \
             patch.object(main, "snapshot", side_effect=AssertionError("snapshot")), \
             patch.object(main.time, "sleep", side_effect=AssertionError("long sleep")):
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.pages = [page]
            main.watch(event_url="https://www.deportick.com/event/argentinabol26")
        page.goto.assert_not_called()
        page.reload.assert_not_called()
        page.click.assert_not_called()
        page.bring_to_front.assert_called_once()
        alarm.return_value.stop.assert_called_once()
        messages = [call.args[0] for call in logger.return_value.emit.call_args_list]
        detected = next(i for i, msg in enumerate(messages) if "CAPTCHA_DETECTED" in msg)
        cleared = next(i for i, msg in enumerate(messages) if "CAPTCHA_CLEARED" in msg)
        queued = next(i for i, msg in enumerate(messages) if "QUEUE_ENTERED" in msg)
        self.assertLess(detected, cleared)
        self.assertLess(cleared, queued)

    def test_visible_embedded_queue_it_challenge(self):
        page, frame = MagicMock(), MagicMock()
        page.evaluate.return_value = {"url": "https://deportick.queue-it.net/"}
        page.frames = [page.main_frame, frame]
        frame.parent_frame = page.main_frame
        page.main_frame.parent_frame = None
        frame.frame_element.return_value.is_visible.return_value = True
        frame.evaluate.return_value = {"body_text": "No soy un robot"}
        self.assertEqual(main.classify(main.probe_page(page)), "CAPTCHA_REQUIRED")
        frame.frame_element.return_value.is_visible.return_value = False
        self.assertEqual(main.classify(main.probe_page(page)), "QUEUE")





class ParkingExclusionTests(unittest.TestCase):
    def test_requested_examples(self):
        for text, evidence, accepted in (
            ('ESTACIONAMIENTO ARGENTINA VS BENIN', '', False),
            ('PARKING ARGENTINA BENIN', '', False),
            ('ARGENTINA C. BENIN AMISTOSO INTERNACIONAL', '', True),
            ('ARGENTINA VS BENÍN', '', True),
            ('Estacionamiento', '/event/argentina-benin', False),
            ('Argentina vs Benin', 'Contexto padre: Estacionamiento', False),
        ):
            with self.subTest(text=text, evidence=evidence):
                self.assertEqual(main.visual_card_match(text, evidence)['accepted'], accepted)

    def test_every_card_source_overrides_countries(self):
        for term in main.EXCLUDED_TARGET_TERMS:
            for field in ('text', 'context', 'aria_label', 'title', 'href'):
                card = {'text': 'Argentina vs Benín', field: term}
                with self.subTest(term=term, field=field):
                    self.assertFalse(main.home_target_verified(card))
            for field in ('alt', 'title', 'aria_label', 'src', 'filename'):
                card = {'text': 'Argentina vs Benín', 'images': [{field: term}]}
                self.assertFalse(main.home_target_verified(card))
            card = {'text': 'Argentina vs Benín', 'ancestors': [{'text': term, 'event_links': 1}]}
            self.assertFalse(main.home_target_verified(card))

    def test_final_card_guard_rejects_changed_parent_without_click(self):
        anchor = MagicMock()
        anchor.evaluate.return_value = 'Argentina vs Benin Estacionamiento'
        with patch.object(main, 'log_watch') as log, patch.object(main, 'save_validated_event') as save:
            self.assertFalse(main.enter_verified_card(MagicMock(), anchor,
                             {'href': '/event/argentina-benin', 'ocr_text': 'Argentina vs Benin'}, 0))
        anchor.click.assert_not_called()
        save.assert_not_called()
        self.assertIn('reason=parking_or_estacionamiento', log.call_args.args[0])

    def test_hot_guard_blocks_late_metadata_change(self):
        page, logger, locator = MagicMock(), MagicMock(), MagicMock()
        url = 'https://www.deportick.com/event/argentina-benin'
        data = {'url': url, 'title': 'Argentina vs Benin', 'buttons': [{'text': 'Comprar'}]}
        locator.evaluate.return_value = 'Comprar parking'
        with patch.object(main, 'probe_page', return_value=data), \
             patch.object(main, 'hot_action_locator', return_value=(locator, 'Comprar')):
            state, reason, _ = main.hot_step(page, url, logger, set())
        self.assertEqual((state, reason), ('UNKNOWN', 'parking_or_estacionamiento'))
        locator.click.assert_not_called()
        logger.emit.assert_called_with('TARGET REJECTED | reason=parking_or_estacionamiento')

    def test_saved_parking_target_is_invalid(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(main, 'ARTIFACTS', Path(folder)), \
             patch.object(main, 'log_watch'):
            (Path(folder) / 'target_event.json').write_text(json.dumps({
                'verified': True, 'keywords': ['Argentina', 'Benin'],
                'url': 'https://www.deportick.com/event/estacionamientosbenin'}))
            self.assertIsNone(main.validated_event_url())

    def test_event_exclusion_overrides_positive_heading(self):
        data = {'title': 'Argentina vs Benin', 'body_text': 'ESTACIONAMIENTO OFICIAL'}
        self.assertEqual(main.verify_event_identity(data), 'REJECTED')
        self.assertFalse(main.event_identity_matches(data))


if __name__ == "__main__":
    unittest.main()
