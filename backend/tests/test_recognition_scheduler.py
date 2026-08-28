import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from recognition_scheduler import ParallelTextRecognizer


class _FakeInferRequest:
    def __init__(self, compiled_model):
        self._compiled_model = compiled_model

    def get_compiled_model(self):
        return self._compiled_model

    def infer(self, inputs):
        tensor = np.asarray(inputs[0], dtype=np.float32)
        with self._compiled_model.lock:
            self._compiled_model.active += 1
            self._compiled_model.maximum_active = max(
                self._compiled_model.maximum_active,
                self._compiled_model.active,
            )
            self._compiled_model.seen_batches.append(tuple(tensor[:, 0, 0, 0]))
        time.sleep(0.02)
        with self._compiled_model.lock:
            self._compiled_model.active -= 1
        return {"output": tensor[:, :1, :1, :1]}


class _FakeCompiledModel:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0
        self.seen_batches = []

    def create_infer_request(self):
        return _FakeInferRequest(self)


class _FakeRecognizer:
    RTL_LANGS = ()

    def __init__(self, rec_batch_num):
        self.rec_batch_num = rec_batch_num
        self.rec_image_shape = (3, 48, 320)
        self.cfg = SimpleNamespace(
            lang_type="ch",
            font_path="",
            model_path="fake.onnx",
        )
        self.compiled_model = _FakeCompiledModel()
        self.session = SimpleNamespace(
            session=_FakeInferRequest(self.compiled_model)
        )

    @staticmethod
    def resize_norm_img(image, max_wh_ratio):
        del max_wh_ratio
        value = float(image[0, 0, 0])
        return np.full((3, 1, 1), value, dtype=np.float32)

    @staticmethod
    def postprocess_op(prediction, return_word_box, **kwargs):
        del return_word_box, kwargs
        values = np.asarray(prediction)[:, 0, 0, 0]
        lines = [(str(int(value)), float(value) / 100.0) for value in values]
        return lines, []


def _image(width, value):
    return np.full((10, width, 3), value, dtype=np.uint8)


class ParallelTextRecognizerTests(unittest.TestCase):
    def test_parallel_requests_keep_single_image_batches_and_original_order(self):
        recognizer = _FakeRecognizer(rec_batch_num=1)
        scheduler = ParallelTextRecognizer(recognizer, request_count=2)
        images = [_image(30, 30), _image(10, 10), _image(20, 20)]
        jobs = scheduler._batch_jobs(images)
        results = [(('', 0.0), None)] * len(images)

        with patch.object(scheduler, "_active_request_count", return_value=2):
            scheduler._run_jobs(jobs, images, False, results)

        self.assertEqual(
            [result[0][0] for result in results],
            ["30", "10", "20"],
        )
        self.assertEqual(
            sorted(recognizer.compiled_model.seen_batches),
            [(10.0,), (20.0,), (30.0,)],
        )
        self.assertEqual(recognizer.compiled_model.maximum_active, 2)

    def test_parallel_requests_keep_medium_ordered_batch_members(self):
        recognizer = _FakeRecognizer(rec_batch_num=2)
        scheduler = ParallelTextRecognizer(recognizer, request_count=2)
        images = [
            _image(40, 40),
            _image(10, 10),
            _image(30, 30),
            _image(20, 20),
        ]
        jobs = scheduler._batch_jobs(images)
        results = [(('', 0.0), None)] * len(images)

        with patch.object(scheduler, "_active_request_count", return_value=2):
            scheduler._run_jobs(jobs, images, False, results)

        self.assertEqual(
            [result[0][0] for result in results],
            ["40", "10", "30", "20"],
        )
        self.assertEqual(
            sorted(recognizer.compiled_model.seen_batches),
            [(10.0, 20.0), (30.0, 40.0)],
        )

    def test_memory_fallback_runs_the_same_jobs_serially(self):
        recognizer = _FakeRecognizer(rec_batch_num=1)
        scheduler = ParallelTextRecognizer(recognizer, request_count=2)
        images = [_image(20, 20), _image(10, 10)]
        jobs = scheduler._batch_jobs(images)
        results = [(('', 0.0), None)] * len(images)

        with patch.object(scheduler, "_active_request_count", return_value=1):
            scheduler._run_jobs(jobs, images, False, results)

        self.assertEqual([result[0][0] for result in results], ["20", "10"])
        self.assertEqual(recognizer.compiled_model.maximum_active, 1)


if __name__ == "__main__":
    unittest.main()
