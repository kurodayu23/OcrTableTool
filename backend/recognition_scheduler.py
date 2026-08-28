from __future__ import annotations

import ctypes
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from queue import Queue
from typing import Any


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_physical", ctypes.c_ulonglong),
        ("available_physical", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("available_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("available_virtual", ctypes.c_ulonglong),
        ("available_extended_virtual", ctypes.c_ulonglong),
    ]


def recommended_request_count(maximum_requests: int = 2) -> int:
    # 并行请求虽然共享模型权重，但仍会复制激活值和工作缓冲区。
    # 低核心数或低内存设备保持串行，避免分页导致速度反而更慢甚至拖垮界面。
    logical_processors = max(1, os.cpu_count() or 1)
    configured = max(1, min(int(maximum_requests), logical_processors))
    if configured <= 1 or logical_processors < 8:
        return 1
    if os.name != "nt":
        return configured

    status = _MemoryStatus()
    status.length = ctypes.sizeof(_MemoryStatus)
    try:
        available = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return 1
    if not available:
        return 1
    if (
        status.available_physical < 4 * 1024**3
        or status.available_page_file < 4 * 1024**3
    ):
        return 1
    return configured


class ParallelTextRecognizer:
    """Run RapidOCR's original recognition batches on shared OpenVINO weights."""

    # 调度器可以并行执行原始批次，但不能改变批次成员、顺序或模型批大小。
    # RapidOCR 会依据每批最宽图片归一化 Mobile/Medium 输入，拆批或删除缓存命中项
    # 都可能改变模型输入。V5 Server 的批大小为 1，才允许按单图安全缓存证据。

    def __init__(
        self,
        recognizer: Any,
        *,
        request_count: int = 2,
        inference_threads: int = 10,
    ) -> None:
        import numpy as np

        logical_processors = max(1, os.cpu_count() or 1)
        self.request_count = max(1, min(int(request_count), logical_processors))
        self.inference_threads = max(
            self.request_count,
            min(int(inference_threads), logical_processors),
        )
        self._recognizer = recognizer
        self._np = np
        self._parallel_calls = 0
        self._serial_calls = 0
        self._original_batches = 0

        original_request = recognizer.session.session
        compiled_model = original_request.get_compiled_model()
        self._compiled_model = compiled_model
        # InferRequest 是可变对象，不能跨工作线程共享；请求池只共享只读的已编译权重，
        # 同时保证一次调用独占一个 InferRequest。
        self._request_pool: Queue[Any] = Queue(maxsize=self.request_count)
        self._request_pool.put(original_request)
        for _ in range(1, self.request_count):
            self._request_pool.put(compiled_model.create_infer_request())
        self._executor = ThreadPoolExecutor(
            max_workers=self.request_count,
            thread_name_prefix="ocr-recognition",
        )

        # TextRecognizer 只在 __call__ 内使用推理会话。现在由包装器接管调用，
        # 因此释放旧的单流编译请求，避免整个进程长期保留一份重复模型。
        recognizer.session = None

    @property
    def rec_batch_num(self) -> int:
        return int(self._recognizer.rec_batch_num)

    @property
    def cfg(self) -> Any:
        return self._recognizer.cfg

    def __getattr__(self, name: str) -> Any:
        return getattr(self._recognizer, name)

    def execution_profile(self) -> dict[str, int | str]:
        return {
            "scheduler": "openvino-shared-requests",
            "requests": self.request_count,
            "active_request_capacity": self._active_request_count(),
            "inference_threads": self.inference_threads,
            "recognition_batch_size": self.rec_batch_num,
            "minimum_parallel_available_mb": 4096,
            "parallel_calls": self._parallel_calls,
            "serial_calls": self._serial_calls,
            "original_batches": self._original_batches,
        }

    def reset_execution_profile(self) -> None:
        self._parallel_calls = 0
        self._serial_calls = 0
        self._original_batches = 0

    def _active_request_count(self) -> int:
        return recommended_request_count(self.request_count)

    def _batch_jobs(self, img_list: list[Any]) -> list[tuple[list[int], float, list[float]]]:
        # 完整复现 RapidOCR 的宽度排序和原始分批方式。
        # max_wh_ratio 会参与归一化，因此每个任务都是不可随意重组的证据单元，
        # 调用方不能为了提速再次拆分或合并。
        np = self._np
        width_list = [image.shape[1] / float(image.shape[0]) for image in img_list]
        indices = np.argsort(np.array(width_list))
        jobs: list[tuple[list[int], float, list[float]]] = []
        batch_num = self.rec_batch_num
        for begin in range(0, len(img_list), batch_num):
            end = min(len(img_list), begin + batch_num)
            image_indices = [int(indices[index]) for index in range(begin, end)]
            _, image_height, image_width = self._recognizer.rec_image_shape[:3]
            max_wh_ratio = image_width / image_height
            wh_ratio_list: list[float] = []
            for image_index in image_indices:
                height, width = img_list[image_index].shape[:2]
                ratio = width / float(height)
                max_wh_ratio = max(max_wh_ratio, ratio)
                wh_ratio_list.append(ratio)
            jobs.append((image_indices, max_wh_ratio, wh_ratio_list))
        return jobs

    def _infer_batch(
        self,
        job: tuple[list[int], float, list[float]],
        img_list: list[Any],
    ) -> tuple[tuple[list[int], float, list[float]], Any]:
        np = self._np
        image_indices, max_wh_ratio, _ = job
        normalized = [
            self._recognizer.resize_norm_img(img_list[index], max_wh_ratio)[np.newaxis, :]
            for index in image_indices
        ]
        input_tensor = np.concatenate(normalized).astype(np.float32)
        request = self._request_pool.get()
        try:
            outputs = request.infer(inputs=[input_tensor])
            if len(outputs) != 1:
                raise RuntimeError("text recognizer returned an unexpected output count")
            prediction = next(iter(outputs.values()))
        finally:
            # 即使推理失败也必须归还请求，否则后续调用可能因请求池条目泄漏而永久等待。
            self._request_pool.put(request)
        return job, prediction

    def _decode_batch(
        self,
        job: tuple[list[int], float, list[float]],
        prediction: Any,
        return_word_box: bool,
        results: list[tuple[Any, Any]],
    ) -> None:
        image_indices, max_wh_ratio, wh_ratio_list = job
        line_results, word_results = self._recognizer.postprocess_op(
            prediction,
            return_word_box,
            wh_ratio_list=wh_ratio_list,
            max_wh_ratio=max_wh_ratio,
        )
        if len(line_results) != len(image_indices):
            raise RuntimeError("text recognizer returned an unexpected result count")
        if return_word_box and len(word_results) != len(image_indices):
            raise RuntimeError("text recognizer returned an unexpected word result count")
        for position, one_result in enumerate(line_results):
            original_index = image_indices[position]
            # 任务可能乱序完成，结果必须写回调用方的原始图片位置，
            # 保证并行分批不会改变单元格与网格的对应关系。
            results[original_index] = (
                one_result,
                word_results[position] if return_word_box else None,
            )

    def _run_jobs(
        self,
        jobs: list[tuple[list[int], float, list[float]]],
        img_list: list[Any],
        return_word_box: bool,
        results: list[tuple[Any, Any]],
    ) -> None:
        active_requests = self._active_request_count()
        self._original_batches += len(jobs)
        if len(jobs) <= 1 or active_requests <= 1:
            self._serial_calls += 1
            for job in jobs:
                completed_job, prediction = self._infer_batch(job, img_list)
                self._decode_batch(
                    completed_job,
                    prediction,
                    return_word_box,
                    results,
                )
            return

        self._parallel_calls += 1

        next_job = 0
        pending = set()
        while next_job < len(jobs) and len(pending) < active_requests:
            pending.add(self._executor.submit(self._infer_batch, jobs[next_job], img_list))
            next_job += 1

        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                try:
                    completed_job, prediction = future.result()
                    self._decode_batch(
                        completed_job,
                        prediction,
                        return_word_box,
                        results,
                    )
                except Exception:
                    # 抛出异常前先等待已经运行的任务结束，因为它们仍占用后续调用所需的请求池对象。
                    for remaining in pending:
                        remaining.cancel()
                    if pending:
                        wait(pending)
                    raise
                if next_job < len(jobs):
                    pending.add(
                        self._executor.submit(
                            self._infer_batch,
                            jobs[next_job],
                            img_list,
                        )
                    )
                    next_job += 1

    def __call__(self, args: Any) -> Any:
        from rapidocr.ch_ppocr_rec.typings import TextRecOutput
        from rapidocr.ch_ppocr_rec.main import normalize_lang, reorder_bidi_for_display
        from rapidocr.utils.vis_res import VisRes

        np = self._np
        started = time.perf_counter()
        img_list = [args.img] if isinstance(args.img, np.ndarray) else list(args.img)
        return_word_box = bool(args.return_word_box)
        results: list[tuple[Any, Any]] = [(('', 0.0), None)] * len(img_list)
        jobs = self._batch_jobs(img_list)
        self._run_jobs(jobs, img_list, return_word_box, results)

        all_line_results, all_word_results = list(zip(*results))
        texts, scores = list(zip(*all_line_results))
        if normalize_lang(self._recognizer.cfg.lang_type) in self._recognizer.RTL_LANGS:
            texts = reorder_bidi_for_display(texts)
        return TextRecOutput(
            img_list,
            texts,
            scores,
            all_word_results,
            time.perf_counter() - started,
            viser=VisRes(
                lang_type=self._recognizer.cfg.lang_type,
                font_path=self._recognizer.cfg.font_path,
            ),
        )


def build_parallel_text_recognizer(
    recognizer: Any,
    *,
    request_count: int | None = None,
) -> Any:
    # 仅在多个 InferRequest 能够安全复用时启用包装器。
    # 回退路径保留原始 RapidOCR 对象和识别语义；这里仅是性能优化边界，
    # 绝不能成为识别结果是否有效的前提。
    logical_processors = max(1, os.cpu_count() or 1)
    if request_count is None:
        request_count = recommended_request_count(2)
    else:
        request_count = max(1, min(int(request_count), logical_processors))
    if request_count <= 1:
        return recognizer
    inference_threads = min(10, logical_processors)
    try:
        return ParallelTextRecognizer(
            recognizer,
            request_count=request_count,
            inference_threads=inference_threads,
        )
    except Exception as error:
        # OpenVINO 版本不支持时保留 RapidOCR 原始单请求识别器；
        # 回退只影响速度，不得改变识别证据。
        model_name = os.path.basename(str(recognizer.cfg.model_path))
        print(
            "Recognition scheduler disabled for {0}: {1}".format(
                model_name,
                type(error).__name__,
            ),
            file=sys.stderr,
            flush=True,
        )
        return recognizer
