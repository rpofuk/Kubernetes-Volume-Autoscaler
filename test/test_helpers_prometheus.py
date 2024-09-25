import unittest
import responses

import main

config = {"prometheus-url": "127.0.0.1:9000"}
class TestHelpersPrometheus(unittest.TestCase):

    @responses.activate
    @_recorder.record(file_path="out.yaml")
    def test_zero(self):
        mian.run_loop(config)
        self.assertEqual(abs(0), 1)

