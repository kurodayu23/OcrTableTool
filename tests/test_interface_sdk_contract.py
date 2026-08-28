import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLIENT_CPP = ROOT / "interface-sdk/qt/ocrtableclient.cpp"
CLIENT_H = ROOT / "interface-sdk/qt/ocrtableclient.h"
CAMERA_CPP = ROOT / "interface-sdk/qt/cameraocrclient.cpp"
CAMERA_H = ROOT / "interface-sdk/qt/cameraocrclient.h"
CAMERA_EXAMPLE = ROOT / "interface-sdk/example/camera-main.cpp"
SDK_README = ROOT / "interface-sdk/README.md"
API_DOC = ROOT / "docs/BACKEND_API_V1.md"
CAMERA_DOC = ROOT / "docs/CAMERA_OCR_INTERFACE.md"
SCHEMA = ROOT / "docs/schemas/backend-api-v1.schema.json"


class InterfaceSdkContractTests(unittest.TestCase):
    def test_schema_is_valid_json_and_contains_all_actions(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        serialized = json.dumps(schema, ensure_ascii=False)
        for action in ("health", "warmup", "recognize", "export_xlsx"):
            self.assertIn(f'"{action}"', serialized)

    def test_client_uses_one_process_per_request(self):
        source = CLIENT_CPP.read_text(encoding="utf-8")
        self.assertIn("m_process->closeWriteChannel();", source)
        self.assertIn("processFinished(int exitCode)", source)
        self.assertIn("payload.append('\\n')", source)
        self.assertNotIn('QStringLiteral("--persistent")', source)
        self.assertNotIn("m_process->kill();\n        m_pendingResponse = response", source)
        self.assertNotIn("warmup()", CLIENT_H.read_text(encoding="utf-8"))

    def test_client_locks_maximum_accuracy(self):
        source = CLIENT_CPP.read_text(encoding="utf-8")
        self.assertIn('QStringLiteral("crop_mode"), QStringLiteral("auto")', source)
        self.assertIn('QStringLiteral("accuracy_mode"), QStringLiteral("maximum")', source)
        self.assertIn('QStringLiteral("deadline_seconds"), 0', source)

    def test_client_validates_publication_and_structure_contracts(self):
        source = CLIENT_CPP.read_text(encoding="utf-8")
        for token in (
            'QStringLiteral("request_id")',
            'QStringLiteral("recognition_state")',
            'QStringLiteral("publication_blocked")',
            'QStringLiteral("publication_block_reasons")',
            'QStringLiteral("structure_verified")',
            'QStringLiteral("structure_certificate")',
            'QStringLiteral("needs_review")',
            'QStringLiteral("geometry_hash")',
            'QStringLiteral("structure_hash")',
            'QStringLiteral("image_quality")',
            "hides non-anchor text",
        ):
            self.assertIn(token, source)

    def test_docs_explain_runtime_and_auto_publish_boundary(self):
        sdk = SDK_README.read_text(encoding="utf-8")
        api = API_DOC.read_text(encoding="utf-8")
        camera = CAMERA_DOC.read_text(encoding="utf-8")
        self.assertIn("OcrBackend.exe", sdk)
        self.assertIn("startCamera", camera)
        self.assertIn("exportLastCsv", camera)
        self.assertIn("needs_review", api)

    def test_camera_facade_exposes_one_complete_business_flow(self):
        header = CAMERA_H.read_text(encoding="utf-8")
        for token in (
            "class CameraOcrClient",
            "startCamera",
            "captureAndRecognize",
            "setTableRegion",
            "clearTableRegion",
            "tableRegion",
            "resolvedTableRegion",
            "tableRecognized",
            "tableCells",
            "setCellText",
            "appendRow",
            "exportLastCsv",
            "canAutoPublish",
            "failed",
        ):
            self.assertIn(token, header)

        example = CAMERA_EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("imagePath", example)
        self.assertNotIn("open image", example.lower())

    def test_camera_facade_creates_no_dialog_or_yellow_style(self):
        source = CAMERA_CPP.read_text(encoding="utf-8")
        header = CAMERA_H.read_text(encoding="utf-8")
        document = CAMERA_DOC.read_text(encoding="utf-8")
        for forbidden in ("QMessageBox", "QDialog", "setStyleSheet"):
            self.assertNotIn(forbidden, source)
            self.assertNotIn(forbidden, header)
        self.assertIn("CameraOcrClient", document)
        self.assertIn("setCellText", document)
        self.assertIn("appendRow", document)
        self.assertNotIn("\u540c\u4e8b", document)

    def test_camera_sdk_uses_stable_chinese_error_messages(self):
        source = CAMERA_CPP.read_text(encoding="utf-8")
        self.assertIn("QString::fromWCharArray", source)
        self.assertIn("Q_UNUSED(errorString);", source)
        self.assertNotIn("emitFailure(errorCode, message", source)

    def test_camera_sdk_degrades_gracefully_before_reporting_failure(self):
        source = CAMERA_CPP.read_text(encoding="utf-8")
        header = CAMERA_H.read_text(encoding="utf-8")
        self.assertIn('QStringLiteral("focus_fallback")', source)
        self.assertIn('SLOT(captureAfterFocusLock())', source)
        self.assertIn('QStringLiteral("image_quality_warning")', source)
        self.assertNotIn('pixels < 7LL * 1000LL * 1000LL) {\n        emitFailure', source)
        self.assertIn("m_ocrRetryCount < 1", source)
        self.assertIn('QStringLiteral("recognizing_retry")', source)
        self.assertIn("int m_ocrRetryCount;", header)
        self.assertIn("bool m_ocrRetryPending;", header)
        self.assertIn("if (!m_ocrRetryPending)", source)
        self.assertIn("m_ocrRetryPending = false;", source)
        self.assertIn('QStringLiteral("INSUFFICIENT_MEMORY")', source)
        self.assertIn('QStringLiteral("table_region_fallback")', source)
        self.assertIn("m_regionFallbackAttempted", source + header)

    def test_camera_sdk_crops_one_selected_table_on_the_full_resolution_photo(self):
        source = CAMERA_CPP.read_text(encoding="utf-8")
        header = CAMERA_H.read_text(encoding="utf-8")
        self.assertIn("bool setTableRegion(const QRectF &normalizedRegion", header)
        self.assertIn("QRectF m_tableRegion;", header)
        self.assertIn("reader.setAutoTransform(true);", source)
        self.assertIn('QStringLiteral("camera-table.png")', source)
        self.assertIn("resolvedTableRegion(capturedImage.size())", source)
        self.assertIn("bounded.width() * 0.015", source)
        self.assertIn("bounded.height() * 0.025", source)
        self.assertIn('QStringLiteral("table_region_cropped")', source)
        self.assertIn("m_capturePath = recognitionPath;", source)
        self.assertIn("m_ocr->recognize(recognitionPath, m_requestDirectory);", source)


if __name__ == "__main__":
    unittest.main()
