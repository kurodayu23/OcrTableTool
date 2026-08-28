from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UiContractTest(unittest.TestCase):
    def test_cpp_sources_with_chinese_literals_use_utf8_bom_for_msvc2015(self):
        for path in (ROOT / "src").rglob("*"):
            if path.suffix not in {".cpp", ".h"}:
                continue
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
            if any(ord(character) > 127 for character in text):
                self.assertTrue(
                    raw.startswith(b"\xef\xbb\xbf"),
                    f"{path} contains non-ASCII UI text but has no UTF-8 BOM",
                )

    def test_target_device_layout_and_portable_backend_contract(self):
        window = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        main = (ROOT / "src/gui/main.cpp").read_text(encoding="utf-8")
        runner = (ROOT / "src/gui/backendrunner.cpp").read_text(encoding="utf-8")

        self.assertIn("availableGeometry", window)
        self.assertIn("setMinimumSize(760, 460);", window)
        self.assertIn("splitter->setSizes(QList<int>() << 400 << 760);", window)
        self.assertIn('QStringLiteral("ocr-runtime/OcrBackend.exe")', runner)
        self.assertIn("Qt::AA_EnableHighDpiScaling", main)
        self.assertIn("Qt::AA_UseHighDpiPixmaps", main)

    def test_approved_blue_workbench_contract(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/mainwindow.h").read_text(encoding="utf-8")
        preview = (ROOT / "src/gui/imagepreview.cpp").read_text(encoding="utf-8")
        runner = (ROOT / "src/gui/backendrunner.cpp").read_text(encoding="utf-8")
        qss = (ROOT / "assets/app.qss").read_text(encoding="utf-8")

        self.assertIn('QStringLiteral("开始识别")', source)
        self.assertIn("button->setMinimumHeight(48);", source)
        self.assertIn("splitter->setSizes(QList<int>() << 400 << 760);", source)
        self.assertIn("#2563EB", qss)
        self.assertIn("#F4F7FB", qss)
        self.assertIn("m_readyStateLabel", header)
        self.assertIn("rect().adjusted(8, 8, -8, -8)", preview)

        # 只检查真正进入界面的字符串，中文维护注释不属于可见文案。
        visible_ui = "\n".join(
            line
            for text, include_all in ((source, False), (runner, False), (qss, True))
            for line in text.splitlines()
            if include_all or "QStringLiteral" in line
        )
        self.assertNotIn("离线", visible_ui)
        self.assertNotIn("后端", visible_ui)
        self.assertNotIn("正在离线识别", visible_ui)
        self.assertNotIn("BackendState", visible_ui)
        self.assertNotIn("识别完成，请检查黄色低置信度单元格后导出", source)

    def test_toolbar_omits_removed_controls(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/mainwindow.h").read_text(encoding="utf-8")

        self.assertNotIn('QStringLiteral("自动定位表格")', source)
        self.assertNotIn('QStringLiteral("重新打开")', source)
        self.assertNotIn("m_cropMode", source + header)
        self.assertIn("m_backend->recognize(m_sourceImagePath,", source)
        self.assertIn('QStringLiteral("auto"),', source)
        self.assertIn("m_recognitionSourceIsRectified);", source)
        self.assertIn("QTimer::singleShot(0, this, [this]() { startBackgroundRecognition(); });", source)
        self.assertNotIn("current-input.png", source)

    def test_user_can_crop_one_target_table_without_overwriting_the_original(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/mainwindow.h").read_text(encoding="utf-8")
        preview = (ROOT / "src/gui/imagepreview.cpp").read_text(encoding="utf-8")
        preview_header = (ROOT / "src/gui/imagepreview.h").read_text(encoding="utf-8")

        self.assertIn('QStringLiteral("框选表格")', source)
        self.assertIn("beginCropSelection", source + preview + preview_header)
        self.assertIn("cropSelected(const QRect &imageRect)", preview_header)
        self.assertIn("cropSelectionActiveChanged(bool active)", preview_header)
        self.assertIn("isCropSelectionActive() const", preview_header)
        self.assertIn("if (m_cropSelectionActive) {", preview)
        self.assertIn("event->key() == Qt::Key_Escape", preview)
        self.assertIn('QStringLiteral("取消框选")', source)
        self.assertIn("!cropSelectionActive && (!busy || hiddenRecognition)", source)
        self.assertIn("source.copy(padded).save(path, \"PNG\")", source)
        self.assertIn("bounded.width() * 0.015", source)
        self.assertIn("bounded.height() * 0.025", source)
        self.assertIn("previewImage.setDevicePixelRatio(1.0);", preview)
        self.assertIn('QStringLiteral("crop-%1.png")', source)
        self.assertIn("clearOwnedCroppedImage", source + header)
        self.assertIn("m_backend->cancel();", source[source.index("void MainWindow::selectTableRegion") : source.index("void MainWindow::applyTableCrop")])
        self.assertNotIn("m_sourceImagePath = path;", source[source.index("void MainWindow::applyTableCrop") : source.index("void MainWindow::takePhoto")])

    def test_exports_replace_targets_only_after_complete_write(self):
        csv_exporter = (ROOT / "src/core/csvexporter.cpp").read_text(encoding="utf-8")
        pipeline = (ROOT / "backend/table_pipeline.py").read_text(encoding="utf-8")

        self.assertIn("QSaveFile file(filePath);", csv_exporter)
        self.assertIn("file.commit()", csv_exporter)
        self.assertIn("tempfile.NamedTemporaryFile", pipeline)
        self.assertIn("os.replace(temporary, destination)", pipeline)

    def test_background_recognition_is_revealed_only_after_button_click(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/mainwindow.h").read_text(encoding="utf-8")

        self.assertIn("startBackgroundRecognition", source + header)
        self.assertIn("m_recognitionDisplayRequested", source + header)

    def test_hidden_recognition_can_be_replaced_and_cancel_is_not_an_error(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/mainwindow.h").read_text(encoding="utf-8")
        runner = (ROOT / "src/gui/backendrunner.cpp").read_text(encoding="utf-8")

        self.assertIn("m_openButton->setEnabled(!cropSelectionActive && (!busy || hiddenRecognition));", source)
        self.assertIn("m_cameraButton->setEnabled(!cropSelectionActive && (!busy || hiddenRecognition));", source)
        self.assertIn('message == QStringLiteral("操作已取消")', source)
        self.assertIn('m_statusLabel->setText(QStringLiteral("识别已取消"))', source)
        self.assertIn("m_stopping", runner)
        self.assertIn("beginStopping(action, QStringLiteral(\"操作已取消\"));", runner)
        self.assertNotIn("waitForFinished(3000)", runner)
        self.assertIn("m_restartRecognitionAfterCancel", source + header)
        self.assertIn("responseRequestId != m_activeRequestId", runner)
        self.assertNotIn("识别已达到30秒上限", runner)
        self.assertIn("m_pendingRecognitionResponse", source + header)
        self.assertIn("m_pendingRecognitionError", source + header)
        self.assertIn("if (!m_recognitionDisplayRequested)", source)
        self.assertIn("m_pendingRecognitionResponse = response;", source)
        self.assertIn("m_recognitionDisplayRequested = true;", source)
        self.assertIn("正在精确识别，请稍等", source)

    def test_camera_capture_and_import_start_automatic_recognition(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/mainwindow.h").read_text(encoding="utf-8")
        dialog = (ROOT / "src/gui/cameracapturedialog.cpp").read_text(encoding="utf-8")
        project = (ROOT / "src/gui/gui.pro").read_text(encoding="utf-8")

        self.assertIn('QStringLiteral("拍照")', source)
        self.assertIn("void MainWindow::takePhoto()", source)
        self.assertIn("CameraCaptureDialog dialog(this);", source)
        self.assertIn("m_backend->cancel();", source[source.index("void MainWindow::takePhoto()") : source.index("bool MainWindow::loadImage")])
        self.assertIn("正在释放识别资源并打开摄像头", source)
        self.assertIn("m_cameraButton", source + header)
        self.assertIn("QCameraInfo::availableCameras()", dialog)
        self.assertIn("QCameraImageCapture::CaptureToFile", dialog)
        self.assertIn("QCamera::CaptureStillImage", dialog)
        self.assertIn("searchAndLock(locks)", dialog)
        self.assertIn("SIGNAL(locked())", dialog)
        self.assertIn("SIGNAL(lockFailed())", dialog)
        self.assertIn("m_focusTimer->start(3500)", dialog)
        self.assertNotIn("singleShot(1200", dialog)
        self.assertIn("actualPixels < minimumPixels", dialog)
        self.assertIn("assessCaptureQuality", dialog)
        self.assertIn('QStringLiteral("重新拍摄")', dialog)
        self.assertIn('QStringLiteral("仍然使用")', dialog)
        self.assertIn("loadImage(dialog.capturedImagePath())", source)
        self.assertIn('QStringLiteral("导入图片后将自动识别表格")', source)
        self.assertNotIn("m_backend->warmup();", source)
        self.assertIn("multimedia multimediawidgets", project)
        self.assertLess(
            source.index("actionLayout->addWidget(m_recognizeButton);"),
            source.index("actionLayout->addWidget(m_cameraButton);"),
        )

    def test_imported_image_preview_applies_exif_orientation(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")

        load_image = source[
            source.index("bool MainWindow::loadImage") :
            source.index("void MainWindow::startBackgroundRecognition")
        ]
        self.assertIn("QImageReader reader(path);", load_image)
        self.assertIn("reader.setAutoTransform(true);", load_image)
        self.assertIn("const QImage image = reader.read();", load_image)

    def test_maximum_recognition_has_no_deadline(self):
        runner = (ROOT / "src/gui/backendrunner.cpp").read_text(encoding="utf-8")
        backend = (ROOT / "backend/ocr_backend.py").read_text(encoding="utf-8")

        self.assertIn('options.insert(QStringLiteral("deadline_seconds"), 0);', runner)
        self.assertIn('options.insert(QStringLiteral("accuracy_mode"), QStringLiteral("maximum"));', runner)
        self.assertIn('startRequest(QStringLiteral("recognize"), request, 0);', runner)
        self.assertIn("if (timeoutMilliseconds > 0)", runner)
        self.assertIn("class _RecognitionBudget", backend)
        self.assertIn('result["recognition_state"]', backend)
        self.assertIn('document_mode == "text_list"', backend)

    def test_backend_releases_native_model_memory_between_recognitions(self):
        runner = (ROOT / "src/gui/backendrunner.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/backendrunner.h").read_text(encoding="utf-8")

        self.assertNotIn("recycleBackendAfterRecognition", runner + header)
        self.assertNotIn("m_pendingRequestObject", runner + header)
        self.assertIn("if (m_process->state() != QProcess::NotRunning)", runner)
        self.assertIn("m_process->closeWriteChannel();", runner)
        self.assertNotIn('QStringLiteral("--persistent")', runner)
        self.assertIn("等待一次性 worker 自然退出后再发布", runner)
        self.assertIn("m_process->kill();", runner)
        response_section = runner[
            runner.index("void BackendRunner::readStandardOutput") :
            runner.index("void BackendRunner::requestTimedOut")
        ]
        self.assertNotIn("m_process->kill();", response_section)
        self.assertNotIn("waitForFinished(3000)", runner)
        self.assertIn('QStringLiteral("可用内存不足")',
                      (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8"))

    def test_rectified_images_are_not_cropped_again(self):
        runner = (ROOT / "src/gui/backendrunner.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/backendrunner.h").read_text(encoding="utf-8")
        window = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")

        self.assertIn("bool inputRectified = false", header)
        self.assertIn('options.insert(QStringLiteral("input_rectified"), true);', runner)
        self.assertIn('QStringLiteral("rectified-")', window)
        self.assertIn("m_recognitionSourceIsRectified", window)

    def test_completed_recognition_displays_measured_elapsed_time(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        runner = (ROOT / "src/gui/backendrunner.cpp").read_text(encoding="utf-8")

        self.assertIn('response.value(QStringLiteral("worker_wall_seconds"))', source)
        self.assertIn('response.value(QStringLiteral("elapsed_seconds")).toDouble()', source)
        self.assertIn('response.insert(QStringLiteral("response_exit_seconds")', runner)
        self.assertIn("m_responseReadyElapsedMilliseconds", runner)
        self.assertIn("精确识别完成 · 用时 %1 秒", source)
        self.assertIn("publication_block_reasons", source)
        self.assertIn('response.value(QStringLiteral("image_quality"))', source)
        self.assertIn('detailTexts.append(QStringLiteral("图像提示：%1")', source)
        self.assertIn(
            "m_statusLabel->setToolTip(detailText.isEmpty() ? statusText : detailText)",
            source,
        )

    def test_backend_failures_retry_and_remain_inline_with_chinese_messages(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        tabledata = (ROOT / "src/core/tabledata.cpp").read_text(encoding="utf-8")

        self.assertIn("localizedBackendError(message)", source)
        self.assertIn('message.contains(QStringLiteral("严重模糊"))', source)
        self.assertIn('message.contains(QStringLiteral("边缘贴近照片边界"))', source)
        self.assertIn('message.contains(QStringLiteral("未纳入网格"))', source)
        self.assertIn('message.contains(QStringLiteral("扩展矫正后仍无法保持"))', source)
        self.assertIn('message.contains(QStringLiteral("有可见内容但模型无法安全确认"))', source)
        self.assertIn("表格一侧仍有列没有完整进入结果", source)
        self.assertIn("为避免漏格或猜错内容", source)
        self.assertNotIn("showBackendFailureDialog", source)
        self.assertNotIn("BackendErrorDialog", source)
        self.assertNotIn("QMessageBox", source)
        self.assertIn("containsLatinLetter", source)
        self.assertIn("containsChinese && !containsLatinLetter", source)
        self.assertIn("无法保存 CSV 文件", source)
        self.assertNotIn('QMessageBox::critical(this, QStringLiteral("导出失败"), error)', source)
        self.assertNotIn("识别结果未生成", source)
        self.assertIn("m_recognitionRetryCount < 1", source)
        self.assertIn("首次识别未完成，正在自动恢复并重试", source)
        self.assertIn("识别未完成：%1 可重新识别或重新框选表格", source)
        for english_error in (
            "Unsupported backend protocol",
            "OCR failed",
            "Backend response has no cell grid",
            "Backend response has no grid dimensions",
            "Backend response grid dimensions are invalid",
            "Backend response contains a non-array row",
            "Backend response contains a non-rectangular grid",
            "Backend response contains an invalid cell",
            "Backend response contains invalid cell fields",
            "Backend response contains an unmarked low-confidence cell",
        ):
            self.assertNotIn(english_error, tabledata)
        self.assertIn('m_statusLabel->setText(QStringLiteral("操作失败：%1")', source)
        self.assertIn("m_statusLabel->setToolTip(localizedBackendDetail(message, displayMessage));", source)
        self.assertIn("这不是程序故障", source)

    def test_recognition_restart_and_failure_cannot_export_stale_table(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/mainwindow.h").read_text(encoding="utf-8")

        self.assertIn("clearPublishedResult", source + header)
        self.assertIn(
            'clearPublishedResult(QStringLiteral("本次识别未生成可导出结果"));',
            source,
        )
        self.assertIn("m_table->setRowCount(0);", source)
        self.assertIn("m_table->setColumnCount(0);", source)
        self.assertIn("m_spans = QJsonArray();", source)
        self.assertIn("m_resultStack->setCurrentIndex(0);", source)
        self.assertIn("m_backend->isRunning() || m_table->rowCount() == 0", source)

    def test_backend_runtime_failures_are_actionable_and_qt_uses_live_source(self):
        window = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        runner = (ROOT / "src/gui/backendrunner.cpp").read_text(encoding="utf-8")
        runner_header = (ROOT / "src/gui/backendrunner.h").read_text(encoding="utf-8")

        for marker in (
            "另一个任务正在运行",
            "操作超时",
            "无法启动识别组件",
            "无法向识别组件发送任务",
            "没有返回有效结果",
            "识别组件意外退出",
        ):
            self.assertIn(marker, window)
        self.assertIn("识别组件本次运行异常", window)
        self.assertIn("QString::fromLocal8Bit(__FILE__)", runner)
        self.assertIn('../../backend/ocr_backend.py', runner)
        self.assertLess(
            runner.index("QString::fromLocal8Bit(__FILE__)"),
            runner.index("QStringList roots;"),
        )
        self.assertIn("QByteArray m_standardError", runner_header)
        self.assertIn("m_standardError.right(16000)", runner)
        self.assertIn("m_process->write(payload) != payload.size()", runner)
        self.assertIn('QStringLiteral("识别组件协议版本不兼容")', runner)
        self.assertIn("responseRequestIdValue.toDouble() != responseRequestId", runner)
        self.assertIn("certificateVersion.toDouble() != 1.0", window)
        self.assertIn(
            "certifiedRows.toDouble() != static_cast<double>(certifiedRows.toInt(-1))",
            window,
        )

    def test_cross_model_conflict_is_not_reported_as_missing_models(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")

        conflict_check = 'message.contains(QStringLiteral("跨模型冲突"))'
        generic_model_check = 'message.contains(QStringLiteral("model"), Qt::CaseInsensitive)'
        self.assertLess(source.index(conflict_check), source.index(generic_model_check))
        self.assertIn("不同模型或结构结果不一致", source)
        self.assertIn("软件主动没有生成结果", source)

    def test_fused_screen_columns_have_a_specific_blocking_message(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")

        self.assertIn('message.contains(QStringLiteral("表头列可能已合并"))', source)
        self.assertIn("已阻止输出可能融合的结果", source)

    def test_recognition_result_requires_manual_review_notice(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/mainwindow.h").read_text(encoding="utf-8")
        qss = (ROOT / "assets/app.qss").read_text(encoding="utf-8")

        self.assertIn("m_reviewNotice", source + header)
        self.assertIn("请对照原图核对行列、文字、数字、符号、单位及空白", source)
        self.assertIn("确认无误后导出", source)
        self.assertIn("m_reviewNotice->show();", source)
        self.assertIn("项待确认", source)
        self.assertIn('response.value(QStringLiteral("publication_blocked"))', source)
        self.assertIn("publicationBlockedValue.isBool()", source)
        self.assertIn("publicationBlockedValue.toBool()", source)
        self.assertIn("!structureCertificateValue.isUndefined()", source)
        self.assertIn("&& !structureCertificateValue.isNull()", source)
        self.assertNotIn("if (!validatedSpans.isEmpty()", source)
        self.assertIn("localizedBackendDetail", source)
        self.assertNotIn("Unverified structure cannot publish", source)
        self.assertNotIn("Backend span would hide", source)
        span_validator = source[source.index("bool validateSpans") : source.index("MainWindow::MainWindow")]
        self.assertNotIn("|| table.needsReview", span_validator)
        self.assertIn("unsafeSpansDiscarded", source)
        self.assertIn("合并关系未通过安全校验，已按独立单元格显示", source)
        self.assertIn("有风险待核对", source)
        self.assertIn("风险原因", source)
        self.assertIn("仍可导出当前结果", source)
        self.assertIn("m_statusLabel->setToolTip(detailText.isEmpty() ? statusText : detailText);", source)
        self.assertIn("#ReviewNotice", qss)

    def test_table_headers_do_not_clip_at_high_dpi(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        qss = (ROOT / "assets/app.qss").read_text(encoding="utf-8")

        self.assertIn("setHorizontalScrollMode(QAbstractItemView::ScrollPerPixel)", source)
        self.assertIn("setVerticalScrollMode(QAbstractItemView::ScrollPerPixel)", source)
        self.assertIn("QScroller::grabGesture", source)
        self.assertIn("horizontalHeader()->setFixedHeight(tableHeaderHeight)", source)
        self.assertIn("horizontalHeader()->setMinimumSectionSize(82)", source)
        self.assertIn("verticalHeader()->setDefaultSectionSize(tableRowHeight)", source)
        self.assertIn("rowHeaderMetrics.width", source)
        self.assertIn("verticalHeader()->setFixedWidth(qMax(48, rowHeaderWidth))", source)
        self.assertIn("resizeRowsToContents()", source)
        self.assertNotIn("m_table->resizeColumnsToContents();", source)
        self.assertIn("titleRows.contains(row)", source)
        self.assertIn("m_table->setColumnWidth(column, qBound(110, contentWidth, 360));", source)
        self.assertIn("tableScroller->stop();", source)
        self.assertIn("horizontalScrollBar()->setSliderPosition", source)
        self.assertIn("QTimer::singleShot(0, this, resetScrollPosition);", source)
        self.assertIn("titleFont.setPixelSize(18);", source)
        self.assertIn("headerFont.setPixelSize(14);", source)
        self.assertIn('role == QStringLiteral("subtitle")', source)
        self.assertIn("centeredHeaderItems.append(subtitleItem);", source)
        self.assertIn("titleFont.setWeight(QFont::DemiBold);", source)
        self.assertIn('QColor(QStringLiteral("#E8F0F8"))', source)
        self.assertIn('QColor(QStringLiteral("#183B5B"))', source)
        self.assertIn("m_table->setRowHeight(titleRow, qMax(m_table->rowHeight(titleRow), 56));", source)
        self.assertIn("m_table->setRowHeight(headerRow, qMax(m_table->rowHeight(headerRow), 48));", source)
        self.assertNotIn("titleFont.pointSizeF() * 1.12", source)
        self.assertIn("QTableWidget {", qss)
        self.assertIn("font-size: 13px;", qss)
        self.assertIn("QHeaderView::section {", qss)
        self.assertIn("font-size: 12px;", qss)

    def test_risky_results_remain_exportable_without_warning_dialog(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        qss = (ROOT / "assets/app.qss").read_text(encoding="utf-8")

        self.assertIn(
            "m_csvButton->setEnabled(!cropSelectionActive && exportReady);",
            source,
        )
        self.assertIn(
            "m_xlsxButton->setEnabled(!cropSelectionActive",
            source,
        )
        self.assertIn("const bool exportReady = !busy && hasTable;", source)
        self.assertNotIn("&& !m_publicationBlocked", source)
        self.assertNotIn("&& pending == 0", source)
        self.assertIn("可导出当前结果", source)
        self.assertNotIn("仍要继续导出吗", source)
        self.assertNotIn("confirmPendingReviewExport", source)
        self.assertIn("黄色单元格仍需核对", source)
        self.assertIn('cell.insert(QStringLiteral("needs_review"), table.needsReview(row, column));',
                      (ROOT / "src/gui/backendrunner.cpp").read_text(encoding="utf-8"))
        self.assertIn("QScrollBar:vertical", qss)
        self.assertIn("width: 30px;", qss)
        self.assertIn("QScrollBar:horizontal", qss)
        self.assertIn("height: 30px;", qss)
        self.assertIn("min-height: 64px;", qss)
        self.assertIn("min-width: 64px;", qss)
        self.assertIn("QAbstractScrollArea::corner", qss)
        self.assertIn("QTableCornerButton::section", qss)

    def test_qt_builtin_context_menus_load_simplified_chinese_translation(self):
        main = (ROOT / "src/gui/main.cpp").read_text(encoding="utf-8")
        build = (ROOT / "scripts/build_msvc2015.ps1").read_text(encoding="utf-8")

        self.assertIn("QTranslator qtTranslator", main)
        self.assertIn('QStringLiteral("qt_zh_CN")', main)
        self.assertIn('QStringLiteral("translations")', main)
        self.assertIn('"translations\\qt_zh_CN.qm"', build)
        self.assertIn("--no-translations", build)

    def test_touch_click_enters_edit_mode_and_requests_keyboard(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        header = (ROOT / "src/gui/mainwindow.h").read_text(encoding="utf-8")

        self.assertIn("QAbstractItemView::SelectedClicked", source)
        self.assertIn("&QTableWidget::cellClicked, this, &MainWindow::editTableCell", source)
        self.assertIn("m_table->editItem(item);", source)
        self.assertIn("QGuiApplication::inputMethod()->show();", source)
        self.assertIn("TabTip.exe", source)
        self.assertIn("editTableCell", header)

    def test_all_primary_work_surfaces_have_tablet_touch_contracts(self):
        source = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        viewer = (ROOT / "src/gui/imageviewerdialog.cpp").read_text(encoding="utf-8")
        camera = (ROOT / "src/gui/cameracapturedialog.cpp").read_text(encoding="utf-8")
        qss = (ROOT / "assets/app.qss").read_text(encoding="utf-8")

        self.assertIn("QScrollerProperties", source)
        self.assertIn("DragVelocitySmoothingFactor", source)
        self.assertIn("DecelerationFactor", source)
        self.assertIn("MaximumVelocity", source)
        self.assertIn("tableRowHeight = qMax(44", source)
        self.assertIn("setMinimumHeight(48);", camera)
        self.assertIn("min-width: 48px;", qss)
        self.assertIn("min-height: 48px;", qss)
        self.assertIn("min-height: 44px;", qss)
        self.assertIn("padding: 12px 30px 12px 14px;", qss)

        self.assertIn("setAttribute(Qt::WA_AcceptTouchEvents, true)", viewer)
        self.assertIn("bool viewportEvent(QEvent *event) override", viewer)
        self.assertIn("QEvent::TouchBegin", viewer)
        self.assertIn("QEvent::TouchUpdate", viewer)
        self.assertIn("QEvent::TouchEnd", viewer)
        self.assertIn("touchPoints().size() >= 2", viewer)
        self.assertIn("m_lastTouchDistance", viewer)
        self.assertIn("horizontalScrollBar()->setValue", viewer)
        self.assertIn("verticalScrollBar()->setValue", viewer)
        self.assertNotIn("双指缩放", viewer)
        self.assertNotIn("单指移动", viewer)

    def test_camera_and_preview_prefer_quality_without_repeated_scaling(self):
        camera = (ROOT / "src/gui/cameracapturedialog.cpp").read_text(encoding="utf-8")
        preview = (ROOT / "src/gui/imagepreview.cpp").read_text(encoding="utf-8")

        self.assertIn("QCamera::BackFace", camera)
        self.assertIn('description.contains(QStringLiteral("8m"))', camera)
        self.assertIn("QMultimedia::VeryHighQuality", camera)
        self.assertIn("maximumPreviewPixels", camera)
        self.assertIn("1280LL * 720LL", camera)
        self.assertIn("12LL * 1024LL * 1024LL", camera)
        self.assertIn("pixels > preferredPixels", camera)
        self.assertIn("m_scaledPreview", preview)
        self.assertIn("previewImage.scaled(targetSize", preview)
        self.assertIn("QPixmap::fromImage", preview)

    def test_stale_rectified_previews_are_cleaned_without_touching_other_files(self):
        window = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")

        self.assertIn("clearStaleRectifiedImages();", window)
        self.assertIn('QStringLiteral("rectified-*.png")', window)
        self.assertIn("QDir::Files | QDir::NoSymLinks", window)
        self.assertIn("currentDateTimeUtc().addDays(-1)", window)

    def test_preview_opens_centered_zoomable_image_viewer(self):
        preview = (ROOT / "src/gui/imagepreview.cpp").read_text(encoding="utf-8")
        viewer = (ROOT / "src/gui/imageviewerdialog.cpp").read_text(encoding="utf-8")
        window = (ROOT / "src/gui/mainwindow.cpp").read_text(encoding="utf-8")
        project = (ROOT / "src/gui/gui.pro").read_text(encoding="utf-8")

        self.assertIn("emit activated();", preview)
        self.assertIn("QGraphicsView::ScrollHandDrag", viewer)
        self.assertIn("void wheelEvent(QWheelEvent", viewer)
        self.assertIn("void mouseDoubleClickEvent(QMouseEvent", viewer)
        self.assertIn("parentWidget()->frameGeometry().center()", viewer)
        self.assertIn("available.width() * 4 / 5", viewer)
        self.assertIn("available.height() * 4 / 5", viewer)
        self.assertIn("setMinimumSize(qMin(520, usableWidth), qMin(320, usableHeight))", viewer)
        self.assertNotIn("QGridLayout *header", viewer)
        self.assertNotIn("hintLabel", viewer)
        self.assertIn("qBound(available.left(), centered.x(), maximumX)", viewer)
        self.assertIn("ImageViewerDialog dialog(image, title, this);", window)
        self.assertIn("imageviewerdialog.cpp", project)


if __name__ == "__main__":
    unittest.main()
