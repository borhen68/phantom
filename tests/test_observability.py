import os
import tempfile
import unittest
from unittest import mock

from core.observability import TraceRecorder, replay_trace


class ObservabilityTests(unittest.TestCase):
    def test_trace_replay_returns_recorded_events(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"PHANTOM_HOME": home}, clear=False):
                recorder = TraceRecorder(goal="trace test")
                recorder.record("start", {"goal": "trace test"}, agent="orchestrator")
                recorder.record("done", {"outcome": "success"}, agent="orchestrator")

                events = replay_trace(recorder.trace_id)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["trace_id"], recorder.trace_id)
        self.assertEqual(events[1]["event_type"], "done")


if __name__ == "__main__":
    unittest.main()
