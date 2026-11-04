# -*- coding: utf-8 -*-
"""离线（禁网）契约的回归测试（2026-10-23）。

审计 ``ai-workspace/overnight/findings/07-offline-contract.md`` 指出原钩子
只拦 ``socket.connect`` 一个 audit event，四条绕过面全开：

* **F1/F2**：``socket.create_connection()`` 的实现顺序是 ``getaddrinfo()``
  → ``connect()``，只拦第二步的话，**第一步的 DNS 查询已经真的发出去了**；
* **F3**：UDP ``sendto()`` 不触发 ``connect``，可无认证发包到任意 IP:端口；
* **F5**：``subprocess.Popen`` / ``os.system`` 的 audit event 不在清单里，
  ``curl``、PowerShell 的 ``Invoke-WebRequest`` 都能出网；
* **静默降级**：被拦之后没有任何上层把异常上报给用户 —— 用户只看到一堆
  内部异常，甚至"识别成功但结果为空"。

这里锁住三件事：

1. 清单里**每一个**事件名单独触发时确实被拦；
2. **不误伤**：``socket.socket()`` 建对象、以及其它普通事件必须放行
   （onnxruntime / opencv / setuptools 在无害场景下会建 socket）；
3. 拦截消息是**用户看得懂的中文**，且异常走的是自己的错误码，
   不会被归成"写盘失败"，也不会被上层静默吞掉。

反向验证：钩子装上之后，后端仍然能 import 并跑完一次最小识别流程
（假引擎，不跑真模型）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# 单跑本文件时（python -m unittest discover -s backend/tests）sys.path 里
# 没有 backend 目录，这里补上，保证 import ocr_backend 一定能解析到被测树。
_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import numpy as np  # noqa: E402

import ocr_backend  # noqa: E402
import table_pipeline  # noqa: E402


_EXTERNAL_HOST = "example.com"
_EXTERNAL_ADDRESS = (_EXTERNAL_HOST, 80)


def _representative_arguments(event: str) -> tuple:
    """每个 audit event 的代表性参数（不真的联网）。"""
    if event == "socket.connect":
        return (None, _EXTERNAL_ADDRESS)
    if event == "socket.getaddrinfo":
        return (_EXTERNAL_HOST, 80, 0, 0, 0)
    if event in ("socket.gethostbyname", "socket.gethostbyaddr"):
        return (_EXTERNAL_HOST,)
    if event == "socket.sendto":
        return (None, ("8.8.8.8", 53))
    if event == "socket.sendmsg":
        return (None, b"payload", [], 0, ("8.8.8.8", 53))
    if event == "subprocess.Popen":
        return (None, "curl http://example.com", None, None)
    if event == "os.system":
        return ("curl http://example.com",)
    if event in ("os.posix_spawn", "os.spawn"):
        return ("curl", ["curl", "http://example.com"], None)
    if event == "webbrowser.open":
        return ("http://example.com",)
    if event == "urllib.Request":
        return ("http://example.com", None)
    if event == "http.client.connect":
        return (_EXTERNAL_HOST, 80, None)
    raise AssertionError("no representative arguments for %s" % event)


class OfflineDenyListTests(unittest.TestCase):
    """清单里的每一个事件都必须被拦。"""

    def test_every_manifest_event_is_denied(self):
        manifest = sorted(ocr_backend._OFFLINE_DENIED_EVENTS)
        self.assertTrue(manifest, "离线拦截清单不能为空")
        for event in manifest:
            with self.subTest(event=event):
                arguments = _representative_arguments(event)
                self.assertIsNotNone(
                    ocr_backend._offline_event_prohibition(event, arguments),
                    "%s 没有被判定为需要拦截" % event,
                )
                with self.assertRaises(PermissionError):
                    ocr_backend._deny_network_access(event, arguments)

    def test_dns_events_are_denied(self):
        """F1/F2：DNS 解析必须一起拦，否则查询已经真的发出去了。"""
        for event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            with self.subTest(event=event):
                with self.assertRaises(PermissionError):
                    ocr_backend._deny_network_access(
                        event, _representative_arguments(event)
                    )

    def test_udp_send_events_are_denied(self):
        """F3：UDP 不需要 connect 也能发包。"""
        for event in ("socket.sendto", "socket.sendmsg"):
            with self.subTest(event=event):
                with self.assertRaises(PermissionError):
                    ocr_backend._deny_network_access(
                        event, _representative_arguments(event)
                    )

    def test_subprocess_family_is_denied(self):
        """F5：子进程可以绕开钩子自己出网。"""
        for event in ("subprocess.Popen", "os.system", "os.exec", "os.execve",
                      "os.spawn", "os.spawnv", "os.posix_spawn",
                      "webbrowser.open", "urllib.Request", "http.client.connect"):
            with self.subTest(event=event):
                with self.assertRaises(PermissionError):
                    ocr_backend._deny_network_access(
                        event, _representative_arguments(
                            event if event in ocr_backend._OFFLINE_DENIED_EVENTS
                            else "subprocess.Popen"
                        )
                    )

    def test_real_library_calls_are_blocked_end_to_end(self):
        """真的调一次标准库，确认钩子拦得住（拦在出网之前，不会真的发包）。"""
        import http.client
        import urllib.request

        cases = (
            ("getaddrinfo", lambda: socket.getaddrinfo(_EXTERNAL_HOST, 80)),
            ("gethostbyname", lambda: socket.gethostbyname(_EXTERNAL_HOST)),
            ("gethostbyaddr", lambda: socket.gethostbyaddr("8.8.8.8")),
            ("os.system", lambda: os.system("echo offline-contract-probe")),
            (
                "subprocess.Popen",
                lambda: subprocess.Popen(
                    [sys.executable, "-c", "print('offline-contract-probe')"]
                ),
            ),
            ("urllib", lambda: urllib.request.urlopen("http://example.com/")),
            ("webbrowser", lambda: __import__("webbrowser").open("http://example.com")),
            (
                "http.client",
                lambda: http.client.HTTPConnection(_EXTERNAL_HOST, 80).connect(),
            ),
        )
        for label, call in cases:
            with self.subTest(call=label):
                with self.assertRaises(PermissionError):
                    call()

        datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with self.assertRaises(PermissionError):
                datagram.sendto(b"offline-contract-probe", ("8.8.8.8", 53))
        finally:
            datagram.close()


class OfflineFalsePositiveTests(unittest.TestCase):
    """拦截不能误伤：建 socket 和普通事件必须放行。"""

    def test_socket_object_creation_is_not_blocked(self):
        """socket.socket() 必须放行 —— 第三方库在无害场景下会建 socket。"""
        self.assertIsNone(
            ocr_backend._offline_event_prohibition("socket.__new__", (None, 2, 2, 0))
        )
        self.assertNotIn("socket.__new__", ocr_backend._OFFLINE_DENIED_EVENTS)
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.assertIsNotNone(connection)
        finally:
            connection.close()

    def test_ordinary_events_are_not_blocked(self):
        for event in ("open", "os.listdir", "os.stat", "socket.__new__",
                      "socket.bind", "socket.listen", "socket.recvfrom",
                      "socket.gethostbyaddr"):
            with self.subTest(event=event):
                arguments = _representative_arguments(event) if event in (
                    "socket.gethostbyaddr",
                ) else ()
                if event == "socket.gethostbyaddr":
                    self.assertIsNotNone(
                        ocr_backend._offline_event_prohibition(event, arguments)
                    )
                    continue
                self.assertIsNone(
                    ocr_backend._offline_event_prohibition(event, arguments),
                    "%s 不该被拦" % event,
                )
                ocr_backend._deny_network_access(event, arguments)

    def test_backend_may_respawn_itself(self):
        """交付自检会用子进程跑一次后端；子进程会装上同一套钩子，放行。"""
        backend = str(Path(ocr_backend.__file__).resolve())
        allowed = (
            None,
            "%s %s" % (sys.executable, backend),
            None,
            None,
        )
        self.assertTrue(ocr_backend._allows_backend_self_spawn(allowed))
        self.assertIsNone(
            ocr_backend._offline_event_prohibition("subprocess.Popen", allowed)
        )

    def test_backend_may_not_respawn_a_shell(self):
        """带 -c 的命令不是"再拉起自己"，必须继续拦。"""
        backend = str(Path(ocr_backend.__file__).resolve())
        for command in (
            "%s -c pass" % sys.executable,
            "%s -c \"open(r'%s')\"" % (sys.executable, backend),
            "curl http://example.com",
        ):
            with self.subTest(command=command):
                self.assertFalse(
                    ocr_backend._allows_backend_self_spawn(
                        (None, command, None, None)
                    )
                )
                with self.assertRaises(PermissionError):
                    ocr_backend._deny_network_access(
                        "subprocess.Popen", (None, command, None, None)
                    )


class OfflineMessageTests(unittest.TestCase):
    """拦截消息必须是用户看得懂的中文。"""

    def _message_for(self, event: str) -> str:
        with self.assertRaises(PermissionError) as caught:
            ocr_backend._deny_network_access(
                event, _representative_arguments(event)
            )
        return str(caught.exception)

    def test_message_states_the_offline_policy_and_the_blocked_action(self):
        for event in sorted(ocr_backend._OFFLINE_DENIED_EVENTS):
            with self.subTest(event=event):
                message = self._message_for(event)
                self.assertIn("离线识别", message)
                self.assertIn("禁止联网", message)
                self.assertIn("禁止", message)
                self.assertIn("模型文件", message)

    def test_connect_message_keeps_the_legacy_wording(self):
        """老单测（test_backend.py）用 "禁止建立网络连接" 断言，不能改掉。"""
        message = self._message_for("socket.connect")
        self.assertIn("禁止建立网络连接", message)

    def test_message_has_no_latin_letters(self):
        """GUI 的 localizedBackendDetail() 见到拉丁字母就改用通用话术，
        真正的离线原因就看不见了 —— 所以消息必须纯中文。"""
        for event in sorted(ocr_backend._OFFLINE_DENIED_EVENTS):
            with self.subTest(event=event):
                message = self._message_for(event)
                self.assertFalse(
                    any("a" <= character.lower() <= "z" for character in message),
                    "离线提示里混进了拉丁字母：%s" % message,
                )

    def test_violation_is_reported_with_its_own_error_code(self):
        error = ocr_backend._OfflinePolicyViolation("离线识别禁止联网：测试。")
        response = ocr_backend._error_response(error, {"action": "recognize"})
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error_code"], "NETWORK_ACCESS_DENIED")
        self.assertIn("离线识别", response["message"])
        self.assertFalse(response["retryable"])

    def test_ordinary_permission_error_still_maps_to_write_failure(self):
        """离线错误码不能把真正的写盘失败也一起吃掉。"""
        error = PermissionError("无法写入导出文件")
        response = ocr_backend._error_response(error, {"action": "recognize"})
        self.assertEqual(response["error_code"], "OUTPUT_WRITE_FAILED")


class OfflineRecognitionStillWorksTests(unittest.TestCase):
    """反向验证：钩子装上之后，后端仍能跑完一次最小识别流程。"""

    def test_minimal_recognition_completes_with_the_hook_installed(self):
        ocr_backend._load_runtime()
        image = np.full((100, 300, 3), 255, dtype=np.uint8)
        false_strip = np.full((10, 300, 3), 255, dtype=np.uint8)
        boxes = np.array(
            [
                [[10, 8], [80, 8], [80, 25], [10, 25]],
                [[160, 8], [230, 8], [230, 25], [160, 25]],
                [[10, 38], [80, 38], [80, 55], [10, 55]],
                [[160, 38], [230, 38], [230, 55], [160, 55]],
                [[10, 68], [80, 68], [80, 85], [10, 85]],
                [[160, 68], [230, 68], [230, 85], [160, 85]],
            ],
            dtype=float,
        )
        ocr_result = SimpleNamespace(
            boxes=boxes,
            txts=["编号", "频率", "A001", "515.221", "A002", "516.347"],
            scores=[0.99] * 6,
        )

        def engine(candidate):
            return ocr_result

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "offline-contract.png"
            ocr_backend.cv2.imencode(".png", image)[1].tofile(str(image_path))
            with (
                patch.object(table_pipeline, "extract_screen_grid", return_value=None),
                patch.object(
                    table_pipeline,
                    "prepare_image",
                    return_value=(false_strip, {"detected": True, "mode": "auto", "corners": []}),
                ),
                patch.object(table_pipeline, "extract_ruled_grid", return_value=None),
                patch.object(ocr_backend, "_engines", return_value=(engine, None)),
            ):
                result = ocr_backend._recognize(
                    {
                        "protocol": ocr_backend.PROTOCOL_VERSION,
                        "action": "recognize",
                        "image_path": str(image_path),
                        "output_directory": directory,
                    }
                )

        self.assertEqual(result["rows"], 3)
        self.assertEqual(result["columns"], 2)
        self.assertEqual(result["cells"][2][1]["text"], "516.347")


if __name__ == "__main__":
    unittest.main()
