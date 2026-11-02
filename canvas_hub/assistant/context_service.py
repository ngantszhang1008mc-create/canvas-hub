"""Compaction lifecycle shared by manual action and automatic budget checks."""
from __future__ import annotations

import json
import threading

from ..util import InvalidInput, now_iso
from .context import (Policy, SUMMARY_SYSTEM, context_error, data_version, encoded,
                      estimate, neutral_message, parse_summary, split_for_summary, summary_message)
from .providers import ProviderError, Stopped


class ContextService:
    def _context_projection(self, cid, account_id, transcript):
        row = self.hub.db.one("SELECT * FROM assistant_context WHERE conversation_id=?", (cid,))
        if not row or row["data_version"] != data_version(self.hub, account_id):
            return transcript
        tail = [m for m in transcript if m.get("seq", float("inf")) > row["through_seq"]]
        return [summary_message(json.loads(row["summary_json"])), *tail]

    def _compact_context(self, cid, account_id, cfg, transcript, make_provider):
        policy = Policy.for_config(cfg)
        prefix, tail = split_for_summary(transcript, policy.keep_turns)
        through = max(m.get("seq", 0) for m in prefix)
        # Preserve exact identifiers independent of summary quality. Each call's
        # arguments are already bounded by the same request budget. Do not trust
        # a model to reconstruct a long resource ID or a stale-write token.
        anchors, executions = [], []
        reference_keys = {"id", "note_id", "item_id", "course_id", "resource_id", "component_id", "source_id",
                          "version", "expected_version", "state_token", "offset", "start_char", "end_char"}

        def refs(value):
            if isinstance(value, dict):
                found = {k: v for k, v in value.items() if k in reference_keys}
                if found:
                    anchors.append(found)
                for val in value.values():
                    if isinstance(val, (dict, list)):
                        refs(val)
            elif isinstance(value, list):
                for val in value:
                    refs(val)

        for m in prefix:
            refs(m.get("tool_calls", []))
            if m.get("context_summary"):
                # Previous deterministic references are kept in the summary
                # input and copied separately rather than recursively growing.
                previous = self.hub.db.one("SELECT summary_json FROM assistant_context WHERE conversation_id=?", (cid,))
                if previous:
                    previous_summary = json.loads(previous["summary_json"])
                    anchors.extend(previous_summary.get("reference_manifest", []))
                    executions.extend(previous_summary.get("tool_execution_manifest", []))
            for result in m.get("results", []):
                error = None
                try:
                    payload = json.loads(result["content"])
                    refs(payload)
                    error = payload.get("error") if isinstance(payload, dict) else None
                except (ValueError, TypeError):
                    pass
                executions.append({"result_seq": m.get("seq"), "call_id": result["id"], "tool": result["name"],
                                   "outcome": "unknown" if error == "interrupted" else "error" if result.get("is_error") else "completed",
                                   "error": error})
        anchors = list({encoded(x): x for x in anchors}.values())
        # A large manifest must not silently consume the whole working budget.
        if estimate(anchors) + estimate(executions) > policy.summary_tokens:
            raise ProviderError("Too many source references for one safe compaction; the original context is retained. Start a focused chat.")
        request = [{"role": "user", "sent_text": encoded({"max_summary_tokens": policy.summary_tokens,
                                                           "conversation": [neutral_message(m) for m in prefix]})}]
        input_tokens = estimate(SUMMARY_SYSTEM) + estimate(request) + 512
        if input_tokens + policy.summary_tokens > int(policy.capacity * .9):
            raise ProviderError("Older history is too large for one safe summary request. Original context is retained; start a focused chat. No summary request was sent.")
        version = data_version(self.hub, account_id)
        source_seq = self.hub.db.one("SELECT MAX(id) AS seq FROM assistant_messages WHERE conversation_id=?", (cid,))["seq"]
        if cid in self._cancel:
            raise Stopped()
        with self.hub.db.tx() as c:
            attempt = c.execute("INSERT INTO assistant_context_usage(conversation_id,provider,model,status,estimated_input_tokens,created_at) "
                                "VALUES (?,?,?,'started',?,?)",
                                (cid, cfg["provider"], cfg.get("model"), input_tokens, now_iso())).lastrowid
        provider = None
        status, usage = "failed", None
        try:
            summary_cfg = {**cfg, "_single_attempt": True, "_response_tokens": policy.summary_tokens,
                           "extra_params": {k: v for k, v in (cfg.get("extra_params") or {}).items()
                                            if k not in ("max_tokens", "max_completion_tokens")}}
            provider = make_provider(summary_cfg)

            def check_cancel(_text):
                if cid in self._cancel:
                    raise Stopped()

            turn = provider.complete(SUMMARY_SYSTEM, request, [], on_text=check_cancel)
            usage = turn.usage
            check_cancel("")
            if turn.tool_calls or turn.stop != "end":
                raise ProviderError("Compaction did not finish with a summary; original context is retained. No summary tools were executed.")
            summary = parse_summary(turn.text, policy.summary_tokens)
            summary["reference_manifest"] = anchors
            summary["tool_execution_manifest"] = executions
            candidate = [summary_message(summary), *tail]
            # Installation is atomic and refuses concurrent data/message changes.
            with self._lock, self.hub.db.tx() as c:
                check_cancel("")
                latest = c.execute("SELECT MAX(id) AS seq FROM assistant_messages WHERE conversation_id=?", (cid,)).fetchone()["seq"]
                if latest != source_seq or data_version(self.hub, account_id) != version:
                    raise ProviderError("Data changed while compacting; the original context is retained. Read the latest sources and retry when ready.")
                if estimate([neutral_message(m) for m in candidate]) >= estimate([neutral_message(m) for m in transcript]):
                    raise ProviderError("The summary would not reduce context; original context is retained.")
                c.execute("INSERT INTO assistant_context VALUES (?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
                          "through_seq=excluded.through_seq,summary_json=excluded.summary_json,data_version=excluded.data_version,created_at=excluded.created_at",
                          (cid, through, encoded(summary), version, now_iso()))
                c.execute("UPDATE assistant_conversations SET context_notice=NULL WHERE id=?", (cid,))
                status = "completed"
            return candidate
        except Stopped:
            status = "cancelled"
            raise
        finally:
            try:
                if provider and callable(getattr(provider, "close", None)):
                    provider.close()
            finally:
                # Unknown usage (errors/cancelled streams) stays explicitly unknown,
                # not zero. A crash leaves 'started', marked interrupted on restart.
                with self.hub.db.tx() as c:
                    c.execute("UPDATE assistant_context_usage SET status=?,usage_json=?,finished_at=? WHERE id=?",
                              (status, encoded(usage) if usage else None, now_iso(), attempt))

    def _prepare_context(self, cid, account_id, cfg, system, transcript, tools, make_provider,
                         *, allow_compact=True, provider_pending=False):
        policy = Policy.for_config(cfg)
        transcript = self._context_projection(cid, account_id, transcript)
        report = policy.measure(cfg, system, transcript, tools)
        if report["overhead_tokens"] + report["response_reserved"] >= int(policy.capacity * .9):
            self._context_reports[cid] = report
            raise context_error(report)
        attempted = False
        if report["near_limit"] and policy.auto_compact and allow_compact and not provider_pending:
            attempted = True
            try:
                transcript = self._compact_context(cid, account_id, cfg, transcript, make_provider)
            except ProviderError as e:
                with self.hub.db.tx() as c:
                    c.execute("UPDATE assistant_conversations SET context_notice=? WHERE id=?", (str(e), cid))
            report = policy.measure(cfg, system, transcript, tools)
        self._context_reports[cid] = report
        if report["over_budget"]:
            raise context_error(report)
        return transcript, attempted

    def compact(self, account_id, cid, profile_id=None, reasoning=None, model=None):
        with self._cfg_lock:
            cfg, _, _ = self._message_config(profile_id, reasoning, model)
        acct = self.hub.identity.get_account(account_id)
        with self._lock:
            self._flush_pending_states()
            conv = self._conv(account_id, cid)
            if conv["run_state"] in ("running", "cancelling") or self._at_capacity():
                raise InvalidInput("Wait for the current run to finish before compacting.", code="assistant_busy")
            if conv["scope_course_id"] is not None or conv["scope_item_id"] is not None:
                fingerprint = self._disclosure_fingerprint(account_id, cfg, course_id=conv["scope_course_id"], item_id=conv["scope_item_id"])
                if not self.hub.db.one("SELECT 1 FROM workspace_disclosures WHERE account_id=? AND fingerprint=?", (account_id, fingerprint)):
                    raise InvalidInput("Review this workspace's provider and source disclosure before compacting.", code="disclosure_required")
            self._persist_partial(cid)
            # Manual compaction never repairs or executes an outstanding tool.
            try:
                split_for_summary(self._context_projection(cid, account_id, self._transcript(cid)), Policy.for_config(cfg).keep_turns)
            except ProviderError as e:
                raise InvalidInput(str(e), code="context_not_compactable") from e
            self._set_state(cid, "running")
            self._cancel.discard(cid)
            th = threading.Thread(target=self._run, args=(cid, account_id, cfg, acct, False, True),
                                  name=f"assistant-compact-{cid}", daemon=True)
            self._threads[cid] = th
            try:
                th.start()
            except Exception:
                self._threads.pop(cid, None)
                self._record_final_state(cid, "error", "Compaction could not start. Original history is retained.")
        return {"conversation_id": cid}

    def context_status(self, account_id, cid, transcript):
        row = self.hub.db.one("SELECT * FROM assistant_context WHERE conversation_id=?", (cid,))
        attempts = [dict(r) for r in self.hub.db.query("SELECT provider,model,status,usage_json,estimated_input_tokens FROM "
                                                    "assistant_context_usage WHERE conversation_id=? ORDER BY id", (cid,))]
        for attempt in attempts:
            attempt["usage"] = json.loads(attempt.pop("usage_json") or "null")
        uses = [m.get("usage") for m in transcript if m["role"] == "assistant" and (m.get("raw") or {}).get("provider") != "canvas_hub"]
        summary_uses = [a["usage"] for a in attempts]

        def totals(values):
            return {"input_tokens": sum(u.get("input_tokens") or 0 for u in values if u),
                    "output_tokens": sum(u.get("output_tokens") or 0 for u in values if u),
                    "cache_read_input_tokens": sum(u.get("cache_read_input_tokens") or 0 for u in values if u),
                    "cache_creation_input_tokens": sum(u.get("cache_creation_input_tokens") or 0 for u in values if u),
                    "unreported_requests": sum(not u or u.get("input_tokens") is None or u.get("output_tokens") is None for u in values)}

        return {"compacted_through": row["through_seq"] if row else None,
                "stale": bool(row and row["data_version"] != data_version(self.hub, account_id)),
                "summary_usage": totals(summary_uses), "total_usage": totals(uses + summary_uses),
                "usage_scope": "recorded assistant responses and compaction attempts",
                "summary_requests": len(attempts), "last_summary_status": attempts[-1]["status"] if attempts else None,
                "budget": self._context_reports.get(cid), "history_preserved": True}
