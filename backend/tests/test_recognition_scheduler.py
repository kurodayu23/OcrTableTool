import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from recognition_scheduler import ParallelTextRecognizer, build_parallel_text_recognizer


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
        self.created_requests = 0

    def create_infer_request(self):
        self.created_requests += 1
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
    def test_release_closes_workers_and_drops_all_native_requests(self):
        recognizer = _FakeRecognizer(rec_batch_num=1)
        scheduler = ParallelTextRecognizer(recognizer, request_count=2)
        images = [_image(10, 10), _image(20, 20)]
        results = [(('', 0.0), None)] * len(images)
        with patch.object(scheduler, "_active_request_count", return_value=2):
            scheduler._run_jobs(scheduler._batch_jobs(images), images, False, results)
        self.assertEqual(scheduler._request_pool.qsize(), 2)

        self.assertTrue(scheduler.release_native_resources())

        self.assertIsNone(scheduler._compiled_model)
        self.assertTrue(scheduler._request_pool.empty())
        self.assertTrue(all(not worker.is_alive() for worker in scheduler._executor._threads))
        self.assertFalse(scheduler.release_native_resources())
        with self.assertRaisesRegex(RuntimeError, "released"):
            scheduler(SimpleNamespace(img=images, return_word_box=False))

    def test_factory_respects_compiled_model_request_budget(self):
        recognizer = _FakeRecognizer(rec_batch_num=2)
        with patch.object(recognizer.compiled_model, "get_property", return_value=1, create=True):
            configured = build_parallel_text_recognizer(recognizer, request_count=2)
        self.assertIs(configured, recognizer)
        self.assertEqual(recognizer.compiled_model.created_requests, 0)
        self.assertEqual(recognizer.rec_batch_num, 2)

    def test_factory_keeps_compatible_fallback_when_property_is_unavailable(self):
        recognizer = _FakeRecognizer(rec_batch_num=2)
        with patch.object(recognizer.compiled_model, "get_property", side_effect=RuntimeError, create=True):
            configured = build_parallel_text_recognizer(recognizer, request_count=2)
        self.assertIsInstance(configured, ParallelTextRecognizer)

    def test_extra_request_is_allocated_only_for_a_parallel_batch(self):
        recognizer = _FakeRecognizer(rec_batch_num=2)
        scheduler = ParallelTextRecognizer(recognizer, request_count=2)
        self.assertEqual(recognizer.compiled_model.created_requests, 0)
        images = [_image(10, 10), _image(20, 20)]
        results = [(('', 0.0), None)] * len(images)
        with patch.object(scheduler, "_active_request_count", return_value=2):
            scheduler._run_jobs(scheduler._batch_jobs(images), images, False, results)
        self.assertEqual(recognizer.compiled_model.created_requests, 0)
        images += [_image(30, 30), _image(40, 40)]
        results = [(('', 0.0), None)] * len(images)
        with patch.object(scheduler, "_active_request_count", return_value=2):
            scheduler._run_jobs(scheduler._batch_jobs(images), images, False, results)
        self.assertEqual(recognizer.compiled_model.created_requests, 1)
        self.assertEqual([result[0][0] for result in results], ["10", "20", "30", "40"])

    def test_extra_request_allocation_failure_preserves_serial_results(self):
        recognizer = _FakeRecognizer(rec_batch_num=1)
        scheduler = ParallelTextRecognizer(recognizer, request_count=2)
        images = [_image(20, 20), _image(10, 10)]
        results = [(('', 0.0), None)] * len(images)
        with patch.object(recognizer.compiled_model, "create_infer_request", side_effect=MemoryError), patch.object(
            scheduler, "_active_request_count", return_value=2
        ):
            scheduler._run_jobs(scheduler._batch_jobs(images), images, False, results)
        self.assertEqual([result[0][0] for result in results], ["20", "10"])
        self.assertEqual(recognizer.compiled_model.maximum_active, 1)

    def test_empty_batch_returns_empty_result_without_inference(self):
        recognizer = _FakeRecognizer(rec_batch_num=2)
        scheduler = ParallelTextRecognizer(recognizer, request_count=2)
        result = scheduler(SimpleNamespace(img=[], return_word_box=False))
        self.assertEqual(list(result.txts), [])
        self.assertEqual(list(result.scores), [])
        self.assertEqual(recognizer.compiled_model.seen_batches, [])

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
