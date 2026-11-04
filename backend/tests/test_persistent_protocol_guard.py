# -*- coding: utf-8 -*-
"""常驻服务（``--persistent``）的协议护栏契约。

**2026-10-30（lead 复查 P0）**：实测交付 exe 收到一行 ``123``（合法 JSON、非法请求）
时，``_echo_request_id`` 对 int 做 ``"request_id" in request`` 抛 ``TypeError``，
异常逃出 ``main()`` ⇒ **整个常驻识别服务进程退出**（rc=1），之后所有客户端请求
全部石沉大海。``--persistent`` 是公开文档化的服务模式（docs/BACKEND_API_V1.md §1），
一次畸形输入打死服务是不可接受的。

这里用**子进程真跑**锁住行为，不依赖被测函数内部实现：
1. 任意畸形输入都必须换回**一行** ``status=error`` 响应；
2. 之后服务必须**仍然存活**并能正常应答（health）；
3. 响应必须是单行、可解析的 JSON（协议"一个请求一行响应"不破）。
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]

# 每条输入 = (名称, payload, 期望 error_code)。顺带锁住错误码分流 ——
# 2026-10-30 lead 复查：原来只断言 status == "error"，未知 action / protocol=2 /
# request_id 非法全都塌缩成同一个可满足条件，等于没测。
BAD_INPUTS = [
    ("标量 int", "123", "INVALID_REQUEST"),
    ("标量 null", "null", "INVALID_REQUEST"),
    ("标量 true", "true", "INVALID_REQUEST"),
    ("标量 float", "1.5", "INVALID_REQUEST"),
    ("数组", "[1,2,3]", "INVALID_REQUEST"),
    ("空对象", "{}", "UNSUPPORTED_PROTOCOL"),
    ("畸形 JSON", "{bad json", "INVALID_REQUEST"),
    ("未闭合字符串", '{"protocol":1,"action":"health"', "INVALID_REQUEST"),
    ("未知 action", '{"protocol":1,"action":"nope","request_id":9}', "UNSUPPORTED_ACTION"),
    ("protocol=2", '{"protocol":2,"action":"health","request_id":10}', "UNSUPPORTED_PROTOCOL"),
    ("protocol 为字符串", '{"protocol":"1","action":"health"}', "UNSUPPORTED_PROTOCOL"),
    ("request_id 越界", '{"protocol":1,"action":"health","request_id":99999999999}', "INVALID_REQUEST"),
    ("request_id 为字符串", '{"protocol":1,"action":"health","request_id":"abc"}', "INVALID_REQUEST"),
    ("options 为数组", '{"protocol":1,"action":"recognize","image_path":"x.png","options":[1,2]}', "INVALID_REQUEST"),
    ("image_path 为数字", '{"protocol":1,"action":"recognize","image_path":123}', "IMAGE_PATH_REQUIRED"),
    ("cells 为字符串", '{"protocol":1,"action":"export_xlsx","output_path":"a.xlsx","cells":"abc"}', "EXPORT_GRID_INVALID"),
]


class PersistentProtocolGuardTests(unittest.TestCase):
    def _start(self):
        proc = subprocess.Popen(
            [sys.executable, "-B", str(_BACKEND_DIR / "ocr_backend.py"), "--persistent"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(_BACKEND_DIR),
            text=True,
            encoding="utf-8",
        )
        lines: queue.Queue[str] = queue.Queue()

        def reader() -> None:
            for line in proc.stdout:
                lines.put(line.rstrip("\n"))

        threading.Thread(target=reader, daemon=True).start()
        return proc, lines

    def _send_and_read(self, proc, lines, payload: str, timeout: float = 20.0):
        while not lines.empty():
            lines.get_nowait()
        proc.stdin.write(payload + "\n")
        proc.stdin.flush()
        try:
            return lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def _stop(self, proc) -> None:
        """结束子进程并关掉两条管道（否则 TextIOWrapper 会报 ResourceWarning）。"""
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=10)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass

    def test_malformed_input_never_kills_service(self):
        proc, lines = self._start()
        try:
            for name, payload, expected_code in BAD_INPUTS:
                with self.subTest(case=name):
                    line = self._send_and_read(proc, lines, payload)
                    self.assertIsNotNone(line, "%s: 没有响应（服务可能已退出）" % name)
                    response = json.loads(line)
                    self.assertEqual(
                        response.get("status"), "error",
                        "%s: 畸形请求应返回 status=error，实际 %r" % (name, response.get("status")),
                    )
                    # 错误码分流必须真的分开（否则这条用例对不同缺陷零信息量）
                    self.assertEqual(
                        response.get("error_code"), expected_code,
                        "%s: 期望 error_code=%s，实际 %r"
                        % (name, expected_code, response.get("error_code")),
                    )
                    # 协议：一行一响应，不允许把 traceback 之类塞进响应体
                    self.assertNotIn("\n", line)
                    # 服务必须还活着
                    self.assertIsNone(
                        proc.poll(),
                        "%s: 处理后服务退出了（rc=%s）—— 畸形输入不允许打死常驻服务"
                        % (name, proc.returncode),
                    )
            # 收尾：一条合法请求仍要能正常应答
            ok_line = self._send_and_read(
                proc, lines, json.dumps({"protocol": 1, "action": "health", "request_id": 1})
            )
            self.assertIsNotNone(ok_line, "畸形输入之后 health 无响应")
            self.assertEqual(json.loads(ok_line).get("status"), "ok")
        finally:
            self._stop(proc)

    def test_request_id_echoed_only_for_dict_request(self):
        """request_id 回显不能对非 dict 请求抛异常（P0 的直接成因）。"""
        for payload in ("123", "null", "true", "[]", '"str"'):
            with self.subTest(payload=payload):
                proc, lines = self._start()
                try:
                    line = self._send_and_read(proc, lines, payload)
                    self.assertIsNotNone(line, "%s 无响应" % payload)
                    response = json.loads(line)
                    self.assertEqual(response.get("status"), "error")
                    self.assertNotIn("request_id", response)
                finally:
                    self._stop(proc)


if __name__ == "__main__":
    unittest.main()