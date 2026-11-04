# -*- coding: utf-8 -*-
"""协议层 / 选项层 / 导出层的攻击面回归（2026-10-28，鲁棒性专线 r1）。

锁住三件事：

1. **`--persistent` 模式的"不死"契约**：``docs/BACKEND_API_V1.md`` §1 把
   ``--persistent`` 定义成公开的服务模式（一个请求一行 JSON，一个响应一行
   JSON）。任何畸形输入 —— 顶层 JSON 标量、非法 UTF-8、BOM、CRLF、10 MB 单行、
   1000 层嵌套、``__proto__`` 之类怪 key —— 都只能换来**一行合法 JSON 错误**，
   绝不能让进程退出。审计 A23 命中的正是这一类：``_echo_request_id`` 不在
   ``try`` 里，``123`` / ``null`` / ``true`` 这一行输入直接把整个服务打死。
2. **响应阶段也要有护栏**：``json.dumps`` 抛 ``TypeError``（响应里混进 numpy
   标量等不可序列化对象）时，同样必须回一行 INTERNAL_ERROR，而不是让异常
   逃出主循环。
3. **选项层的数值边界**：``deadline_seconds`` 的 NaN / ±Infinity / 超大整数
   必须在协议边界被判成 ``INVALID_OPTION``，不能被静默放大成"最长时限"，
   也不能因为 ``OverflowError`` 落到 ``INTERNAL_ERROR``（那会让客户端自动重试
   一轮同样非法的请求）。

全部测试都不加载识别模型。
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# 单跑本文件时（python -m unittest discover -s backend/tests）sys.path 里没有
# backend 目录，这里补上，保证 import ocr_backend 一定能解析到被测树。
_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import numpy as np  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

import ocr_backend  # noqa: E402
import table_pipeline  # noqa: E402


_BACKEND_PATH = str(Path(ocr_backend.__file__).resolve())


def _serve(lines, timeout=240.0):
    """把若干原始行喂给 --persistent 子进程，返回 (响应行, stderr, 退出码)。"""
    process = subprocess.Popen(
        [sys.executable, "-B", _BACKEND_PATH, "--persistent"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(b"".join(lines), timeout=timeout)
    responses = [line for line in stdout.split(b"\n") if line.strip()]
    return responses, stderr.decode("utf-8", "replace"), process.returncode


def _parse(response_lines):
    parsed = []
    for line in response_lines:
        # ensure_ascii=True 的响应必须是纯 ASCII：混进非 ASCII 就是编码回归
        parsed.append(json.loads(line.decode("ascii")))
    return parsed


class PersistentServiceSurvivesMalformedInputTests(unittest.TestCase):
    """一个进程喂一整批畸形输入：只要还回得出响应，服务就是活的。"""

    def test_malformed_corpus_never_kills_the_service(self):
        corpus = [
            # 顶层 JSON 标量（审计 A23 的原始复现输入）
            (b"123\n", "top-level-int"),
            (b"1.5\n", "top-level-float"),
            (b"null\n", "top-level-null"),
            (b"true\n", "top-level-true"),
            (b"false\n", "top-level-false"),
            (b'"str"\n', "top-level-string"),
            (b"[1,2,3]\n", "top-level-array"),
            (b"[]\n", "top-level-empty-array"),
            # 编码面
            (b"\xef\xbb\xbf" + b'{"protocol":1,"action":"nope","request_id":2}\n', "bom"),
            (b'{"protocol":1,"action":"nope","request_id":3}\r\n', "crlf"),
            (b'{"protocol":1,"action":"nope","request_id":4,"x":"\xff\xfe"}\n', "invalid-utf8"),
            (b"\x80\x81\n", "invalid-utf8-line"),
            # 结构面
            (b"[" * 1000 + b"]" * 1000 + b"\n", "nested-1000"),
            (b'{"a":' * 500 + b"1" + b"}" * 500 + b"\n", "nested-object-500"),
            (b'{"protocol":1,"action":"nope","request_id":5,"__proto__":{"a":1}}\n', "proto-key"),
            (b'{"protocol":1,"action":"nope","request_id":6,"$ref":"#/a"}\n', "dollar-ref"),
            (
                b'{"protocol":1,"action":"nope","request_id":7,'
                b'"constructor":{"prototype":{"x":1}}}\n',
                "constructor-key",
            ),
            # 尺寸面：10 MB 单行
            (
                b'{"protocol":1,"action":"nope","request_id":8,"pad":"'
                + b"a" * (10 * 1024 * 1024)
                + b'"}\n',
                "huge-10mb",
            ),
            # 参数类型面
            (b'{"protocol":1,"action":1,"request_id":9}\n', "action-nonstring"),
            (b'{"protocol":1,"action":"nope","request_id":"10"}\n', "request-id-string"),
            (b'{"protocol":1,"action":"nope","request_id":null}\n', "request-id-null"),
            (b'{"protocol":1,"action":"nope","request_id":0}\n', "request-id-zero"),
            (b'{"protocol":1,"action":"nope","request_id":true}\n', "request-id-bool"),
            (b'{"protocol":1,"action":"nope","request_id":{"a":1}}\n', "request-id-object"),
            (b'{"protocol":1,"action":"recognize","request_id":11,"image_path":"x",'
             b'"options":5}\n', "options-not-object"),
            # 收尾探针：只要这一行还有响应，前面的畸形输入就都没把服务打死
            (b'{"protocol":1,"action":"nope","request_id":99}\n', "liveness-probe"),
        ]
        responses, stderr, returncode = _serve([payload for payload, _ in corpus])
        self.assertEqual(
            returncode,
            0,
            "常驻服务被打死了（退出码 %s）：%s" % (returncode, stderr[-800:]),
        )
        self.assertEqual(
            len(responses),
            len(corpus),
            "响应行数 %d != 输入行数 %d —— 有输入没有换到响应"
            % (len(responses), len(corpus)),
        )
        parsed = _parse(responses)
        for (_, label), response in zip(corpus, parsed):
            with self.subTest(case=label):
                self.assertIn(response.get("status"), ("ok", "error"))
                if response.get("status") == "error":
                    self.assertTrue(
                        response.get("error_code"),
                        "错误响应必须带 error_code：%r" % (response,),
                    )
        # 收尾探针真的被当成一次请求处理了（而不是随便凑了一行）
        probe = parsed[-1]
        self.assertEqual(probe.get("status"), "error")
        self.assertEqual(probe.get("error_code"), "UNSUPPORTED_ACTION")
        self.assertEqual(probe.get("request_id"), 99)

    def test_top_level_scalar_gets_one_error_line_and_request_id_is_not_faked(self):
        """标量请求没有 request_id 可回显：不许补一个假的上去。"""
        responses, _, returncode = _serve([b"123\n"])
        self.assertEqual(returncode, 0)
        self.assertEqual(len(responses), 1)
        response = _parse(responses)[0]
        self.assertEqual(response.get("status"), "error")
        self.assertEqual(response.get("error_code"), "INVALID_REQUEST")
        self.assertNotIn("request_id", response)

    def test_blank_line_is_not_a_request_and_gets_no_response(self):
        """空行不是请求：调用方 BackendRunner::readStandardOutput 同样跳过空行。"""
        responses, _, returncode = _serve(
            [b"\n", b"   \n", b'{"protocol":1,"action":"nope","request_id":1}\n', b"\n"]
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(len(responses), 1)


class EchoRequestIdTests(unittest.TestCase):
    """A23 的单元级护栏：``_echo_request_id`` 对任何输入都不许抛。"""

    def test_scalar_requests_do_not_raise(self):
        for scalar in (123, 1.5, None, True, False):
            with self.subTest(request=scalar):
                response = {"status": "error"}
                ocr_backend._echo_request_id(response, scalar)
                self.assertNotIn("request_id", response)

    def test_dict_request_is_echoed_verbatim(self):
        response = {"status": "ok"}
        ocr_backend._echo_request_id(response, {"request_id": 2_147_483_647})
        self.assertEqual(response["request_id"], 2_147_483_647)

    def test_non_dict_response_is_ignored(self):
        for response in (None, [], "x", 1):
            with self.subTest(response=response):
                ocr_backend._echo_request_id(response, {"request_id": 1})


class ClassifyErrorRequestShapeTests(unittest.TestCase):
    """``_classify_error`` 原来对非 dict 的 request 会抛 AttributeError。"""

    def test_permission_error_with_scalar_request_does_not_raise(self):
        for request in (123, None, "abc", [], True):
            with self.subTest(request=request):
                code = ocr_backend._classify_error(PermissionError("x"), request)
                self.assertEqual(code[0], "INTERNAL_ERROR")

    def test_permission_error_with_export_request_still_maps_to_write_failure(self):
        code = ocr_backend._classify_error(
            PermissionError("x"), {"action": "export_xlsx"}
        )
        self.assertEqual(code, ("EXPORT_WRITE_FAILED", "output_path", True))


class ResponseSerializationTests(unittest.TestCase):
    """``json.dumps`` 在写响应这一步失败，也不许把服务打死。"""

    def test_unserializable_response_becomes_one_error_line(self):
        response = {"status": "ok", "rows": 1, "bad": np.int64(7)}
        line = ocr_backend._json_line(response, {"action": "recognize", "request_id": 5})
        parsed = json.loads(line)
        self.assertEqual(parsed["status"], "error")
        self.assertEqual(parsed["error_code"], "INTERNAL_ERROR")
        self.assertTrue(parsed["retryable"])
        self.assertEqual(parsed["request_id"], 5)
        self.assertEqual(parsed["action"], "recognize")

    def test_serializable_response_is_passed_through(self):
        line = ocr_backend._json_line({"status": "ok", "rows": 3})
        self.assertEqual(json.loads(line), {"status": "ok", "rows": 3})

    def test_persistent_line_survives_a_failing_error_response_builder(self):
        """错误响应构造函数自己抛错时，仍然要回一行合法 JSON。"""
        with (
            patch.object(
                ocr_backend, "handle_request", side_effect=RuntimeError("boom")
            ),
            patch.object(
                ocr_backend, "_error_response", side_effect=RuntimeError("also boom")
            ),
        ):
            line = ocr_backend._persistent_response_line(
                b'{"protocol":1,"action":"health","request_id":3}\n'
            )
        parsed = json.loads(line)
        self.assertEqual(parsed["status"], "error")
        self.assertEqual(parsed["error_code"], "INTERNAL_ERROR")
        self.assertEqual(parsed["request_id"], 3)

    def test_persistent_line_never_raises_on_arbitrary_bytes(self):
        payloads = [
            b"\x00\x01\x02\n",
            b"\xef\xbb\xbf\n",
            b"{}",
            b'{"a":',
            b"\xff" * 64 + b"\n",
            b"Infinity\n",
            b"NaN\n",
        ]
        for payload in payloads:
            with self.subTest(payload=payload[:16]):
                line = ocr_backend._persistent_response_line(payload)
                json.loads(line)


class DeadlineSecondsProtocolBoundaryTests(unittest.TestCase):
    """NaN / ±Infinity / 超大整数必须在协议边界被判成 INVALID_OPTION。"""

    def _request(self, deadline):
        return {
            "protocol": 1,
            "action": "recognize",
            "request_id": 1,
            "image_path": "no-such-file.png",
            "options": {"deadline_seconds": deadline},
        }

    def _code_for(self, deadline):
        try:
            table_pipeline.validate_request(self._request(deadline))
        except ValueError as error:
            return ocr_backend._classify_error(error, self._request(deadline))
        return None

    def test_non_finite_and_oversized_deadlines_are_rejected(self):
        for raw in (
            float("nan"),
            float("inf"),
            float("-inf"),
            1e400,  # JSON 里的标准写法也会解析成 inf
            10 ** 400,  # 超大整数：float() 抛 OverflowError
        ):
            with self.subTest(deadline=raw):
                self.assertEqual(
                    self._code_for(raw),
                    ("INVALID_OPTION", "options", False),
                    "非法 deadline_seconds 没有被判成 INVALID_OPTION",
                )

    def test_legal_deadlines_are_still_accepted(self):
        for raw in (0, 0.0, 1, 5, 60, 120, 1e9):
            with self.subTest(deadline=raw):
                self.assertIsNone(self._code_for(raw))

    def test_negative_and_non_numeric_deadlines_are_rejected(self):
        for raw in (-5, -1.5, "abc", None, True, [5], {"v": 5}):
            with self.subTest(deadline=raw):
                self.assertEqual(self._code_for(raw)[0], "INVALID_OPTION")

    def test_json_nan_and_infinity_literals_reach_the_same_verdict(self):
        """非标准字面量能被 json.loads 收下 —— 协议层同样要拒。"""
        for literal in (b"NaN", b"Infinity", b"-Infinity", b"1e400"):
            with self.subTest(literal=literal):
                payload = (
                    b'{"protocol":1,"action":"recognize","request_id":1,'
                    b'"image_path":"x.png","options":{"deadline_seconds":'
                    + literal
                    + b"}}"
                )
                request = json.loads(payload.decode("ascii"))
                self.assertFalse(math.isfinite(request["options"]["deadline_seconds"]))
                with self.assertRaises(ValueError):
                    table_pipeline.validate_request(request)


class ExportNullAndDeviceNameTests(unittest.TestCase):
    """导出边界：JSON null 格不能变成字面量 "None"；写盘成功必须文件真的存在。"""

    def test_null_cell_is_treated_as_empty_not_the_string_none(self):
        self.assertEqual(table_pipeline.cell_display_text(None), "")
        self.assertEqual(table_pipeline.cell_display_text({"text": None}), "")
        self.assertEqual(table_pipeline.xml_safe_text(None), "")
        self.assertEqual(table_pipeline.xml_safe_text({"text": None}), "")

    def test_grid_of_nulls_is_rejected_as_empty_table(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nulls.xlsx"
            with self.assertRaises(ValueError) as caught:
                table_pipeline.write_xlsx(target, [[None, None], [None, None]], [])
            self.assertIn("cells must", str(caught.exception))
            self.assertFalse(target.exists())

    def test_null_cells_export_as_blank_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "mixed.xlsx"
            table_pipeline.write_xlsx(
                target, [[None, {"text": "b"}], [{"text": "c"}, None]], []
            )
            workbook = load_workbook(target)
            sheet = workbook.active
            self.assertIn(sheet.cell(row=1, column=1).value, (None, ""))
            self.assertEqual(sheet.cell(row=1, column=2).value, "b")
            self.assertEqual(sheet.cell(row=2, column=1).value, "c")
            self.assertIn(sheet.cell(row=2, column=2).value, (None, ""))
            workbook.close()

    def test_write_reports_failure_when_the_target_never_appears(self):
        """Windows 保留设备名会让写盘调用"成功"但目标路径上什么都没有。"""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "CON.xlsx"
            with patch.object(table_pipeline.os, "replace", return_value=None):
                with self.assertRaises(PermissionError) as caught:
                    table_pipeline.write_xlsx(target, [["a"]], [])
            self.assertIn("无法写入导出文件", str(caught.exception))
            self.assertEqual(
                ocr_backend._classify_error(
                    caught.exception, {"action": "export_xlsx"}
                )[0],
                "EXPORT_WRITE_FAILED",
            )

    def test_normal_write_still_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ok.xlsx"
            table_pipeline.write_xlsx(target, [["a", "b"]], [])
            self.assertTrue(target.is_file())


_MISSING = object()


class OptionsBoundaryTests(unittest.TestCase):
    """``options`` 里的**键值**非法判 ``INVALID_OPTION``；``options`` **整体不是对象**
    判 ``INVALID_REQUEST``。

    口径依据（2026-11-02 核对）：契约 docs/BACKEND_API_V1.md L60/L61 的 19 码表写明
        | INVALID_REQUEST | 请求不合法（字段缺失/**类型错**/值域错）… | request_id / null | false |
        | INVALID_OPTION  | ``options`` 里 ``crop_mode`` / ``accuracy_mode`` / ``deadline_seconds`` 非法 | options | false |
    ⇒ ``INVALID_OPTION`` 只覆盖那三个**具体键**的值非法；
      「``options`` 整体不是对象」属于**字段类型错** ⇒ 归 ``INVALID_REQUEST``。

    ⚠ 本条曾期望 ``INVALID_OPTION``，依据是 2026-10-28 时那份**旧版**契约
      （125 行、只有错误码枚举、没有 19 码对照表）。现行契约（合并后进入仓库的
      10-30 版本）口径相反 ⇒ 已按现行契约改为 ``INVALID_REQUEST``。
    ⇒ 「options 里具体键非法 ⇒ INVALID_OPTION」这条**不变**，见下方用例。
    """

    def _code(self, options):
        request = {
            "protocol": 1,
            "action": "recognize",
            "request_id": 1,
            "image_path": "no-such-file.png",
        }
        if options is not _MISSING:
            request["options"] = options
        try:
            table_pipeline.validate_request(request)
        except ValueError as error:
            return ocr_backend._classify_error(error, request)
        return None

    def test_bad_crop_mode_is_an_option_error(self):
        for raw in ("zzz", None, 1, "", "FULL"):
            with self.subTest(crop_mode=raw):
                self.assertEqual(self._code({"crop_mode": raw}),
                                 ("INVALID_OPTION", "options", False))

    def test_bad_accuracy_mode_is_an_option_error(self):
        for raw in ("ultra", None, 1, ""):
            with self.subTest(accuracy_mode=raw):
                self.assertEqual(self._code({"accuracy_mode": raw}),
                                 ("INVALID_OPTION", "options", False))

    def test_non_boolean_flags_are_option_errors(self):
        for name in ("input_rectified", "selected_table_region"):
            for raw in ("true", 1, 0, None, [], {}):
                with self.subTest(option=name, value=raw):
                    self.assertEqual(
                        self._code({name: raw}),
                        ("INVALID_OPTION", "options", False),
                        "%s 的类型错应当判成 INVALID_OPTION" % name,
                    )

    def test_options_object_of_wrong_type_is_an_invalid_request(self):
        """``options`` 整体不是对象 ⇒ 字段类型错 ⇒ INVALID_REQUEST（现行契约 L60）。"""
        for raw in (5, [], "auto", None, True):
            with self.subTest(options=raw):
                self.assertEqual(self._code(raw),
                                 ("INVALID_REQUEST", None, False))

    def test_option_key_values_stay_invalid_option(self):
        """``options`` 里**具体键**的值非法 ⇒ 仍是 INVALID_OPTION / field=options。"""
        for key, raw in (
            ("crop_mode", "zzz"),
            ("accuracy_mode", "nope"),
            ("deadline_seconds", "soon"),
        ):
            with self.subTest(key=key, value=raw):
                self.assertEqual(self._code({key: raw}),
                                 ("INVALID_OPTION", "options", False))

    def test_legal_option_values_are_accepted(self):
        legal = (
            {},
            {"crop_mode": "auto", "accuracy_mode": "maximum", "deadline_seconds": 0},
            {"crop_mode": "grid"},
            {"crop_mode": "full"},
            {"accuracy_mode": "adaptive"},
            {"accuracy_mode": "adaptive_high"},
            {"input_rectified": True, "selected_table_region": False},
            {"unknown_future_option": {"anything": [1, 2, 3]}},
        )
        for options in legal:
            with self.subTest(options=options):
                self.assertIsNone(self._code(options))

    def test_default_options_are_not_required(self):
        self.assertIsNone(self._code(_MISSING))


class CropModeConsumptionTests(unittest.TestCase):
    """审计 A24 的事实核对：crop_mode 到底在哪儿被消费、响应有没有回传。

    结论（2026-10-28）：``crop_mode`` 只在**没有物理框线网格**的兜底路径上被
    读到（`pipeline.prepare_image`，两个消费点），响应里**从来不回传**
    ``crop_mode``（"回传但不生效"的前半句本身不成立）。本类把这两点锁成事实，
    并附一条"有框线时三种取值结果一致"的行为证据 —— 是否让它在框线路径上
    也生效（或从契约里删掉）需要主控拍板，见本轮交付报告。
    """

    def test_response_has_no_crop_mode_field(self):
        with tempfile.TemporaryDirectory() as directory:
            image = np.full((240, 320, 3), 255, np.uint8)
            path = Path(directory) / "blank.png"
            import cv2

            cv2.imwrite(str(path), image)
            request = {
                "protocol": 1,
                "action": "recognize",
                "request_id": 1,
                "image_path": str(path),
                "output_directory": directory,
                "options": {"crop_mode": "full"},
            }
            # 全白图必然识别不出表格：这里只关心"错误响应里也没有 crop_mode"
            try:
                response = ocr_backend._recognize(request)
            except ValueError as error:
                response = ocr_backend._error_response(error, request)
        self.assertNotIn("crop_mode", response)
        self.assertNotIn("options", response)

    def test_prepare_image_is_the_only_consumer(self):
        source = Path(_BACKEND_DIR, "ocr_backend.py").read_text(encoding="utf-8")
        lines = [
            number
            for number, line in enumerate(source.splitlines(), start=1)
            if "crop_mode" in line and not line.lstrip().startswith("#")
        ]
        # 剩下的消费点：_recognize 里那两行（判 auto / 传参）+ _classify_error
        # 里判错误码的那一行。任何"新增消费点"都该同步更新这条断言。
        self.assertLessEqual(
            len(lines), 6, "ocr_backend.py 里 crop_mode 的消费点变多了：%r" % (lines,)
        )


class OfflineLoopbackConsistencyTests(unittest.TestCase):
    """审计 A20：注释与实现必须说同一件事。

    结论（2026-10-28）：实现没改，**改的注释**。既有回归测试
    ``test_backend.py::test_backend_runtime_blocks_socket_connections`` 明确断言
    ``socket.connect(("127.0.0.1", 9))`` 要抛「禁止建立网络连接」——实现与测试
    一致，只有模块顶部那句「本机回环仍然放行」漂了。这里把**真实口径**锁死，
    并显式记下"有意不对称"，防止将来只改一半。
    """

    def _prohibition(self, event, arguments):
        return ocr_backend._offline_event_prohibition(event, arguments)

    def test_tcp_connect_is_denied_even_for_loopback(self):
        for host in ("127.0.0.1", "127.1.2.3", "localhost", "::1", "", "0.0.0.0"):
            with self.subTest(host=host):
                self.assertIsNotNone(
                    self._prohibition("socket.connect", (None, (host, 8080))),
                    "socket.connect 应当无条件拒绝（含回环）",
                )

    def test_external_tcp_connect_is_denied(self):
        for host in ("example.com", "8.8.8.8", b"example.com"):
            with self.subTest(host=host):
                self.assertIsNotNone(
                    self._prohibition("socket.connect", (None, (host, 80)))
                )

    def test_dns_and_udp_to_loopback_are_allowed(self):
        """有意不对称：DNS/UDP 的本机目标放行（引擎的探测包），连接不放行。"""
        self.assertIsNone(
            self._prohibition("socket.getaddrinfo", ("localhost", 80, 0, 0, 0))
        )
        self.assertIsNone(
            self._prohibition("socket.gethostbyname", ("127.0.0.1",))
        )
        self.assertIsNone(self._prohibition("socket.sendto", (None, ("127.0.0.1", 53))))

    def test_dns_and_udp_to_external_hosts_are_denied(self):
        for event, arguments in (
            ("socket.getaddrinfo", ("example.com", 80, 0, 0, 0)),
            ("socket.gethostbyname", ("example.com",)),
            ("socket.gethostbyaddr", ("8.8.8.8",)),
            ("socket.sendto", (None, ("8.8.8.8", 53))),
            ("socket.sendmsg", (None, b"payload", [], 0, ("8.8.8.8", 53))),
        ):
            with self.subTest(event=event):
                self.assertIsNotNone(self._prohibition(event, arguments))

    def test_deny_raises_with_the_legacy_wording(self):
        for arguments in ((None, ("example.com", 80)), (None, ("127.0.0.1", 9))):
            with self.subTest(arguments=arguments):
                with self.assertRaises(PermissionError) as caught:
                    ocr_backend._deny_network_access("socket.connect", arguments)
                self.assertIn("禁止建立网络连接", str(caught.exception))

    def test_documented_behaviour_matches_the_regression_test_in_test_backend(self):
        """交叉引用：test_backend.py 那条"连回环也拦"的断言与本类的口径一致。"""
        source = Path(_BACKEND_DIR, "tests", "test_backend.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("test_backend_runtime_blocks_socket_connections", source)
        self.assertIn('connection.connect(("127.0.0.1", 9))', source)


if __name__ == "__main__":
    unittest.main()
