from __future__ import annotations

import ctypes
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from queue import Empty, Full, Queue
from typing import Any


# 2026-11-06（lead 复查，内存增长 P1）：已创建识别器包装器的登记表。
# 目的是让上层能在**请求收尾**时查询"这次进程里已经见过多少个不同输入几何"，
# 据此决定是否释放 OpenVINO 的原生激活缓冲（动态形状模型按几何各留一份）。
_SCHEDULER_REGISTRY: list[Any] = []


def native_shape_pressure() -> int:
    """当前进程里累计出现过的不同输入几何数量（原生激活缓冲的代理指标）。"""
    total = 0
    for scheduler in _SCHEDULER_REGISTRY:
        counter = getattr(scheduler, "distinct_shape_count", None)
        if callable(counter):
            try:
                total += int(counter())
            except (TypeError, ValueError):
                continue
    return total


def reset_native_shape_pressure() -> None:
    """释放原生缓冲后清零计数，让下一轮从"这次释放之后新增了多少几何"重新计。"""
    for scheduler in _SCHEDULER_REGISTRY:
        shapes = getattr(scheduler, "_distinct_shapes", None)
        if isinstance(shapes, set):
            shapes.clear()


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
        self._created_requests = 1
        self._request_limit = self.request_count
        self._allocation_failures = 0
        # 2026-10-30（lead 复查 P2）：等待请求池的超时上限。单批推理耗时是秒级，
        # 30 秒还拿不到 InferRequest 就一定是记账漂移，必须明确失败而不是挂死。
        self._request_wait_timeout = 30.0
        # 2026-11-06（lead 复查，内存增长 P1）：OpenVINO CPU 插件对**动态形状**识别模型
        # （`x[?,3,48,?]`）按**每个不同输入几何**各留一份激活缓冲，直到编译模型被释放。
        # 实测：固定 crop 形状跑 12 轮 RSS 完全平（468→469 MB）；每轮换新形状 +8 MB/轮；
        # 单个新几何 (16,3,48,640) 就 +152 MB、(16,3,48,1280) +297 MB；同形状重跑只 +0.2 MB。
        # 每张表宽度都不同 ⇒ 几何不断新增 ⇒ RSS 无平台期（13 例 851→1356 MB）。
        # 这里**只计数**（不改变推理），供请求收尾时决定要不要释放原生缓冲。
        self._distinct_shapes: set[tuple[int, ...]] = set()
        _SCHEDULER_REGISTRY.append(self)
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
            "allocated_requests": self._created_requests,
            "request_allocation_failures": self._allocation_failures,
            "active_request_capacity": self._active_request_count(),
            "inference_threads": self.inference_threads,
            "recognition_batch_size": self.rec_batch_num,
            "minimum_parallel_available_mb": 4096,
            "parallel_calls": self._parallel_calls,
            "serial_calls": self._serial_calls,
            "original_batches": self._original_batches,
            "distinct_input_shapes": len(self._distinct_shapes),
        }

    def distinct_shape_count(self) -> int:
        """本次进程里已经出现过的**不同输入几何**数量（原生激活缓冲的代理指标）。"""
        return len(self._distinct_shapes)

    def reset_execution_profile(self) -> None:
        self._parallel_calls = 0
        self._serial_calls = 0
        self._original_batches = 0

    def release_native_resources(self) -> bool:
        if self._compiled_model is None:
            return False
        self._executor.shutdown(wait=True)
        while True:
            try:
                self._request_pool.get_nowait()
            except Empty:
                break
        self._compiled_model = None
        # 2026-10-30（lead 复查 P2）：清空请求池后 `_created_requests` 原来不复位，
        # 于是 `execution_profile()["allocated_requests"]` 之后一直报旧值（虚高），
        # 下一次扩容也会从错误基数开始。这里一并复位成"0 个已建请求"。
        self._created_requests = 0
        self._request_limit = 0
        self._allocation_failures = 0
        return True

    def _active_request_count(self) -> int:
        return min(self._request_limit, recommended_request_count(self.request_count))

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
        # 2026-11-06（lead 复查，内存增长 P1）：记录本次输入的几何形状。
        # OpenVINO CPU 插件对动态形状模型按几何各留一份激活缓冲，几何越多占得越多。
        # 这里只记账，不改变任何推理行为。
        try:
            self._distinct_shapes.add(tuple(int(value) for value in input_tensor.shape))
        except (AttributeError, TypeError, ValueError):
            pass
        # 2026-10-30（lead 复查 P2）：`get()` 原来没有超时 —— 只要请求池的记账出现
        # 任何漂移（例如扩容竞态、异常路径少还一次），这里就**永久阻塞**，而 60 秒
        # 墙钟是协作式检查点、只在阶段边界触发，救不了阻塞在队列上的调用
        # ⇒ 客户端只能等到自己超时（实测 150 秒）。
        # 这里给一个远大于单批推理耗时的超时：拿不到请求就明确失败，
        # 让上层按 INTERNAL_ERROR 收场，而不是把整条请求挂死。
        try:
            request = self._request_pool.get(timeout=self._request_wait_timeout)
        except Empty:
            raise RuntimeError(
                "recognition request pool exhausted (%.1fs); refusing to block forever"
                % self._request_wait_timeout
            ) from None
        try:
            outputs = request.infer(inputs=[input_tensor])
            if len(outputs) != 1:
                raise RuntimeError("text recognizer returned an unexpected output count")
            prediction = next(iter(outputs.values()))
        finally:
            # 即使推理失败也必须归还请求，否则后续调用可能因请求池条目泄漏而永久等待。
            try:
                self._request_pool.put(request, timeout=self._request_wait_timeout)
            except Full:
                # 池子已经满 = 记账漂移，不能在这里死等；丢一个条目比挂死整条请求好。
                pass
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
        active_requests = min(self._active_request_count(), self._request_limit, len(jobs))
        self._original_batches += len(jobs)
        # 单批识别不分配第二套工作缓冲；分配失败时仍完整执行原始批次。
        while self._created_requests < active_requests:
            try:
                request = self._compiled_model.create_infer_request()
            except Exception:
                self._allocation_failures += 1
                self._request_limit = self._created_requests
                active_requests = self._created_requests
                break
            self._request_pool.put(request)
            self._created_requests += 1
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
        if self._compiled_model is None:
            raise RuntimeError("text recognizer has been released")
        from rapidocr.ch_ppocr_rec.typings import TextRecOutput
        from rapidocr.ch_ppocr_rec.main import normalize_lang, reorder_bidi_for_display
        from rapidocr.utils.vis_res import VisRes

        np = self._np
        started = time.perf_counter()
        img_list = [args.img] if isinstance(args.img, np.ndarray) else list(args.img)
        return_word_box = bool(args.return_word_box)
        if not img_list:
            return TextRecOutput(
                [],
                [],
                [],
                [],
                time.perf_counter() - started,
                viser=VisRes(
                    lang_type=self._recognizer.cfg.lang_type,
                    font_path=self._recognizer.cfg.font_path,
                ),
            )
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
    try:
        compiled_model = recognizer.session.session.get_compiled_model()
        optimal_requests = int(compiled_model.get_property("OPTIMAL_NUMBER_OF_INFER_REQUESTS"))
        if optimal_requests > 0:
            request_count = min(request_count, optimal_requests)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
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
