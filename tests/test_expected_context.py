"""Offline, synthetic regressions for intentional context changes (issue #12)."""
import copy
import os
import tempfile
import unittest
from unittest import mock

from test_dgc import TMP, dgc, rollout_lines, run_cli, tokens


class ExpectedContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        paths = {"NERFED_HOME": root, "SESSIONS_DIR": os.path.join(root, "sessions"),
                 "PROBES_DIR": os.path.join(root, "probes")}
        for name in ("CONFIG_PATH", "EVENTS_PATH", "PROBES_INDEX", "ERRORS_PATH", "WORKER_LOG",
                     "LOG_PATH", "ACCOUNT_PATH", "HOOKS_STATUS_PATH", "CODEX_VERSIONS_PATH",
                     "UPDATE_PATH", "STATE_PATH"):
            paths[name] = os.path.join(root, name.lower() + ".json")
        for name, value in paths.items():
            patch = mock.patch.object(dgc, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.account = {"id": "synthetic-account", "label": "Synthetic", "plan": None}
        for name, value in (("note_account", self.account), ("current_account", self.account),
                            ("db_recent_threads_detailed", []), ("db_thread", None),
                            ("hooks_status", {}), ("codex_bin", None), ("hidden_models", frozenset()),
                            ("plugin_enabled_in_config", False), ("default_model_settings", ("gpt-6-astra", "max")),
                            ("maybe_check_updates", {})):
            patch = mock.patch.object(dgc, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        dgc.ensure_dirs()
        self.cfg = {**dgc.DEFAULT_CONFIG, "frequency": "manual", "fresh_frequency": "manual",
                    "notify": False, "sound": False, "check_updates": False}
        dgc.save_config(self.cfg)

    def finding(self, before=828000, after=568480, ts="2026-10-27T00:00:00Z", **extra):
        return {"kind": "context_window_change", "severity": "hard", "ts": ts,
                "detail": f"model context window {before} → {after}", "turn_id": None, **extra}

    def session(self, evidence, sid="synthetic-thread"):
        st = dgc.new_session(sid)
        st.update(kind="main", model="gpt-6-astra", evidence=copy.deepcopy(evidence))
        dgc.save_session(st)
        return sid

    def entries(self, sid):
        ev = dgc.load_session(sid)["evidence"]
        return dgc.evidence_entries(ev, dgc.active_evidence(ev))

    def mark(self, sid, finding_id, undo=False):
        return run_cli(["evidence-expected", "--thread", sid, "--evidence", finding_id]
                       + (["--undo"] if undo else []))

    def confirm(self, sid, index=0):
        row = self.entries(sid)[index]
        self.assertTrue(row.get("id"), "snapshot evidence needs an occurrence ID")
        rc, out = self.mark(sid, row["id"])
        self.assertEqual(rc, 0, out)
        return row["id"]

    def snapshot(self):
        return dgc.build_snapshot(self.cfg)

    def test_confirmation_persists_and_undo_preserves_original_evidence(self):
        original = self.finding()
        sid = self.session([original])
        eid = self.confirm(sid)
        saved = dgc.load_session(sid)["evidence"][0]
        for key, value in original.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(saved["expected"]["reason"], "user_confirmed_context_change")
        self.assertTrue(any(dgc.parse_iso(v) for k, v in saved["expected"].items() if k != "reason"),
                        "confirmation needs a persisted timestamp")
        self.assertEqual(dgc.active_evidence([saved]), [])
        self.assertEqual(self.entries(sid)[0]["id"], eid)
        self.assertEqual(self.mark(sid, eid)[0], 0, "repeating confirmation is harmless")
        self.assertEqual(self.mark(sid, eid, undo=True)[0], 0)
        restored = dgc.load_session(sid)["evidence"][0]
        self.assertNotIn("expected", restored)
        self.assertEqual(dgc.active_evidence([restored]), [restored])
        self.assertEqual(self.entries(sid)[0]["id"], eid)

    def test_latest_expected_does_not_resurrect_previous_context_alert(self):
        sid = self.session([self.finding(), self.finding(568480, 400000, "2026-10-27T00:00:01Z")])
        self.confirm(sid)
        self.assertEqual(dgc.active_evidence(dgc.load_session(sid)["evidence"]), [])

    def test_legacy_identical_rows_get_distinct_stable_ids_and_mark_exactly_one(self):
        sid = self.session([self.finding(), self.finding()])
        ids = [e["id"] for e in self.entries(sid)]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(ids, [e["id"] for e in self.entries(sid)])
        self.assertEqual(self.mark(sid, ids[1])[0], 0)
        ev = dgc.load_session(sid)["evidence"]
        self.assertTrue(ev[0].get("expected"))
        self.assertFalse(ev[1].get("expected"))
        self.assertEqual(dgc.active_evidence(ev), [ev[1]])
        self.assertEqual([e["active"] for e in self.entries(sid)], [True, False])

    def test_rejects_unknown_stale_and_ambiguous_ids_without_modifying_evidence(self):
        sid = self.session([self.finding()])
        stale_id = self.entries(sid)[0]["id"]
        st = dgc.load_session(sid)
        st["evidence"] = [self.finding(400000, 200000, "2026-10-27T00:00:02Z")]
        dgc.save_session(st)
        for bad in ("does-not-exist", stale_id):
            before = dgc.load_session(sid)["evidence"]
            self.assertNotEqual(self.mark(sid, bad)[0], 0)
            self.assertEqual(dgc.load_session(sid)["evidence"], before)
        sid = self.session([self.finding(id="collision"), self.finding(id="collision")])
        before = dgc.load_session(sid)["evidence"]
        self.assertNotEqual(self.mark(sid, "collision")[0], 0)
        self.assertEqual(dgc.load_session(sid)["evidence"], before)

    def test_confirmation_is_scoped_to_exact_thread(self):
        a = self.session([self.finding()], "thread-a")
        b = self.session([self.finding()], "thread-ab")
        self.confirm(a)
        self.assertFalse(dgc.load_session(b)["evidence"][0].get("expected"))
        self.assertEqual(len(dgc.active_evidence(dgc.load_session(b)["evidence"])), 1)
        before = dgc.load_session(b)["evidence"]
        self.assertNotEqual(self.mark("thread", self.entries(b)[0]["id"])[0], 0)
        self.assertEqual(dgc.load_session(b)["evidence"], before)

    def test_cannot_confirm_other_finding_kinds_or_nonhard_context(self):
        for kind, severity in (("silent_model_change", "hard"), ("silent_effort_change", "hard"),
                               ("context_window_change", "soft"), ("context_window_change", "good")):
            with self.subTest(kind=kind, severity=severity):
                sid = self.session([self.finding(kind=kind, severity=severity)])
                before = dgc.load_session(sid)["evidence"]
                eid = self.entries(sid)[0]["id"]
                self.assertNotEqual(self.mark(sid, eid)[0], 0)
                self.assertEqual(dgc.load_session(sid)["evidence"], before)

    def test_confirmation_keeps_other_dimensions_active(self):
        other = [self.finding(kind="silent_model_change", detail="model gpt-6-astra → gpt-5.6-luna with no settings change (downgrade)"),
                 self.finding(kind="silent_effort_change", detail="reasoning effort max → low with no settings change")]
        sid = self.session(other + [self.finding()])
        self.confirm(sid)
        active = dgc.active_evidence(dgc.load_session(sid)["evidence"])
        self.assertEqual({e["kind"] for e in active}, {"silent_model_change", "silent_effort_change"})
        self.assertTrue(self.snapshot()["threads"][0]["alert"])

    def test_expected_history_is_not_labelled_reverted_and_counts_clear(self):
        sid = self.session([self.finding(), self.finding(568480, 400000, "2026-10-27T00:00:01Z")])
        self.confirm(sid)
        snap = self.snapshot()
        row = snap["threads"][0]
        self.assertFalse(row["alert"])
        self.assertEqual(row["hard_evidence"], 0)
        self.assertEqual(snap["overall"]["downgraded"], 0)
        self.assertEqual(len(row["evidence"]), 2)
        self.assertTrue(row["evidence"][0].get("expected"))
        self.assertFalse(row["evidence"][0]["active"])
        lines = [line for line in row["report_text"].splitlines() if line.startswith("Evidence")]
        self.assertIn("expected", lines[0].lower())
        self.assertNotIn("reverted", lines[0].lower())
        self.assertIn("reverted", lines[1].lower())

    def test_later_same_drop_has_new_id_and_remains_active_without_timestamps(self):
        for timestamp in (None, "2026-10-27T00:00:00Z"):
            with self.subTest(timestamp=timestamp):
                def token(value):
                    event = tokens(value)
                    if timestamp:
                        event["timestamp"] = timestamp
                    return event
                path = os.path.join(self.tmp.name, "synthetic-rollout.jsonl")
                with open(path, "w") as f:
                    f.write(rollout_lines(token(828000), token(568480)))
                scan = dgc.new_scan_state()
                first = dgc.scan_rollout(path, scan, frozenset())
                sid = self.session(first)
                eid = self.confirm(sid)
                with open(path, "a") as f:
                    f.write(rollout_lines(token(828000), token(568480)))
                later = dgc.scan_rollout(path, scan, frozenset())
                self.assertEqual([e["severity"] for e in later], ["info", "hard"])
                st = dgc.load_session(sid)
                st["evidence"].extend(later)
                dgc.save_session(st)
                self.assertEqual(dgc.active_evidence(st["evidence"]), [later[-1]])
                self.assertNotEqual(self.entries(sid)[0]["id"], eid)
                self.assertEqual(dgc.scan_rollout(path, scan, frozenset()), [])
                self.assertTrue(self.snapshot()["threads"][0]["alert"])

    def probe_record(self, sid, results, verdict="DOWNGRADED!", direction="hard"):
        rec = {"id": "synthetic-probe", "thread_id": sid, "mode": "fork", "status": "done",
               "finished": dgc.iso(), "expected": "gpt-6-astra", "prediction": results[0]["model"] if results else "gpt-6-astra",
               "probability": 1.0, "used_outputs": 3, "queries": 3, "verdict": verdict, "direction": direction,
               "account_id": self.account["id"], "account": self.account,
               "hard_evidence": copy.deepcopy(dgc.load_session(sid)["evidence"]), "results": results}
        dgc.write_json(dgc.probe_path(rec["id"]), rec)
        dgc.append_jsonl(dgc.PROBES_INDEX, {k: v for k, v in rec.items() if k not in ("results", "hard_evidence", "account")})
        return rec

    def test_old_passive_only_downgrade_clears_without_rewriting_probe_history(self):
        sid = self.session([self.finding()])
        rec = self.probe_record(sid, [{"model": "gpt-6-astra", "probability": 1.0, "score": 2.0}])
        self.assertTrue(self.snapshot()["threads"][0]["alert"])
        self.confirm(sid)
        snap = self.snapshot()
        self.assertFalse(snap["threads"][0]["alert"])
        self.assertEqual(snap["overall"]["downgraded"], 0)
        self.assertEqual(dgc.read_json(dgc.probe_path(rec["id"])), rec, "historical probe must remain intact")
        self.assertEqual(list(dgc.iter_jsonl(dgc.PROBES_INDEX))[0]["verdict"], "DOWNGRADED!")

    def test_saved_fingerprint_mismatch_still_alerts_after_context_confirmation(self):
        results = [{"model": "gpt-5.6-luna", "probability": 0.99, "score": 2.0},
                   {"model": "gpt-6-astra", "probability": 0.01, "score": 0.0}]
        for verdict, direction in (("DOWNGRADED!", "hard"), ("MISMATCH", "downgrade")):
            with self.subTest(verdict=verdict):
                if os.path.exists(dgc.PROBES_INDEX):
                    os.remove(dgc.PROBES_INDEX)
                sid = self.session([self.finding()])
                self.probe_record(sid, results, verdict, direction)
                self.confirm(sid)
                snap = self.snapshot()
                self.assertTrue(snap["threads"][0]["alert"])
                self.assertEqual(snap["overall"]["downgraded"], 1)

    def test_incomplete_old_fingerprint_data_retains_alert_conservatively(self):
        for results in ([], [{"model": "gpt-6-astra", "probability": 1.0}]):
            with self.subTest(results=results):
                if os.path.exists(dgc.PROBES_INDEX):
                    os.remove(dgc.PROBES_INDEX)
                sid = self.session([self.finding()])
                self.probe_record(sid, results)
                self.confirm(sid)
                self.assertTrue(self.snapshot()["threads"][0]["alert"])


    def test_malformed_legacy_hard_evidence_retains_warning_without_crashing(self):
        sid = self.session([self.finding()])
        rec = self.probe_record(sid, [{"model": "gpt-6-astra", "probability": 1.0, "score": 2.0}])
        self.confirm(sid)
        evidence = dgc.load_session(sid)["evidence"]
        summary = {"id": rec["id"], "verdict": "DOWNGRADED!", "is_downgrade": True}
        for hard in (None, {}, "invalid", [], [None], ["invalid"], [12], [self.finding(), None]):
            with self.subTest(hard=hard):
                dgc.write_json(dgc.probe_path(rec["id"]), {**rec, "hard_evidence": hard})
                self.assertEqual(dgc.expected_probe_projection(summary, evidence, self.cfg), summary)
                self.assertTrue(self.snapshot()["threads"][0]["alert"])
        for full in (["not-a-record"], "not-a-record", 12):
            with self.subTest(full=full):
                dgc.write_json(dgc.probe_path(rec["id"]), full)
                self.assertEqual(dgc.expected_probe_projection(summary, evidence, self.cfg), summary)

    def test_invalid_legacy_fingerprint_values_never_project_a_match(self):
        sid = self.session([self.finding()])
        candidate = {"model": "gpt-6-astra", "probability": 1.0, "score": 2.0}
        rec = self.probe_record(sid, [candidate])
        self.confirm(sid)
        evidence = dgc.load_session(sid)["evidence"]
        summary = {"id": rec["id"], "verdict": "DOWNGRADED!", "is_downgrade": True}
        bad_results = [None, {}, "invalid", [None], ["invalid"]]
        for key, values in (("probability", (-0.01, 1.01, float("nan"), float("inf"), True, "1", 10 ** 1000)),
                            ("score", (float("nan"), float("-inf"), True, "2", 10 ** 1000)),
                            ("model", (None, 42, "", "   "))):
            bad_results.extend([{**candidate, key: value}] for value in values)
        bad_results.extend([
            [candidate, dict(candidate)],
            [{**candidate, "probability": 0.1},
             {"model": "gpt-5.6-luna", "probability": 0.9, "score": 3.0}],
            [{**candidate, "probability": 0.9, "score": 1.0},
             {"model": "gpt-5.6-luna", "probability": 0.1, "score": 3.0}],
        ])
        for index, results in enumerate(bad_results):
            with self.subTest(case=index):
                dgc.write_json(dgc.probe_path(rec["id"]), {**rec, "results": results})
                self.assertEqual(dgc.expected_probe_projection(summary, evidence, self.cfg), summary)
        for expected in (None, 42, "", "   "):
            with self.subTest(expected=expected):
                dgc.write_json(dgc.probe_path(rec["id"]), {**rec, "expected": expected})
                self.assertEqual(dgc.expected_probe_projection(summary, evidence, self.cfg), summary)
        for used in (None, True, 0, -1, 1.5, "3"):
            with self.subTest(used=used):
                dgc.write_json(dgc.probe_path(rec["id"]), {**rec, "used_outputs": used})
                self.assertEqual(dgc.expected_probe_projection(summary, evidence, self.cfg), summary)

    def queued_alert(self, sid, verdict="DOWNGRADED!"):
        st = dgc.load_session(sid)
        alert = {"ts": dgc.iso(), "probe": "synthetic-probe", "verdict": verdict,
                 "text": "Synthetic downgrade warning", "delivered": False}
        st["alerts"] = [alert]
        st["halt"] = {"id": "synthetic-probe", "ts": dgc.iso(), "detail": "Synthetic existing halt"}
        dgc.save_session(st)
        return copy.deepcopy(alert), copy.deepcopy(st["halt"])

    def test_confirmed_pending_alert_is_suppressed_until_undo_and_halt_is_untouched(self):
        sid = self.session([self.finding()])
        self.probe_record(sid, [{"model": "gpt-6-astra", "probability": 1.0, "score": 2.0}])
        alert, halt = self.queued_alert(sid)
        eid = self.confirm(sid)
        for model_visible in (False, True):
            with self.subTest(model_visible=model_visible):
                st = dgc.load_session(sid)
                output = {}
                dgc.deliver_alerts(st, output, model_visible, self.cfg)
                self.assertEqual(output, {})
                self.assertEqual(st["alerts"], [alert], "suppression must preserve the queued record")
                self.assertEqual(st["halt"], halt)
        self.assertEqual(self.mark(sid, eid, undo=True)[0], 0)
        st = dgc.load_session(sid)
        output = {}
        dgc.deliver_alerts(st, output, True, self.cfg)
        self.assertIn(alert["text"], output["systemMessage"])
        self.assertIn(alert["text"], output["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(st["alerts"][0]["delivered"])
        self.assertEqual(st["halt"], halt)

    def test_real_mismatch_pending_alert_still_delivers_after_confirmation(self):
        results = [{"model": "gpt-5.6-luna", "probability": 0.99, "score": 2.0},
                   {"model": "gpt-6-astra", "probability": 0.01, "score": 0.0}]
        for verdict, direction in (("MISMATCH", "downgrade"), ("DOWNGRADED!", "hard")):
            with self.subTest(verdict=verdict):
                sid = self.session([self.finding()])
                self.probe_record(sid, results, verdict, direction)
                alert, halt = self.queued_alert(sid, verdict)
                self.confirm(sid)
                st = dgc.load_session(sid)
                output = {}
                dgc.deliver_alerts(st, output, True, self.cfg)
                self.assertIn(alert["text"], output["systemMessage"])
                self.assertTrue(st["alerts"][0]["delivered"])
                self.assertEqual(st["halt"], halt)

    def test_pending_alert_suppression_does_not_cross_thread_identity(self):
        sid = self.session([self.finding()])
        rec = self.probe_record(sid, [{"model": "gpt-6-astra", "probability": 1.0, "score": 2.0}])
        alert, _ = self.queued_alert(sid)
        self.confirm(sid)
        dgc.write_json(dgc.probe_path(rec["id"]), {**rec, "thread_id": "another-thread"})
        st = dgc.load_session(sid)
        output = {}
        dgc.deliver_alerts(st, output, True, self.cfg)
        self.assertIn(alert["text"], output["systemMessage"])
        self.assertTrue(st["alerts"][0]["delivered"])

    def test_projected_expanded_report_explains_historical_downgrade(self):
        sid = self.session([self.finding()])
        rec = self.probe_record(sid, [{"model": "gpt-6-astra", "probability": 1.0, "score": 2.0}])
        self.confirm(sid)
        snap = self.snapshot()
        row = snap["threads"][0]
        for projection in (row["last_probe"], row["probes"][0], snap["last_verdict"]):
            self.assertEqual(projection["verdict"], "MATCH")
            self.assertEqual(projection["historical_verdict"], "DOWNGRADED!")
        report = row["report_text"]
        self.assertIn("Last probe · Match", report)
        self.assertIn("historical DOWNGRADED!", report)
        self.assertIn("context change confirmed as expected", report)
        self.assertEqual(dgc.read_json(dgc.probe_path(rec["id"])), rec)

if __name__ == "__main__":
    unittest.main()
