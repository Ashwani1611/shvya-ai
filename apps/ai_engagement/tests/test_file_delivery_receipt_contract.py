"""No Django setup, credentials, model calls or channel sends required."""
import copy
import unittest

from apps.ai_engagement.services.file_delivery_receipts import (
    FILE_ID_KEY, FILE_STATUS_KEY, SOURCE_KEY, advance_status,
    positive_id, shared_history, source_file_reference, valid_uuid,
)


class ReceiptContracts(unittest.TestCase):
    def test_positive_ids_are_strict(self):
        for value in (True, False, 1.0, [], {}, None, "1.0", "-2", 0, " 1", "١", 10**30):
            with self.subTest(value=value):
                self.assertIsNone(positive_id(value))
        self.assertEqual(positive_id("12"), 12)
        self.assertEqual(positive_id(12), 12)

    def test_uuid_validation(self):
        self.assertTrue(valid_uuid("123e4567-e89b-12d3-a456-426614174000"))
        for value in (None, [], True, "source-1"):
            self.assertFalse(valid_uuid(value))

    def test_previous_turn_file_is_not_inherited(self):
        runtime = {SOURCE_KEY: "A", FILE_ID_KEY: 12, FILE_STATUS_KEY: "sent"}
        self.assertEqual(source_file_reference(processing={}, runtime=runtime, source_id="B"), (None, "none"))

    def test_current_turn_file_is_used(self):
        runtime = {SOURCE_KEY: "A", FILE_ID_KEY: 12, FILE_STATUS_KEY: "resolved_pending_send"}
        self.assertEqual(source_file_reference(processing={}, runtime=runtime, source_id="A"), (12, "resolved_pending_send"))

    def test_explicit_empty_selection_never_resurrects_runtime_file(self):
        runtime = {SOURCE_KEY: "A", FILE_ID_KEY: 12, FILE_STATUS_KEY: "sent"}
        self.assertEqual(source_file_reference(processing={"resolved_file_document_id": None}, runtime=runtime, source_id="A"), (None, "none"))

    def test_source_receipt_precedes_runtime(self):
        self.assertEqual(source_file_reference(
            processing={"resolved_file_document_id": 8, "file_share_status": "delivered"},
            runtime={SOURCE_KEY: "B", FILE_ID_KEY: 12}, source_id="A"), (8, "delivered"))

    def test_missing_source_and_malformed_state_fail_closed(self):
        self.assertEqual(source_file_reference(processing=[], runtime=None, source_id=""), (None, "none"))
        self.assertEqual(source_file_reference(processing={}, runtime={FILE_ID_KEY: 12}, source_id=""), (None, "none"))

    def test_read_is_monotonic(self):
        for persisted in ("sent", "delivered", "failed", "queued", "read"):
            self.assertEqual(advance_status("read", persisted), "read")

    def test_delivered_is_monotonic(self):
        for persisted in ("sent", "failed", "queued", "delivered"):
            self.assertEqual(advance_status("delivered", persisted), "delivered")
        self.assertEqual(advance_status("delivered", "read"), "read")

    def test_acceptance_is_not_delivery(self):
        self.assertEqual(advance_status(None, "sent"), "sent")
        self.assertEqual(advance_status("sent", "failed"), "failed")
        self.assertEqual(advance_status("failed", "sent"), "sent")
        self.assertIsNone(advance_status(None, "queued"))
        self.assertIsNone(advance_status(None, []))

    def test_history_is_bounded_and_tolerates_malformed_items(self):
        history = [{"document_id": n + 1, "message_id": str(n)} for n in range(35)]
        history += [None, {"document_id": "bad"}, {"document_id": True}]
        result = shared_history(history, document_id=50, source_id="S", message_id="new", status="sent", now="T")
        self.assertLessEqual(len(result), 30)
        self.assertTrue(all(positive_id(x["document_id"]) for x in result))

    def test_duplicate_receipt_preserves_timestamp_and_input(self):
        history = [{"document_id": 12, "message_id": "M", "source_message_id": "S", "status": "sent", "sent_at": "T"}]
        before = copy.deepcopy(history)
        result = shared_history(history, document_id=12, source_id="S", message_id="M", status="sent", now="later")
        self.assertEqual(result, before)
        self.assertEqual(history, before)

    def test_failed_attempt_removal_preserves_other_success_for_same_document(self):
        history = [{"document_id": 12, "message_id": "old", "status": "delivered"},
                   {"document_id": 12, "message_id": "new", "status": "sent"}]
        result = shared_history(history, document_id=12, source_id="S", message_id="new", status="failed", now="T")
        self.assertEqual([x["message_id"] for x in result], ["old"])

    def test_history_records_read_without_rewriting_sent_time(self):
        history = [{"document_id": 12, "message_id": "M", "status": "sent", "sent_at": "T"}]
        result = shared_history(history, document_id=12, source_id="S", message_id="M", status="read", now="later")
        self.assertEqual((result[0]["status"], result[0]["sent_at"]), ("read", "T"))


if __name__ == "__main__":
    unittest.main()
